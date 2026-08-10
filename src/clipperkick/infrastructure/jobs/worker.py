"""Punto de entrada del proceso hijo que ejecuta una etapa.

Se invoca como `python -m clipperkick.infrastructure.jobs.worker <spec.json>`.
El contrato con el padre es estricto y minimo:

- `stdout` transporta **solo** mensajes NDJSON del protocolo. Cualquier otra
  cosa que se imprima ahi rompe el contrato, asi que `print` queda redirigido a
  `stderr` desde el primer instante.
- `stderr` es diagnostico libre y jamas decide nada.
- `stdin` es el canal de cancelacion: una linea de control basta para pedir la
  parada cooperativa, y la terminacion forzada la decide el padre por reloj.

El hijo siempre emite un mensaje terminal (`resultado`, `error` o `cancelado`)
salvo cuando muere de golpe, que es justamente el caso que el coordinador debe
saber interpretar por su cuenta.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading

from clipperkick.domain.jobs import PROTOCOLO_WORKER, ManifiestoSalida, MensajeWorker, TipoMensaje

from .protocol import codificar, es_control_de_cancelacion
from .stages import REGISTRO_STAGES, CancelacionStage, ContextoStage, FalloStage


#: Codigo de salida con el que un worker se retira al quedarse huerfano.
CODIGO_SALIDA_HUERFANO = 33
CODIGO_STAGE_DESCONOCIDO = "stage.desconocido"
CODIGO_SPEC_INVALIDA = "worker.spec"
CODIGO_EXCEPCION = "stage.excepcion"


class Emisor:
    """Escribe mensajes en binario para no depender de la traduccion de saltos."""

    def __init__(self, flujo) -> None:
        self._flujo = flujo

    def __call__(self, mensaje: MensajeWorker) -> None:
        self.crudo(codificar(mensaje))

    def crudo(self, datos: bytes) -> None:
        self._flujo.write(datos)
        self._flujo.flush()


def _escuchar_control(entrada, cancelado: threading.Event) -> None:
    """Canal de control y, a la vez, detector de muerte del padre.

    El EOF de `stdin` es la senal portable de que el coordinador ya no existe:
    cuando su proceso muere, el sistema cierra su extremo de la tuberia. Sin
    reaccionar a eso, un worker sobrevive a su coordinador, sigue escribiendo en
    el proyecto y compite con el intento que la siguiente apertura reprograma.

    La salida es brutal a proposito: no hay a quien informar —el unico lector se
    ha ido— y cualquier limpieza ordenada solo alargaria la ventana en la que dos
    procesos creen ser el mismo intento.
    """
    try:
        for linea in iter(entrada.readline, b""):
            if es_control_de_cancelacion(linea.strip()):
                # Se marca y se **sigue leyendo**. Salir del bucle aqui dejaria el
                # canal sin lector y el EOF de la muerte del padre no llegaria
                # nunca: un stage sordo, ya cancelado, sobreviviria a su
                # coordinador. La orden es idempotente, de modo que repetirla no
                # cuesta nada y quedarse escuchando lo cubre todo.
                cancelado.set()
    except (OSError, ValueError):
        pass
    os._exit(CODIGO_SALIDA_HUERFANO)


def _leer_spec(ruta: str) -> dict[str, object]:
    documento = json.loads(Path(ruta).read_text(encoding="utf-8"))
    if not isinstance(documento, dict) or documento.get("protocolo") != PROTOCOLO_WORKER:
        raise ValueError("la especificacion no pertenece a este protocolo")
    for obligatorio in ("job_id", "attempt_id", "stage", "directorio"):
        if not isinstance(documento.get(obligatorio), str):
            raise ValueError(f"la especificacion no declara '{obligatorio}'")
    return documento


def principal(argumentos: list[str]) -> int:
    emitir = Emisor(sys.stdout.buffer)
    # A partir de aqui `stdout` es del protocolo: cualquier impresion accidental
    # de una etapa —o de una biblioteca— iria a `stderr` y no rompera el canal.
    sys.stdout = sys.stderr
    if len(argumentos) != 2:
        emitir(MensajeWorker(tipo=TipoMensaje.ERROR, codigo=CODIGO_SPEC_INVALIDA,
                             detalle="se esperaba exactamente una ruta de especificacion"))
        return 2
    try:
        spec = _leer_spec(argumentos[1])
    except (OSError, ValueError) as error:
        emitir(MensajeWorker(tipo=TipoMensaje.ERROR, codigo=CODIGO_SPEC_INVALIDA,
                             detalle=str(error)))
        return 2

    cancelado = threading.Event()
    threading.Thread(target=_escuchar_control, args=(sys.stdin.buffer, cancelado),
                     daemon=True).start()

    emitir(MensajeWorker(tipo=TipoMensaje.INICIO, clave="worker.inicio", pid=os.getpid()))
    etapa = REGISTRO_STAGES.get(str(spec["stage"]))
    if etapa is None:
        emitir(MensajeWorker(tipo=TipoMensaje.ERROR, codigo=CODIGO_STAGE_DESCONOCIDO,
                             detalle=str(spec["stage"])))
        return 2

    payload = spec.get("payload") or {}
    checkpoint = spec.get("checkpoint")
    contexto = ContextoStage(
        job_id=str(spec["job_id"]), attempt_id=str(spec["attempt_id"]),
        directorio=Path(str(spec["directorio"])),
        payload=payload if isinstance(payload, dict) else {},
        checkpoint=checkpoint if isinstance(checkpoint, dict) else None,
        emitir=emitir, emitir_crudo=emitir.crudo, cancelado=cancelado.is_set)
    try:
        manifiesto = etapa(contexto)
    except CancelacionStage as error:
        emitir(MensajeWorker(tipo=TipoMensaje.CANCELADO, clave="stage.cancelado",
                             detalle=str(error)))
        return 0
    except FalloStage as error:
        emitir(MensajeWorker(tipo=TipoMensaje.ERROR, codigo=error.codigo, detalle=str(error)))
        return 1
    except BaseException as error:  # noqa: BLE001 - la frontera no deja escapar nada
        emitir(MensajeWorker(tipo=TipoMensaje.ERROR, codigo=CODIGO_EXCEPCION,
                             detalle=f"{type(error).__name__}: {error}"))
        return 1
    if isinstance(manifiesto, ManifiestoSalida):
        emitir(MensajeWorker(tipo=TipoMensaje.RESULTADO, clave="stage.resultado",
                             manifiesto=manifiesto))
    return 0


def salir(codigo: int) -> None:
    """Termina sin pasar por la finalizacion del interprete.

    El escucha de cancelacion es una hebra demonio bloqueada en `stdin`. Si se
    deja que el interprete finalice con ella dentro de una lectura nativa, en
    Windows el proceso muere con una violacion de acceso (`0xC0000005`) en lugar
    de con su codigo: el coordinador veria un crash donde hubo un final limpio.

    Salir asi es seguro porque este proceso no tiene nada pendiente de volcar:
    cada mensaje del protocolo se escribe con `flush` inmediato, y aqui se
    fuerza el de ambos flujos antes de irse.
    """
    for flujo in (sys.__stdout__, sys.__stderr__):
        try:
            flujo.flush()
        except (OSError, ValueError):
            pass
    os._exit(codigo)


if __name__ == "__main__":
    salir(principal(sys.argv))
