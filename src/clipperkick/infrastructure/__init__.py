"""Adaptadores de procesos externos y sistema de archivos."""

from .ffmpeg import FILTRO_VERTICAL, buscar_fuente, construir_filtro, escapar_texto
from .legacy import ES_WINDOWS, RE_EBUR, SIN_CONSOLA, crear_adaptadores_compatibilidad
from .probe import SondaFfprobe

__all__ = ["ES_WINDOWS", "FILTRO_VERTICAL", "RE_EBUR", "SIN_CONSOLA", "SondaFfprobe",
           "buscar_fuente", "construir_filtro", "crear_adaptadores_compatibilidad", "escapar_texto"]
