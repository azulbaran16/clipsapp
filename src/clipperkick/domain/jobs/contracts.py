"""Alineacion entre un job persistido y el contrato de su etapa.

Un `Job` viaja en disco y puede haber sido escrito por otra version de la
aplicacion, por una herramienta de diagnostico o por un caller descuidado. La
`StageDefinition` es el contrato vigente. Cuando ambos discrepan gana la etapa,
y esa decision se toma en un unico sitio para que no existan dos fuentes de
verdad sobre lo mismo:

| Campo | Regla | Por que |
| --- | --- | --- |
| `version_contrato` | debe coincidir | Ejecutar codigo nuevo bajo una version fijada rompe la reproducibilidad que el plan promete |
| `max_intentos` | techo de la politica de la etapa | El presupuesto de reintentos es una propiedad de la etapa, no un deseo del caller |
| `recursos` | maximo por clase | Subdeclarar peso —o cambiar CPU por GPU— eludiria la capacidad configurada |

Se permite pedir *menos* reintentos o *mas* peso: ambas cosas son mas
conservadoras que el contrato. Lo que no se permite es lo contrario.
"""

from __future__ import annotations

from dataclasses import replace

from .errors import ErrorContratoStage
from .models import ClaseRecurso, Job, Recursos, StageDefinition


def recursos_efectivos(declarados: Recursos, contrato: Recursos) -> Recursos:
    """Maximo por clase: el contrato es un piso, nunca un techo."""
    return Recursos(*(max(declarados.peso(clase), contrato.peso(clase))
                      for clase in ClaseRecurso))


def contrato_compatible(job: Job, stage: StageDefinition) -> bool:
    return job.version_contrato == stage.version_contrato


def exigir_contrato(job: Job, stage: StageDefinition) -> None:
    if not contrato_compatible(job, stage):
        raise ErrorContratoStage(
            f"El trabajo {job.id!r} esta fijado a la version {job.version_contrato!r} de"
            f" '{stage.nombre}' y la etapa disponible ofrece {stage.version_contrato!r}.")


def conciliar_con_stage(job: Job, stage: StageDefinition) -> Job:
    """Devuelve el job tal y como su etapa admite ejecutarlo.

    Se aplica al encolar —para que lo persistido ya sea correcto— y otra vez al
    planificar, porque una fila puede venir de una sesion anterior y el
    planificador no debe fiarse de lo que lee.
    """
    exigir_contrato(job, stage)
    return replace(
        job,
        max_intentos=min(job.max_intentos, stage.politica_retry.max_intentos),
        recursos=recursos_efectivos(job.recursos, stage.recursos))
