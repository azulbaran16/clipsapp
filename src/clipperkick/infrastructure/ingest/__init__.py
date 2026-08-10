"""Adaptadores de ingesta: filesystem, ffprobe, yt-dlp y SQLite."""

from .artifacts import NOMBRE_INTENTO, AlmacenArtefactosIngestaProyecto, digest_archivo
from .download import (
    GRACIA_TERMINACION, NOMBRE_SALIDA, DescargadorYtDlp, comando_descarga, lanzar_proceso,
)
from .fingerprints import INTENTOS_POR_DEFECTO, ServicioHuellaArchivo
from .probe import ARGUMENTOS_SONDEO, SondaFfprobeDetallada
from .repository import RepositorioSqliteFuentes, ruta_publicada
from .sources import (
    NOMBRE_MARCADOR, PREFIJO_PARCIAL, PREFIJO_STAGING, SUFIJO_PARCIAL, AlmacenFuentesProyecto,
)
from .wiring import crear_caso_de_uso_ingesta

__all__ = [
    "ARGUMENTOS_SONDEO", "AlmacenArtefactosIngestaProyecto", "AlmacenFuentesProyecto",
    "DescargadorYtDlp", "GRACIA_TERMINACION", "INTENTOS_POR_DEFECTO", "NOMBRE_INTENTO",
    "NOMBRE_MARCADOR", "NOMBRE_SALIDA", "PREFIJO_PARCIAL", "PREFIJO_STAGING",
    "RepositorioSqliteFuentes", "SUFIJO_PARCIAL", "ServicioHuellaArchivo",
    "SondaFfprobeDetallada", "comando_descarga", "crear_caso_de_uso_ingesta", "digest_archivo",
    "lanzar_proceso", "ruta_publicada",
]
