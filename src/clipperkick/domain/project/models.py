"""Valores independientes del almacenamiento de proyectos."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class InformacionProyecto:
    project_id: str
    nombre: str
    schema_version: int


@dataclass(frozen=True)
class ResultadoReconciliacion:
    fuentes_faltantes: int
    artefactos_reconstruibles: int
