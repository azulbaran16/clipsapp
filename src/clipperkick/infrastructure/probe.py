"""Adaptador de inspeccion de medios para el backend FFmpeg."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import json
import math
import shutil
import subprocess

from clipperkick.domain import ErrorSondeoMedia, InformacionMedia

from .legacy import SIN_CONSOLA


@dataclass(frozen=True)
class ResultadoEjecucion:
    codigo_salida: int
    salida: str
    error: str = ""


Ejecutor = Callable[[Sequence[str]], ResultadoEjecucion]
BuscadorHerramienta = Callable[[str], str | None]


def ejecutar_comando(comando: Sequence[str]) -> ResultadoEjecucion:
    resultado = subprocess.run(
        comando, capture_output=True, text=True, errors="replace",
        creationflags=SIN_CONSOLA,
    )
    return ResultadoEjecucion(resultado.returncode, resultado.stdout, resultado.stderr)


class SondaFfprobe:
    """Obtiene informacion media sin filtrar detalles de ffprobe a capas altas."""

    def __init__(self, ejecutor: Ejecutor = ejecutar_comando,
                 buscar_herramienta: BuscadorHerramienta = shutil.which) -> None:
        self._ejecutor = ejecutor
        self._buscar_herramienta = buscar_herramienta

    def sondear(self, archivo: str) -> InformacionMedia:
        ejecutable = self._buscar_herramienta("ffprobe")
        if ejecutable is None:
            raise ErrorSondeoMedia("No encuentro 'ffprobe'. Instala FFmpeg y vuelve a intentarlo.")
        try:
            resultado = self._ejecutor((
                ejecutable, "-v", "error", "-show_entries",
                "format=duration:stream=codec_type,width,height", "-of", "json", archivo,
            ))
        except OSError as error:
            raise ErrorSondeoMedia("No se pudo iniciar la inspeccion del archivo multimedia.") from error
        if resultado.codigo_salida != 0:
            raise ErrorSondeoMedia("No se pudo inspeccionar el archivo multimedia.")
        try:
            datos = json.loads(resultado.salida)
            duracion = float(datos["format"]["duration"])
            streams = datos["streams"]
            video = next(stream for stream in streams if stream["codec_type"] == "video")
            ancho, alto = int(video["width"]), int(video["height"])
        except (KeyError, TypeError, ValueError, StopIteration, json.JSONDecodeError) as error:
            raise ErrorSondeoMedia("La informacion tecnica del archivo es incompleta o invalida.") from error
        if not math.isfinite(duracion) or duracion < 0 or ancho <= 0 or alto <= 0:
            raise ErrorSondeoMedia("La informacion tecnica del archivo es incompleta o invalida.")
        tiene_audio = any(stream.get("codec_type") == "audio" for stream in streams)
        return InformacionMedia(duracion, ancho, alto, tiene_audio)
