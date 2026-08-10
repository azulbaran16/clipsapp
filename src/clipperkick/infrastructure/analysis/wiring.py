"""Ensamblado del analisis basico sobre un proyecto abierto."""

from __future__ import annotations

from collections.abc import Sequence

from clipperkick.application.analysis import (
    CasoDeUsoAnalisis, Notificador, Progreso, ProveedorSenalOpcional, sin_notificar,
    sin_progreso,
)
from clipperkick.domain.analysis import ErrorAnalisis
from clipperkick.infrastructure.ingest.sources import AlmacenFuentesProyecto
from clipperkick.infrastructure.project.persistence import ProyectoAbierto

from .artifacts import AlmacenArtefactosAnalisisProyecto
from .ffmpeg import ExtractorAudioFfmpeg, MedidorEbur128Ffmpeg
from .repository import RepositorioSqliteAnalisis


def crear_caso_de_uso_analisis(
        proyecto: ProyectoAbierto, *,
        senales_opcionales: Sequence[ProveedorSenalOpcional] = (),
        notificar: Notificador = sin_notificar,
        progreso: Progreso = sin_progreso) -> CasoDeUsoAnalisis:
    if proyecto.solo_lectura:
        raise ErrorAnalisis("Un proyecto abierto en solo lectura no puede analizar fuentes.")
    raiz = proyecto.repositorio.raiz
    artefactos = AlmacenArtefactosAnalisisProyecto(raiz)
    return CasoDeUsoAnalisis(
        localizador=AlmacenFuentesProyecto(raiz), extractor=ExtractorAudioFfmpeg(),
        medidor=MedidorEbur128Ffmpeg(), artefactos=artefactos,
        repositorio=RepositorioSqliteAnalisis(proyecto.repositorio, artefactos),
        senales_opcionales=senales_opcionales, notificar=notificar, progreso=progreso)


__all__ = ["crear_caso_de_uso_analisis"]
