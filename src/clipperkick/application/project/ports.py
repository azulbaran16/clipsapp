"""Interfaces que la aplicacion necesita para abrir proyectos."""

from __future__ import annotations

from typing import Protocol

from clipperkick.domain.project import InformacionProyecto, ResultadoReconciliacion


class RepositorioProyecto(Protocol):
    def informacion(self) -> InformacionProyecto: ...
    def reconciliar(self) -> ResultadoReconciliacion: ...
