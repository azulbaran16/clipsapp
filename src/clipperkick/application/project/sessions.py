"""Puertos y lecturas que la carcasa necesita para vivir sobre un proyecto.

`RepositorioProyecto` (T02) describe lo que se puede *consultar* de un proyecto
ya abierto. Falta lo anterior: abrirlo, crearlo, cerrarlo y recordar cuales se
usaron. Eso es lo que se declara aqui, en la aplicacion, para que la carcasa
PySide6 pueda pedirlo sin conocer carpetas, locks ni SQLite.

`SnapshotProyecto` es deliberadamente un valor plano y no una referencia viva a
la sesion: es lo unico que cruza hacia los view models, de modo que una vista no
puede —ni por descuido ni por conveniencia— llamar a la base de datos desde el
hilo de la interfaz.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from clipperkick.domain.project import InformacionProyecto, ResultadoReconciliacion


@dataclass(frozen=True)
class SnapshotProyecto:
    """Estado publicable de un proyecto abierto."""

    ruta: str
    project_id: str
    nombre: str
    schema_version: int
    solo_lectura: bool
    fuentes_faltantes: int = 0
    artefactos_reconstruibles: int = 0

    @property
    def requiere_atencion(self) -> bool:
        """Una fuente ausente exige a la persona localizarla; un artefacto no.

        Los artefactos reconstruibles se regeneran solos en la siguiente etapa,
        asi que degradan el proyecto sin bloquearlo. Distinguirlos aqui evita que
        la UI muestre una alarma por algo que el sistema ya sabe rehacer.
        """
        return self.fuentes_faltantes > 0


@dataclass(frozen=True)
class ProyectoReciente:
    """Entrada de la lista de recientes. `visto_en` es epoch en segundos."""

    ruta: str
    nombre: str
    visto_en: float = 0.0


class SesionProyecto(Protocol):
    """Un proyecto abierto y su propiedad sobre la carpeta.

    Es un recurso, no un dato: mientras viva retiene el lock de escritor y la
    conexion SQLite, y `cerrar` es la unica forma de devolverlos.
    """

    @property
    def ruta(self) -> str: ...

    @property
    def solo_lectura(self) -> bool: ...

    def informacion(self) -> InformacionProyecto: ...
    def reconciliar(self) -> ResultadoReconciliacion: ...
    def cerrar(self) -> None: ...


class CatalogoProyectos(Protocol):
    """Crea y abre proyectos en el almacenamiento local."""

    def crear(self, ruta: str, nombre: str) -> SesionProyecto: ...
    def abrir(self, ruta: str) -> SesionProyecto: ...


class RepositorioRecientes(Protocol):
    """Lista de proyectos recientes, ordenada del mas reciente al mas antiguo."""

    def listar(self) -> tuple[ProyectoReciente, ...]: ...
    def registrar(self, reciente: ProyectoReciente) -> None: ...
    def olvidar(self, ruta: str) -> None: ...
