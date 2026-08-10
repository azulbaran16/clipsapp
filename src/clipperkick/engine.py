"""Fachada compatible del Motor legado.

Las implementaciones viven ahora en dominio, aplicacion e infraestructura.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from .application import CasoDeUsoCompatibilidad
from .domain import ErrorAmigable, SolicitudClips, elegir_picos, formato_legible, formato_nombre
from .infrastructure import (ES_WINDOWS, FILTRO_VERTICAL, RE_EBUR, SIN_CONSOLA,
                             buscar_fuente, construir_filtro as _construir_filtro,
                             escapar_texto)

Log = Callable[[str], None]


def crear_caso_de_uso_compatibilidad(
    log: Log | None = None,
) -> CasoDeUsoCompatibilidad:
    """Ensamblado de produccion para la interfaz de compatibilidad."""
    from .infrastructure import crear_adaptadores_compatibilidad

    fuente, analizador, exportador, espacio_temporal, herramientas = (
        crear_adaptadores_compatibilidad()
    )
    return CasoDeUsoCompatibilidad(
        fuente, analizador, exportador, espacio_temporal, herramientas, log
    )


def construir_filtro(vertical: bool, nombre: str, con_logo: bool, fuente: str | None = None) -> str:
    """Compatibilidad: conserva la resolucion automatica de fuente del Motor."""
    return _construir_filtro(vertical, nombre, con_logo, buscar_fuente() if fuente is None else fuente)


class Motor:
    """Fachada del flujo anterior; preferir el caso de uso de aplicacion."""

    def __init__(self, log: Log | None = None) -> None:
        self.log = log or (lambda _mensaje: None)
        self._caso_de_uso: CasoDeUsoCompatibilidad = crear_caso_de_uso_compatibilidad(self.log)

    @staticmethod
    def _comprobar_herramienta(nombre: str, instalacion: str) -> None:
        from .infrastructure.legacy import comprobar_herramienta
        comprobar_herramienta(nombre, instalacion)

    def obtener_video(self, entrada: str, carpeta_temporal: str, hd: bool) -> str:
        return self._caso_de_uso.fuente.obtener(entrada, carpeta_temporal, hd, self.log)

    def analizar_audio(self, video: str) -> list[float]:
        return self._caso_de_uso.analizador.analizar(video, self.log)

    def exportar(self, video: str, picos: Sequence[tuple[int, float]], duracion: int,
                 carpeta_salida: str, vertical: bool, nombre_canal: str = "", logo: str = "") -> list[str]:
        return self._caso_de_uso.exportador.exportar(video, picos, duracion, carpeta_salida,
                                                      vertical, nombre_canal, logo, self.log)

    def procesar(self, entrada: str, numero_clips: int, duracion: int, carpeta_salida: str,
                 vertical: bool, hd: bool, nombre_canal: str = "", logo: str = "") -> list[str]:
        return self._caso_de_uso.procesar(SolicitudClips(
            entrada, numero_clips, duracion, carpeta_salida, vertical, hd, nombre_canal, logo
        ))
