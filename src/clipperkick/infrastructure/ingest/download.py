"""Descargador de fuentes remotas, con el proceso hijo en propiedad.

El adaptador legado (`FuenteYtDlp`) sigue intacto para la UI de compatibilidad,
pero no sirve aqui y la razon no es de estilo: bloquea leyendo `stdout` y
despues en `wait()`, sin ninguna ruta de `terminate`/`kill`. Una ingesta que se
cancela no puede pedirle que pare, y el context manager que retira el staging
corre mientras el hijo **todavia escribe dentro**: en POSIX queda un escritor
suelto y en Windows el directorio ni siquiera se puede borrar mientras ese
handle siga abierto.

Por eso esta capa posee su propio `Popen` y garantiza una sola cosa por encima
de todas: **al volver, el hijo esta recolectado**. Da igual si termino solo, si
coopero al `terminate` o si hubo que matarlo; `descargar` no devuelve el control
—ni por retorno ni por excepcion— con un proceso vivo o sin `wait()`.

El ciclo de cancelacion es el mismo que T03 fijo para los workers, y por la
misma razon: cooperativo primero, forzado despues de una gracia medida, y
siempre `wait()` antes de tocar el sistema de archivos.

```text
cancelado() -> terminate() -> wait(gracia) -> [kill() -> wait()] -> cerrar pipes -> join
```

La lectura vive en una hebra que solo mueve lineas a una cola. Sin ella,
`readline()` bloquearia hasta la siguiente linea del hijo y un proceso colgado
haria que la cancelacion no se observase nunca.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading

from clipperkick.application.ingest.ports import Cancelacion, Progreso, nunca_cancelado
from clipperkick.domain.ingest import ErrorCancelacionIngesta, ErrorDescargaIngesta

from ..legacy import SIN_CONSOLA


HERRAMIENTA = "yt-dlp"
INSTALACION = 'python -m pip install -U "yt-dlp[default,curl-cffi]"'
#: Nombre fijo del resultado. La descarga vive en un staging privado por
#: ingesta, de modo que no hay colisiones posibles y el nombre no tiene que
#: derivarse del titulo remoto —que llega del mundo y habria que sanear—.
NOMBRE_SALIDA = "vod.mp4"
ALTURA_HD = 1080
ALTURA_SD = 720
#: Cada cuanto se comprueba la cancelacion mientras no llegan lineas.
RODAJA_ESPERA = 0.05
#: Gracia entre pedir la parada y forzarla.
GRACIA_TERMINACION = 5.0

RE_PORCENTAJE = re.compile(r"([\d.]+)\s*%")

Lanzador = Callable[[Sequence[str]], subprocess.Popen]
BuscadorHerramienta = Callable[[str], str | None]


def comando_descarga(ejecutable: str, url: str, destino: str, hd: bool) -> list[str]:
    """Comando de descarga. Mismos criterios que el flujo legado de la UI."""
    altura = ALTURA_HD if hd else ALTURA_SD
    return [ejecutable, "-f", f"bv*[height<={altura}]+ba/b[height<={altura}]/b",
            "--merge-output-format", "mp4", "-o", destino, "--no-playlist", "--newline", url]


def lanzar_proceso(comando: Sequence[str]) -> subprocess.Popen:
    """`Popen` con una sola tuberia: `stderr` entra en `stdout`.

    Con dos tuberias haria falta un lector por cada una para no bloquear al
    hijo cuando llene un buffer; con una sola, un unico lector basta y el
    diagnostico llega intercalado en el mismo orden en que se produjo.
    """
    return subprocess.Popen(list(comando), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, creationflags=SIN_CONSOLA)


class _ProcesoDescarga:
    """Hijo vivo. Su unica promesa es no sobrevivir a `cerrar()`."""

    def __init__(self, proceso: subprocess.Popen, gracia: float = GRACIA_TERMINACION) -> None:
        self._proceso = proceso
        self._gracia = gracia
        self._cola: queue.Queue[str] = queue.Queue()
        self._fin_lectura = threading.Event()
        self._hilo = threading.Thread(target=self._bombear, daemon=True)
        self._hilo.start()

    # ------------------------------------------------------------------ #

    def _bombear(self) -> None:
        try:
            flujo = self._proceso.stdout
            if flujo is not None:
                for linea in iter(flujo.readline, b""):
                    self._cola.put(linea.decode("utf-8", errors="replace").rstrip())
        except (OSError, ValueError):
            pass  # tuberia cerrada por `cerrar()`
        finally:
            self._fin_lectura.set()

    def esperar(self, cancelado: Cancelacion,
                registrar: Callable[[str], None]) -> int:
        """Consume la salida hasta el final, vigilando la cancelacion.

        La cancelacion se comprueba en cada rodaja y no solo entre lineas: un
        hijo que se queda callado —resolviendo un DNS, negociando TLS, o
        simplemente colgado— no emitiria nada y la orden no se atenderia jamas.
        """
        while True:
            if cancelado():
                self.detener()
                raise ErrorCancelacionIngesta("La descarga se cancelo antes de terminar.")
            try:
                registrar(self._cola.get(timeout=RODAJA_ESPERA))
            except queue.Empty:
                # Muerto *y* leido: sin la segunda mitad se declararia terminada
                # una descarga cuyas ultimas lineas siguen en la tuberia.
                if (self._proceso.poll() is not None and self._fin_lectura.is_set()
                        and self._cola.empty()):
                    break
        return self._proceso.wait()

    def detener(self) -> None:
        """Cooperativo, luego forzado; en ambos casos con `wait()` al final."""
        if self._proceso.poll() is None:
            try:
                self._proceso.terminate()
            except OSError:
                pass
            try:
                self._proceso.wait(timeout=self._gracia)
            except subprocess.TimeoutExpired:
                try:
                    self._proceso.kill()
                except OSError:
                    pass
                try:
                    self._proceso.wait(timeout=self._gracia)
                except subprocess.TimeoutExpired:
                    pass

    def cerrar(self) -> None:
        """Recolecta al hijo y suelta sus handles. Idempotente y sin excepciones.

        El orden importa en Windows: mientras el hijo viva o la tuberia siga
        abierta, el directorio de staging no se puede retirar. Se termina, se
        espera, se cierra el flujo y solo entonces se deja de mirar la hebra.
        """
        self.detener()
        try:
            self._proceso.wait(timeout=self._gracia)
        except (subprocess.TimeoutExpired, OSError):
            pass
        flujo = self._proceso.stdout
        if flujo is not None:
            try:
                flujo.close()
            except OSError:
                pass
        self._hilo.join(timeout=self._gracia)

    @property
    def pid(self) -> int | None:
        return self._proceso.pid

    def codigo_salida(self) -> int | None:
        return self._proceso.poll()


class DescargadorYtDlp:
    """Implementa `DescargadorFuente` con el subproceso bajo control propio."""

    def __init__(self, lanzador: Lanzador = lanzar_proceso,
                 buscar_herramienta: BuscadorHerramienta = shutil.which,
                 gracia: float = GRACIA_TERMINACION) -> None:
        self._lanzar = lanzador
        self._buscar_herramienta = buscar_herramienta
        self._gracia = gracia

    def descargar(self, url: str, directorio: str, hd: bool = True,
                  progreso: Progreso | None = None,
                  cancelado: Cancelacion | None = None) -> str:
        carpeta = Path(directorio)
        if not carpeta.is_dir():
            raise ErrorDescargaIngesta("El directorio de descarga no existe.")
        vigilar = cancelado if cancelado is not None else nunca_cancelado
        if vigilar():
            # Cancelada antes de empezar: no se lanza nada. Lanzar y matar
            # inmediatamente seria correcto pero deja rastro —un proceso, un
            # archivo a medias— por un trabajo que nadie llego a pedir.
            raise ErrorCancelacionIngesta("La descarga se cancelo antes de empezar.")
        ejecutable = self._buscar_herramienta(HERRAMIENTA)
        if ejecutable is None:
            raise ErrorDescargaIngesta(
                f"No encuentro '{HERRAMIENTA}'. Instalalo con: {INSTALACION}")
        salida = carpeta / NOMBRE_SALIDA
        try:
            proceso = self._lanzar(comando_descarga(ejecutable, url, str(salida), hd))
        except OSError as error:
            raise ErrorDescargaIngesta(f"No se pudo iniciar la descarga de {url!r}.") from error
        handle = _ProcesoDescarga(proceso, self._gracia)
        try:
            codigo = handle.esperar(vigilar, self._registrador(progreso))
        finally:
            # Pase lo que pase —fin normal, cancelacion o excepcion del
            # llamador— el hijo queda recolectado antes de que nadie toque el
            # staging. Es la unica garantia que el puerto promete.
            handle.cerrar()
        if codigo != 0:
            raise ErrorDescargaIngesta(
                f"No se pudo descargar {url!r} (codigo {codigo}). Actualiza {HERRAMIENTA} y"
                " verifica que el enlace abra en tu navegador.")
        return str(self._exigir_dentro(str(salida), carpeta, url))

    # ------------------------------------------------------------------ #

    def _registrador(self, progreso: Progreso | None) -> Callable[[str], None]:
        def registrar(mensaje: str) -> None:
            if progreso is None:
                return
            coincidencia = RE_PORCENTAJE.search(mensaje or "")
            if coincidencia:
                try:
                    progreso(float(coincidencia.group(1)), 100.0)
                except ValueError:
                    pass

        return registrar

    def _exigir_dentro(self, obtenido: object, destino: Path, url: str) -> Path:
        """Lo devuelto tiene que ser un archivo real, no vacio y de este staging."""
        if not isinstance(obtenido, str) or not obtenido:
            raise ErrorDescargaIngesta(f"La descarga de {url!r} no devolvio un archivo.")
        candidata = Path(obtenido)
        try:
            resuelta = candidata.resolve()
            raiz = destino.resolve()
        except OSError as error:
            raise ErrorDescargaIngesta(
                f"No se pudo resolver el resultado de descargar {url!r}.") from error
        if raiz not in resuelta.parents:
            raise ErrorDescargaIngesta(
                f"La descarga de {url!r} dejo su resultado fuera del directorio pedido.")
        if not resuelta.is_file() or resuelta.stat().st_size == 0:
            # Un archivo vacio es el sintoma habitual de una descarga que fallo
            # sin decirlo. Adoptarlo produciria una fuente con huella valida y
            # cero bytes: reproducible, verificable y completamente inutil.
            raise ErrorDescargaIngesta(f"La descarga de {url!r} no dejo contenido utilizable.")
        return resuelta
