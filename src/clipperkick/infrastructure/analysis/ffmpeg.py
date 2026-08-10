"""Extraccion y ebur128 con FFmpeg, CPU-only y sin red."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
import math
import re
import shutil
import subprocess

from clipperkick.application.analysis.ports import Cancelacion
from clipperkick.domain.analysis import (
    ConfiguracionExtraccion, ConfiguracionRasgos, ErrorAnalisis, ErrorAudioAusente,
    ErrorCancelacionAnalisis,
)


RE_MUESTRA = re.compile(
    r"\bt:\s*([0-9]+(?:\.[0-9]+)?)\b.*?\bM:\s*(-?(?:[0-9]+(?:\.[0-9]+)?|inf))\b",
    re.IGNORECASE,
)
PATRONES_SIN_AUDIO = (
    "matches no streams", "does not contain any stream", "stream map '0:a:0' matches no streams",
)


@dataclass(frozen=True)
class ResultadoProceso:
    codigo: int
    salida: str = ""
    error: str = ""


class EjecutorFfmpeg:
    """Subproceso cancelable sin shell ni hilos residentes."""

    def ejecutar(self, comando: Sequence[str], cancelado: Cancelacion | None = None,
                 ) -> ResultadoProceso:
        try:
            proceso = subprocess.Popen(
                list(comando), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
        except OSError as error:
            raise ErrorAnalisis("No se pudo iniciar FFmpeg.") from error
        while True:
            try:
                salida, error = proceso.communicate(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                if cancelado is None or not cancelado():
                    continue
                proceso.terminate()
                try:
                    salida, error = proceso.communicate(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proceso.kill()
                    salida, error = proceso.communicate()
                raise ErrorCancelacionAnalisis("El analisis se cancelo durante FFmpeg.")
        return ResultadoProceso(int(proceso.returncode or 0), salida, error)


class _HerramientaFfmpeg:
    def __init__(self, ejecutor: EjecutorFfmpeg | None = None,
                 localizar: Callable[[str], str | None] = shutil.which) -> None:
        self._ejecutor = ejecutor if ejecutor is not None else EjecutorFfmpeg()
        self._localizar = localizar
        self._ruta: str | None = None
        self._version: str | None = None

    def _ffmpeg(self) -> str:
        if self._ruta is None:
            self._ruta = self._localizar("ffmpeg")
        if not self._ruta:
            raise ErrorAnalisis("FFmpeg no esta instalado o no esta disponible en PATH.")
        return self._ruta

    def version(self) -> str:
        if self._version is None:
            resultado = self._ejecutor.ejecutar((self._ffmpeg(), "-version"))
            if resultado.codigo != 0:
                raise ErrorAnalisis("No se pudo identificar la version de FFmpeg.")
            primera = (resultado.salida or resultado.error).splitlines()
            self._version = primera[0].strip() if primera else "ffmpeg/desconocida"
        return self._version


class ExtractorAudioFfmpeg(_HerramientaFfmpeg):
    def extraer(self, origen: str, destino: str, config: ConfiguracionExtraccion,
                cancelado: Cancelacion | None = None) -> None:
        comando = (
            self._ffmpeg(), "-hide_banner", "-nostdin", "-y", "-i", origen,
            "-map", "0:a:0", "-vn", "-ac", str(config.canales), "-ar",
            str(config.tasa_muestreo), "-c:a", config.codec, destino,
        )
        resultado = self._ejecutor.ejecutar(comando, cancelado)
        if resultado.codigo != 0:
            detalle = resultado.error.lower()
            if any(patron in detalle for patron in PATRONES_SIN_AUDIO):
                raise ErrorAudioAusente("La fuente no contiene una pista de audio.")
            raise ErrorAnalisis("FFmpeg no pudo preparar la pista de audio.")
        if not Path(destino).is_file():
            raise ErrorAnalisis("FFmpeg termino sin producir la pista de audio.")


class MedidorEbur128Ffmpeg(_HerramientaFfmpeg):
    def medir(self, ruta: str, config: ConfiguracionRasgos,
              cancelado: Cancelacion | None = None):
        comando = (
            self._ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "verbose",
            "-i", ruta, "-filter_complex", "ebur128=framelog=verbose", "-f", "null", "-",
        )
        resultado = self._ejecutor.ejecutar(comando, cancelado)
        if resultado.codigo != 0:
            raise ErrorAnalisis("FFmpeg no pudo medir la sonoridad del audio.")
        muestras: list[list[float]] = []
        for coincidencia in RE_MUESTRA.finditer(resultado.error):
            segundo = float(coincidencia.group(1))
            texto = coincidencia.group(2).lower()
            valor = float(config.piso_lufs) if "inf" in texto else float(texto)
            if math.isfinite(segundo) and math.isfinite(valor):
                muestras.append([segundo, max(float(config.piso_lufs), valor)])
        if not muestras:
            raise ErrorAnalisis("FFmpeg no produjo muestras ebur128 legibles.")
        return {"muestras": muestras, "unidad": "LUFS",
                "duracion_segundos": max(muestra[0] for muestra in muestras)}


__all__ = [
    "EjecutorFfmpeg", "ExtractorAudioFfmpeg", "MedidorEbur128Ffmpeg", "ResultadoProceso",
]
