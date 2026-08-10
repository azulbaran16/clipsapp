"""Puertos del coordinador de trabajos.

El coordinador solo sabe pedir tres cosas: estado durable (`RepositorioJobs`),
ejecucion aislada (`EjecutorStage`) y espacio de artefactos (`AlmacenArtefactos`).
SQLite, `subprocess` y el filesystem viven detras de estas firmas.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from clipperkick.domain.jobs import (
    ArchivoSalida, EstadoJob, EventoProgreso, Job, JobAttempt, ManifiestoSalida, MensajeWorker,
)


class Reloj(Protocol):
    """Tiempo monotono inyectable: los tests no dependen del reloj de pared."""

    def ahora(self) -> float: ...
    def dormir(self, segundos: float) -> None: ...


@dataclass(frozen=True)
class SolicitudEjecucion:
    job_id: str
    attempt_id: str
    stage: str
    version_contrato: str
    directorio: str
    payload: Mapping[str, object] = field(default_factory=dict)
    checkpoint: Mapping[str, object] | None = None


class HandleWorker(Protocol):
    """Worker vivo. `mensajes()` no bloquea: el coordinador nunca cede su turno."""

    @property
    def pid(self) -> int | None: ...

    def mensajes(self) -> tuple[MensajeWorker, ...]: ...
    def vivo(self) -> bool: ...
    def agotado(self) -> bool: ...
    def solicitar_cancelacion(self) -> None: ...
    def terminar(self) -> None: ...
    def codigo_salida(self) -> int | None: ...
    def diagnostico(self) -> str: ...
    def cerrar(self) -> None: ...


class EjecutorStage(Protocol):
    def lanzar(self, solicitud: SolicitudEjecucion) -> HandleWorker: ...


class AlmacenArtefactos(Protocol):
    """Stage temporal, validacion y publicacion atomica de la salida."""

    def preparar(self, job_id: str, attempt_id: str) -> str: ...
    def validar(self, directorio: str, manifiesto: ManifiestoSalida,
                salidas: Sequence[str] = ()) -> tuple[ArchivoSalida, ...]: ...
    def publicar(self, job_id: str, directorio: str, manifiesto: ManifiestoSalida,
                 attempt_id: str = "", clave: str = "", salidas: Sequence[str] = (),
                 stage: str = "", version_contrato: str = "") -> tuple[str, ...]: ...
    def cuarentena(self, directorio: str) -> str | None: ...
    def descartar(self, directorio: str) -> None: ...


class RepositorioJobs(Protocol):
    """Estado durable de jobs, intentos, dependencias y eventos.

    Dos exigencias, ambas estructurales:

    - **El lease.** Toda mutacion de un intento lo requiere: un coordinador que
      perdio la propiedad no puede publicar progreso ni cerrar el trabajo de otro.
    - **La historia.** Todo comando que cambie el estado observable de un `Job` o
      un `JobAttempt` recibe sus eventos sin valor por defecto. No es una
      convencion del llamador: un estado que ninguna consulta puede explicar es
      un estado que no debe poder escribirse, y la firma lo impide antes que
      cualquier revision de codigo. Los comandos que no cambian estado
      —`renovar_lease`, `registrar_pid`, `registrar_checkpoint`— no la exigen.
    """

    def encolar(self, job: Job, evento: EventoProgreso) -> Job: ...
    def obtener(self, job_id: str) -> Job: ...
    def listar(self) -> tuple[Job, ...]: ...
    def intentos(self, job_id: str) -> tuple[JobAttempt, ...]: ...

    def adquirir_lease(self, job_id: str, propietario: str, duracion: float,
                       ahora: float, evento: EventoProgreso,
                       max_intentos: int | None = None) -> JobAttempt: ...
    def renovar_lease(self, attempt_id: str, propietario: str, duracion: float,
                      ahora: float) -> None: ...
    def registrar_pid(self, attempt_id: str, propietario: str, pid: int | None,
                      ahora: float) -> None: ...
    def registrar_checkpoint(self, attempt_id: str, propietario: str,
                             checkpoint: Mapping[str, object], ahora: float,
                             eventos: Sequence[EventoProgreso] = ()) -> None: ...
    def marcar_estado(self, attempt_id: str, propietario: str, destino: EstadoJob,
                      ahora: float, eventos: Sequence[EventoProgreso]) -> None: ...
    def finalizar(self, attempt_id: str, propietario: str, destino: EstadoJob, ahora: float,
                  eventos: Sequence[EventoProgreso], error_codigo: str | None = None,
                  diagnostico: str | None = None,
                  artefactos: Sequence[tuple[str, str]] = ()) -> None: ...

    def solicitar_cancelacion(self, job_id: str, evento: EventoProgreso) -> bool: ...
    def reprogramar(self, job_id: str, ahora: float,
                    eventos: Sequence[EventoProgreso]) -> bool: ...
    def marcar_fallo_directo(self, job_id: str, ahora: float, error_codigo: str,
                             eventos: Sequence[EventoProgreso],
                             diagnostico: str | None = None) -> None: ...
    def entradas_invalidadas(self) -> frozenset[str]: ...
    def artefactos(self, job_id: str) -> tuple[tuple[str, str], ...]: ...
    def recuperar_huerfanos(
        self, propietario: str, ahora: float,
        evento: Callable[[str, str], EventoProgreso]) -> tuple[str, ...]: ...

    def registrar_evento(self, evento: EventoProgreso) -> EventoProgreso: ...
    def eventos(self, job_id: str | None = None, desde: int = 0) -> tuple[EventoProgreso, ...]: ...
