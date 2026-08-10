"""Modelo puro de trabajos durables por etapas."""

from .errors import (
    ErrorContratoStage, ErrorDependenciaJob, ErrorIdentificadorJob, ErrorJob, ErrorLeaseJob,
    ErrorProtocoloWorker, ErrorRecursoJob, ErrorStageDesconocido, ErrorTransicionJob,
    ErrorValidacionSalida,
)
from .identity import (
    LONGITUD_MAXIMA, RESERVADOS_WINDOWS, clave_canonica, es_componente_interno,
    exigir_componente_interno, exigir_ruta_interna,
)
from .models import (
    CODIGO_ARRANQUE, CODIGO_CANCELADO, CODIGO_CONTRATO, CODIGO_CRASH,
    CODIGO_DEPENDENCIA_INVALIDA, CODIGO_PRESUPUESTO, CODIGO_DEPENDENCIA, CODIGO_INTERRUMPIDO, CODIGO_LANZAMIENTO,
    CODIGO_PROTOCOLO, CODIGO_RECURSOS, CODIGO_SALIDA_INVALIDA, CODIGO_STAGE,
    CODIGO_TIMEOUT_CANCELACION, ESTADOS_ACTIVOS, ESTADOS_TERMINALES,
    LIMITES_POR_DEFECTO, PROTOCOLO_WORKER, TRANSICIONES, ArchivoSalida, ClaseRecurso, EstadoJob,
    EventoProgreso, Job, JobAttempt, ManifiestoSalida, MensajeWorker, PoliticaCancelacion,
    PoliticaRetry, Recursos, StageDefinition, TipoEvento, TipoMensaje, exigir_transicion,
    transicion_valida,
)
from .contracts import (
    conciliar_con_stage, contrato_compatible, exigir_contrato, recursos_efectivos,
)
from .scheduling import (
    EstadoDependencias, clasificar_dependencias, detectar_ciclo, excede_capacidad, exigir_dag,
    recursos_en_curso, seleccionar_ejecutables,
)

__all__ = [
    "CODIGO_ARRANQUE", "CODIGO_CANCELADO", "CODIGO_CONTRATO", "CODIGO_CRASH",
    "CODIGO_DEPENDENCIA_INVALIDA", "CODIGO_PRESUPUESTO", "CODIGO_DEPENDENCIA", "CODIGO_INTERRUMPIDO",
    "CODIGO_LANZAMIENTO", "CODIGO_PROTOCOLO", "CODIGO_RECURSOS", "CODIGO_SALIDA_INVALIDA",
    "CODIGO_STAGE", "CODIGO_TIMEOUT_CANCELACION",
    "ESTADOS_ACTIVOS", "ESTADOS_TERMINALES", "LIMITES_POR_DEFECTO", "PROTOCOLO_WORKER",
    "LONGITUD_MAXIMA", "RESERVADOS_WINDOWS",
    "TRANSICIONES", "ArchivoSalida", "ClaseRecurso", "ErrorContratoStage", "ErrorDependenciaJob",
    "ErrorIdentificadorJob", "ErrorJob",
    "ErrorLeaseJob", "ErrorProtocoloWorker", "ErrorRecursoJob", "ErrorStageDesconocido",
    "ErrorTransicionJob", "ErrorValidacionSalida", "EstadoDependencias", "EstadoJob",
    "EventoProgreso", "Job", "JobAttempt", "ManifiestoSalida", "MensajeWorker",
    "PoliticaCancelacion", "PoliticaRetry", "Recursos", "StageDefinition", "TipoEvento",
    "TipoMensaje", "clasificar_dependencias", "clave_canonica", "conciliar_con_stage",
    "contrato_compatible",
    "es_componente_interno", "exigir_componente_interno", "exigir_contrato",
    "exigir_ruta_interna", "recursos_efectivos", "detectar_ciclo", "excede_capacidad", "exigir_dag",
    "exigir_transicion", "recursos_en_curso", "seleccionar_ejecutables", "transicion_valida",
]
