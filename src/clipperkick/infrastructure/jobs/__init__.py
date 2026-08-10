"""Adaptadores del coordinador: SQLite, filesystem y procesos hijos."""

from .artifacts import AlmacenArtefactosProyecto
from .process import EjecutorSubproceso, HandleSubproceso
from .repository import RepositorioSqliteJobs
from .wiring import crear_coordinador
from .stages import (
    CODIGO_PERMANENTE_PRUEBA, CODIGO_TRANSITORIO_PRUEBA, DEFINICION_PRUEBA, DEFINICION_PRUEBA_GPU,
    DEFINICIONES, NOMBRE_STAGE_PRUEBA, NOMBRE_STAGE_PRUEBA_GPU, REGISTRO_STAGES, CancelacionStage,
    ContextoStage, FalloStage, registrar,
)

__all__ = [
    "AlmacenArtefactosProyecto", "CODIGO_PERMANENTE_PRUEBA", "CODIGO_TRANSITORIO_PRUEBA",
    "CancelacionStage", "ContextoStage", "DEFINICION_PRUEBA", "DEFINICIONES", "EjecutorSubproceso",
    "DEFINICION_PRUEBA_GPU", "FalloStage", "HandleSubproceso", "NOMBRE_STAGE_PRUEBA",
    "NOMBRE_STAGE_PRUEBA_GPU", "REGISTRO_STAGES",
    "RepositorioSqliteJobs", "crear_coordinador", "registrar",
]
