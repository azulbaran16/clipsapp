"""Errores tipados del coordinador de trabajos.

La regla es la misma que en T01/T02: ninguna capa superior debe distinguir un
fallo leyendo texto. Cada condicion que cambia una decision tiene su clase.
"""


class ErrorJob(Exception):
    """Falla del coordinador de trabajos."""


class ErrorTransicionJob(ErrorJob):
    """Se intento una transicion que la maquina de estados no admite."""


class ErrorLeaseJob(ErrorJob):
    """Quien intenta mutar el intento no es su arrendatario vigente."""


class ErrorDependenciaJob(ErrorJob):
    """El grafo declarado es incompleto o ciclico."""


class ErrorRecursoJob(ErrorJob):
    """El job exige mas de lo que la capacidad total puede conceder jamas."""


class ErrorProtocoloWorker(ErrorJob):
    """El worker emitio algo que no pertenece al protocolo versionado."""


class ErrorValidacionSalida(ErrorJob):
    """La salida declarada por el worker no supero la validacion."""


class ErrorStageDesconocido(ErrorJob):
    """El job nombra una etapa que este coordinador no sabe ejecutar."""


class ErrorIdentificadorJob(ErrorJob):
    """Un identificador no puede usarse como tramo de ruta interna del proyecto."""


class ErrorContratoStage(ErrorJob):
    """El job esta fijado a una version de contrato que la etapa ya no ofrece."""
