"""Adaptadores que ponen `crear_proyecto`/`abrir_proyecto` detras de un puerto.

Es una capa fina a proposito. La persistencia de T02 ya decide lo dificil —lock,
migracion, coherencia manifiesto/base—; aqui solo se traduce su API concreta al
vocabulario que la aplicacion declaro, sin reimplementar ni relajar nada.
"""

from __future__ import annotations

from pathlib import Path

from clipperkick.domain.project import InformacionProyecto, ResultadoReconciliacion

from .persistence import ProyectoAbierto, abrir_proyecto, crear_proyecto


class SesionProyectoLocal:
    """`ProyectoAbierto` visto como sesion: identidad, estado y cierre."""

    def __init__(self, abierto: ProyectoAbierto) -> None:
        self._abierto = abierto

    @property
    def ruta(self) -> str:
        return str(self._abierto.repositorio.raiz)

    @property
    def solo_lectura(self) -> bool:
        return self._abierto.solo_lectura

    def informacion(self) -> InformacionProyecto:
        return self._abierto.repositorio.informacion()

    def reconciliar(self) -> ResultadoReconciliacion:
        return self._abierto.repositorio.reconciliar()

    def cerrar(self) -> None:
        self._abierto.close()


class CatalogoProyectosLocal:
    """Catalogo sobre el filesystem local."""

    def crear(self, ruta: str, nombre: str) -> SesionProyectoLocal:
        return SesionProyectoLocal(crear_proyecto(Path(ruta), nombre))

    def abrir(self, ruta: str) -> SesionProyectoLocal:
        return SesionProyectoLocal(abrir_proyecto(Path(ruta)))
