"""Puertos que permiten a los casos de uso ignorar sus adaptadores."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import ContextManager, Protocol

from clipperkick.domain import InformacionMedia


Log = Callable[[str], None]


class FuenteVideo(Protocol):
    def obtener(self, entrada: str, carpeta_temporal: str, hd: bool, log: Log) -> str: ...


class AnalizadorAudio(Protocol):
    def analizar(self, video: str, log: Log) -> list[float]: ...


class ExportadorClips(Protocol):
    def exportar(
        self, video: str, picos: Sequence[tuple[int, float]], duracion: int,
        carpeta_salida: str, vertical: bool, nombre_canal: str, logo: str, log: Log,
    ) -> list[str]: ...


class EspacioTemporal(Protocol):
    def crear(self) -> ContextManager[str]: ...


class VerificadorHerramientas(Protocol):
    def comprobar_renderizador(self) -> None: ...


class SondaMedia(Protocol):
    def sondear(self, archivo: str) -> InformacionMedia: ...
