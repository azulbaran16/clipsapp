"""Politica pura de planificacion: dependencias, ciclos y limites de recursos.

Se mantiene separada del coordinador porque es la parte que debe poder probarse
sin base de datos ni procesos, y porque un adaptador nunca debe poder relajarla:
si algo corre, corrio porque esta funcion lo eligio.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum

from .errors import ErrorDependenciaJob
from .models import ESTADOS_ACTIVOS, EstadoJob, Job, Recursos


class EstadoDependencias(str, Enum):
    LISTAS = "listas"
    PENDIENTES = "pendientes"
    IMPOSIBLES = "imposibles"


def detectar_ciclo(dependencias: Mapping[str, Sequence[str]]) -> tuple[str, ...] | None:
    """Devuelve un ciclo concreto, o `None`. El ciclo se devuelve para poder
    diagnosticarlo: un booleano obligaria a la UI a redescubrirlo."""
    EN_CURSO, HECHO = 1, 2
    marca: dict[str, int] = {}
    camino: list[str] = []

    def visitar(nodo: str) -> tuple[str, ...] | None:
        estado = marca.get(nodo)
        if estado == HECHO:
            return None
        if estado == EN_CURSO:
            return tuple(camino[camino.index(nodo):]) + (nodo,)
        marca[nodo] = EN_CURSO
        camino.append(nodo)
        for vecino in dependencias.get(nodo, ()):  # una arista a un ausente no es ciclo
            ciclo = visitar(vecino)
            if ciclo is not None:
                return ciclo
        camino.pop()
        marca[nodo] = HECHO
        return None

    for nodo in dependencias:
        ciclo = visitar(nodo)
        if ciclo is not None:
            return ciclo
    return None


def exigir_dag(dependencias: Mapping[str, Sequence[str]], conocidos: Sequence[str]) -> None:
    """Rechaza aristas a jobs inexistentes y cualquier ciclo."""
    existentes = set(conocidos)
    for nodo, aristas in dependencias.items():
        for arista in aristas:
            if arista not in existentes:
                raise ErrorDependenciaJob(f"El job '{nodo}' depende de '{arista}', que no existe.")
    ciclo = detectar_ciclo(dependencias)
    if ciclo is not None:
        raise ErrorDependenciaJob("El grafo de trabajos contiene un ciclo: " + " -> ".join(ciclo))


def clasificar_dependencias(job: Job, estados: Mapping[str, EstadoJob]) -> EstadoDependencias:
    """Un dependiente solo corre con todas sus entradas validadas.

    `IMPOSIBLES` distingue el fallo permanente de una entrada del simple "aun no
    termino": sin esa distincion, un job cuyo padre fallo quedaria encolado para
    siempre sin que nadie pueda decirlo.
    """
    impedido = False
    for dependencia in job.dependencias:
        estado = estados.get(dependencia)
        if estado is EstadoJob.SUCCEEDED:
            continue
        if estado is None or estado is EstadoJob.FAILED:
            impedido = True
        else:
            return EstadoDependencias.PENDIENTES if not impedido else EstadoDependencias.IMPOSIBLES
    if impedido:
        return EstadoDependencias.IMPOSIBLES
    return EstadoDependencias.LISTAS


def _orden(job: Job) -> tuple[int, str]:
    return (-job.prioridad, job.id)


def seleccionar_ejecutables(jobs: Sequence[Job], limites: Recursos,
                            en_curso: Recursos = Recursos()) -> tuple[str, ...]:
    """Elige que jobs `queued` pueden arrancar ahora sin exceder los limites.

    El recorrido es por prioridad y luego por id —determinista y reproducible en
    los tests—; un job que no cabe no bloquea a los siguientes mas ligeros, pero
    tampoco cede su turno permanentemente porque el orden se recalcula en cada
    paso del coordinador.
    """
    estados = {job.id: job.estado for job in jobs}
    disponible = limites.restar(en_curso)
    elegidos: list[str] = []
    for job in sorted(jobs, key=_orden):
        if job.estado is not EstadoJob.QUEUED or job.cancelacion_solicitada:
            continue
        if clasificar_dependencias(job, estados) is not EstadoDependencias.LISTAS:
            continue
        if not job.recursos.cabe_en(disponible):
            continue
        elegidos.append(job.id)
        disponible = disponible.restar(job.recursos)
    return tuple(elegidos)


def recursos_en_curso(jobs: Sequence[Job]) -> Recursos:
    total = Recursos()
    for job in jobs:
        if job.estado in ESTADOS_ACTIVOS:
            total = total.sumar(job.recursos)
    return total


def excede_capacidad(job: Job, limites: Recursos) -> bool:
    """Un job que no cabe ni en la capacidad total nunca correra: es un error de
    configuracion, no una espera."""
    return not job.recursos.cabe_en(limites)
