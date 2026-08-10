"""Ejecutor de etapas en procesos hijos reales.

Aislar el trabajo pesado en un proceso es la unica forma de cumplir el criterio
"un crash del worker no derriba el coordinador": una hebra que se lleva el
interprete por delante —o que se queda colgada en una biblioteca nativa— no
tiene remedio desde dentro.

Los hilos que hay aqui son solo bombas de tuberia: leen `stdout`/`stderr` del
hijo y depositan mensajes ya decodificados en una cola. El coordinador nunca
bloquea leyendo, y por eso puede vigilar diez workers desde un unico bucle.
"""

from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import uuid

from clipperkick.application.jobs.ports import SolicitudEjecucion
from clipperkick.domain.jobs import (
    CODIGO_PROTOCOLO, PROTOCOLO_WORKER, ErrorProtocoloWorker, MensajeWorker, TipoMensaje,
)

from ..legacy import SIN_CONSOLA
from .protocol import CANAL_CANCELACION, codificar_control, decodificar


MODULO_WORKER = "clipperkick.infrastructure.jobs.worker"
NOMBRE_SPEC = "_worker.json"
LINEAS_DIAGNOSTICO = 40
GRACIA_CIERRE = 5.0


class HandleSubproceso:
    """Worker vivo. Nada de lo que haga el hijo puede bloquear al coordinador."""

    def __init__(self, proceso: subprocess.Popen) -> None:
        self._proceso = proceso
        self._cola: queue.Queue[MensajeWorker] = queue.Queue()
        self._errores: deque[str] = deque(maxlen=LINEAS_DIAGNOSTICO)
        self._fin_lectura = threading.Event()
        self._hilos = (
            threading.Thread(target=self._bombear_salida, daemon=True),
            threading.Thread(target=self._bombear_error, daemon=True),
        )
        for hilo in self._hilos:
            hilo.start()

    # ------------------------------------------------------------------ #

    def _bombear_salida(self) -> None:
        try:
            for linea in iter(self._proceso.stdout.readline, b""):
                linea = linea.strip()
                if not linea:
                    continue
                try:
                    self._cola.put(decodificar(linea))
                except Exception as error:
                    # Una linea invalida no se descarta en silencio ni se lleva
                    # por delante al lector: se convierte en un fallo nombrado
                    # para que el job muera diagnosticado. Se atrapa `Exception`
                    # entera porque si el decoder deja escapar algo inesperado,
                    # el sintoma seria una hebra muerta y un coordinador esperando
                    # mensajes que ya nadie va a entregar.
                    self._cola.put(MensajeWorker(tipo=TipoMensaje.ERROR, codigo=CODIGO_PROTOCOLO,
                                                 detalle=f"{type(error).__name__}: {error}"
                                                         f" | {linea[:200]!r}"))
        except (OSError, ValueError):
            pass  # tuberia cerrada por el cierre del handle
        finally:
            self._fin_lectura.set()

    def _bombear_error(self) -> None:
        try:
            for linea in iter(self._proceso.stderr.readline, b""):
                self._errores.append(linea.decode("utf-8", errors="replace").rstrip())
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------------ #

    @property
    def pid(self) -> int | None:
        return self._proceso.pid

    def mensajes(self) -> tuple[MensajeWorker, ...]:
        drenados: list[MensajeWorker] = []
        while True:
            try:
                drenados.append(self._cola.get_nowait())
            except queue.Empty:
                return tuple(drenados)

    def vivo(self) -> bool:
        return self._proceso.poll() is None

    def agotado(self) -> bool:
        """Muerto *y* leido.

        Sin la segunda mitad habria una carrera real: un worker que escribe su
        resultado y termina puede observarse muerto antes de que el lector haya
        drenado la tuberia, y el coordinador lo declararia un crash.
        """
        return not self.vivo() and self._fin_lectura.is_set() and self._cola.empty()

    def solicitar_cancelacion(self) -> None:
        try:
            self._proceso.stdin.write(codificar_control(CANAL_CANCELACION))
            self._proceso.stdin.flush()
        except (OSError, ValueError):
            pass  # el hijo ya no escucha; la terminacion forzada sigue disponible

    def terminar(self) -> None:
        if self.vivo():
            try:
                self._proceso.kill()
            except OSError:
                pass

    def codigo_salida(self) -> int | None:
        return self._proceso.poll()

    def diagnostico(self) -> str:
        return "\n".join(self._errores)

    def cerrar(self, gracia: float = GRACIA_CIERRE) -> None:
        """Espera un cierre limpio y solo entonces mata. Siempre recolecta."""
        try:
            if self.vivo():
                try:
                    self._proceso.wait(timeout=gracia)
                except subprocess.TimeoutExpired:
                    self.terminar()
                    try:
                        self._proceso.wait(timeout=gracia)
                    except subprocess.TimeoutExpired:
                        pass
        finally:
            for flujo in (self._proceso.stdin, self._proceso.stdout, self._proceso.stderr):
                if flujo is not None:
                    try:
                        flujo.close()
                    except OSError:
                        pass
            for hilo in self._hilos:
                hilo.join(timeout=gracia)


class EjecutorSubproceso:
    """Lanza `python -m clipperkick...worker` con la especificacion en disco.

    La especificacion viaja por archivo y no por `stdin` porque `stdin` es el
    canal de cancelacion: mezclarlos obligaria a delimitar dos protocolos sobre
    la misma tuberia, y un worker que aun no leyo su encargo no podria ser
    cancelado.

    El hijo **no** recibe su directorio de trabajo como `cwd`. En Windows el
    directorio actual de un proceso no puede renombrarse ni moverse, y esa es
    justamente la operacion con la que el coordinador publica o pone en
    cuarentena el temporal del intento. La ruta viaja en la especificacion.
    """

    def __init__(self, ejecutable: str = sys.executable, entorno: dict[str, str] | None = None,
                 modulo: str = MODULO_WORKER) -> None:
        self._ejecutable = ejecutable
        self._modulo = modulo
        self._entorno = entorno

    def _preparar_entorno(self) -> dict[str, str]:
        if self._entorno is not None:
            return dict(self._entorno)
        entorno = dict(os.environ)
        raiz = str(Path(__file__).resolve().parents[3])
        rutas = entorno.get("PYTHONPATH", "")
        if raiz not in rutas.split(os.pathsep):
            entorno["PYTHONPATH"] = raiz + (os.pathsep + rutas if rutas else "")
        entorno["PYTHONDONTWRITEBYTECODE"] = "1"
        return entorno

    def lanzar(self, solicitud: SolicitudEjecucion) -> HandleSubproceso:
        directorio = Path(solicitud.directorio)
        spec = directorio / NOMBRE_SPEC
        spec.write_text(json.dumps({
            "protocolo": PROTOCOLO_WORKER,
            "job_id": solicitud.job_id,
            "attempt_id": solicitud.attempt_id,
            "stage": solicitud.stage,
            "version_contrato": solicitud.version_contrato,
            "directorio": str(directorio),
            "payload": dict(solicitud.payload),
            "checkpoint": None if solicitud.checkpoint is None else dict(solicitud.checkpoint),
        }, ensure_ascii=False), encoding="utf-8")
        proceso = subprocess.Popen(
            [self._ejecutable, "-m", self._modulo, str(spec)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self._preparar_entorno(), creationflags=SIN_CONSOLA)
        return HandleSubproceso(proceso)


def identificador() -> str:
    return str(uuid.uuid4())
