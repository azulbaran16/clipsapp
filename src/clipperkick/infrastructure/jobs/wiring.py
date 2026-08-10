"""Ensamblado por defecto del coordinador sobre un proyecto abierto.

Existe para que la UI y las pruebas no tengan que repetir el cableado —y para
que ese cableado sea *uno solo*: si el almacen apuntase a una carpeta y el
repositorio a otra base, el fallo apareceria mucho mas tarde y mucho peor.
"""

from __future__ import annotations

from collections.abc import Mapping

from clipperkick.application.jobs import Coordinador
from clipperkick.domain.jobs import LIMITES_POR_DEFECTO, ErrorJob, Recursos, StageDefinition
from clipperkick.infrastructure.project.persistence import ProyectoAbierto

from .artifacts import AlmacenArtefactosProyecto
from .process import EjecutorSubproceso
from .repository import RepositorioSqliteJobs
from .stages import DEFINICIONES


def crear_coordinador(proyecto: ProyectoAbierto, *,
                      catalogo: Mapping[str, StageDefinition] | None = None,
                      limites: Recursos = LIMITES_POR_DEFECTO,
                      duracion_lease: float = 30.0,
                      propietario: str | None = None) -> Coordinador:
    """Ensambla el coordinador y recupera los huerfanos antes de devolverlo.

    La recuperacion no es un paso que el llamador pueda olvidar. Si se omitiese,
    los intentos que quedaron `running` en la sesion anterior seguirian contando
    como activos: ocuparian capacidad en `recursos_en_curso()` sin tener ningun
    worker detras y la cola podria no avanzar nunca. Es idempotente, asi que
    ensamblar dos veces no cuesta nada.

    Un proyecto en solo lectura no puede coordinar: no posee el lock de escritor
    y por tanto no puede ni tomar leases ni recuperar nada. Se rechaza aqui, con
    un error tipado, en vez de fallar mas tarde y de forma menos clara.
    """
    if proyecto.solo_lectura:
        raise ErrorJob("Un proyecto abierto en solo lectura no puede coordinar trabajos.")
    repositorio = RepositorioSqliteJobs(proyecto.repositorio)
    almacen = AlmacenArtefactosProyecto(proyecto.repositorio.raiz)
    coordinador = Coordinador(repositorio, EjecutorSubproceso(), almacen,
                              dict(catalogo if catalogo is not None else DEFINICIONES),
                              limites=limites, duracion_lease=duracion_lease,
                              propietario=propietario)
    coordinador.recuperar()
    return coordinador
