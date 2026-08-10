"""Modelo puro de etapas, trabajos, intentos, recursos y progreso.

Nada de esto conoce SQLite, procesos ni Qt: son los valores que el coordinador
persiste y que la UI observa. Las dos piezas con reglas de verdad son la maquina
de estados (`TRANSICIONES`) y la clasificacion de fallos: ambas viven aqui para
que un adaptador no pueda inventarse una transicion ni un reintento.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum


PROTOCOLO_WORKER = "clipsapp-worker/1"

# Codigo reservado: no lo produce ninguna etapa, lo produce la cancelacion.
CODIGO_CANCELADO = "cancelado"
CODIGO_INTERRUMPIDO = "interrumpido"
CODIGO_CRASH = "worker.crash"
CODIGO_PROTOCOLO = "worker.protocolo"
CODIGO_SALIDA_INVALIDA = "salida.invalida"
CODIGO_DEPENDENCIA = "dependencia.imposible"
CODIGO_TIMEOUT_CANCELACION = "cancelacion.forzada"
CODIGO_RECURSOS = "recursos.imposibles"
CODIGO_STAGE = "stage.desconocido"
CODIGO_LANZAMIENTO = "worker.lanzamiento"
CODIGO_CONTRATO = "contrato.incompatible"
CODIGO_ARRANQUE = "worker.sin_inicio"
CODIGO_DEPENDENCIA_INVALIDA = "dependencia.invalidada"
CODIGO_PRESUPUESTO = "intentos.agotados"


class EstadoJob(str, Enum):
    """Los siete estados publicados por el ticket, sin sinonimos internos."""

    QUEUED = "queued"
    RUNNING = "running"
    VALIDATING = "validating"
    SUCCEEDED = "succeeded"
    CANCEL_REQUESTED = "cancel_requested"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


#: Un job cancelado termina en `failed` con `CODIGO_CANCELADO`: el ticket no
#: define un estado terminal de cancelacion, y anadir uno haria que la UI
#: tuviese que conocer ocho estados para un caso que ya es un fin sin exito.
TRANSICIONES: Mapping[EstadoJob, frozenset[EstadoJob]] = {
    EstadoJob.QUEUED: frozenset({EstadoJob.RUNNING, EstadoJob.CANCEL_REQUESTED, EstadoJob.FAILED}),
    EstadoJob.RUNNING: frozenset({EstadoJob.VALIDATING, EstadoJob.CANCEL_REQUESTED,
                                  EstadoJob.FAILED, EstadoJob.INTERRUPTED}),
    EstadoJob.VALIDATING: frozenset({EstadoJob.SUCCEEDED, EstadoJob.FAILED, EstadoJob.INTERRUPTED}),
    EstadoJob.CANCEL_REQUESTED: frozenset({EstadoJob.FAILED, EstadoJob.INTERRUPTED}),
    # `interrupted` y `failed` vuelven a la cola solo por decision de politica;
    # nunca saltan directamente a `running` para que todo reintento nazca de un
    # encolado observable.
    EstadoJob.INTERRUPTED: frozenset({EstadoJob.QUEUED, EstadoJob.FAILED}),
    EstadoJob.FAILED: frozenset({EstadoJob.QUEUED}),
    EstadoJob.SUCCEEDED: frozenset(),
}

ESTADOS_TERMINALES = frozenset({EstadoJob.SUCCEEDED, EstadoJob.FAILED})
ESTADOS_ACTIVOS = frozenset({EstadoJob.RUNNING, EstadoJob.VALIDATING, EstadoJob.CANCEL_REQUESTED})


def transicion_valida(origen: EstadoJob, destino: EstadoJob) -> bool:
    return destino in TRANSICIONES[origen]


def exigir_transicion(origen: EstadoJob, destino: EstadoJob) -> EstadoJob:
    from .errors import ErrorTransicionJob

    if not transicion_valida(origen, destino):
        raise ErrorTransicionJob(f"Transicion no permitida: {origen.value} -> {destino.value}.")
    return destino


class ClaseRecurso(str, Enum):
    CPU = "cpu"
    GPU = "gpu"
    DISK = "disk"
    NETWORK = "network"


@dataclass(frozen=True)
class Recursos:
    """Peso que un job ocupa en cada clase mientras corre."""

    cpu: int = 0
    gpu: int = 0
    disk: int = 0
    network: int = 0

    def __post_init__(self) -> None:
        from .errors import ErrorRecursoJob

        for clase in ClaseRecurso:
            valor = getattr(self, clase.value)
            if not isinstance(valor, int) or isinstance(valor, bool) or valor < 0:
                raise ErrorRecursoJob(f"El peso de '{clase.value}' debe ser un entero no negativo.")

    def peso(self, clase: ClaseRecurso) -> int:
        return int(getattr(self, clase.value))

    def sumar(self, otro: "Recursos") -> "Recursos":
        return Recursos(*(self.peso(clase) + otro.peso(clase) for clase in ClaseRecurso))

    def restar(self, otro: "Recursos") -> "Recursos":
        return Recursos(*(max(0, self.peso(clase) - otro.peso(clase)) for clase in ClaseRecurso))

    def cabe_en(self, capacidad: "Recursos") -> bool:
        return all(self.peso(clase) <= capacidad.peso(clase) for clase in ClaseRecurso)

    def como_dict(self) -> dict[str, int]:
        return {clase.value: self.peso(clase) for clase in ClaseRecurso}

    @classmethod
    def desde_dict(cls, datos: Mapping[str, object]) -> "Recursos":
        """Decodifica un documento persistido *sin* rellenar huecos.

        Un peso ausente o ilegible no puede degradarse a cero: eso convertiria
        una fila corrupta en un job que no consume capacidad y elude los limites
        configurados, que es exactamente lo contrario de lo que debe pasar.
        """
        from .errors import ErrorRecursoJob

        if not isinstance(datos, Mapping):
            raise ErrorRecursoJob("El documento de recursos no es un objeto.")
        faltantes = [clase.value for clase in ClaseRecurso if clase.value not in datos]
        if faltantes:
            raise ErrorRecursoJob(f"El documento de recursos no declara {faltantes!r}.")
        return cls(**{clase.value: datos[clase.value] for clase in ClaseRecurso})


#: Politica inicial del plan: un trabajo pesado por clase, ligeros concurrentes.
LIMITES_POR_DEFECTO = Recursos(cpu=1, gpu=1, disk=1, network=2)


@dataclass(frozen=True)
class PoliticaRetry:
    """Reintento acotado y explicito.

    Un codigo no listado nunca se reintenta: el plan exige que los errores de
    entrada o de capacidad esperen intervencion en vez de consumir presupuesto.
    """

    max_intentos: int = 1
    codigos_transitorios: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        from .errors import ErrorJob

        if isinstance(self.max_intentos, bool) or not isinstance(self.max_intentos, int):
            raise ErrorJob("El maximo de intentos debe ser un entero.")
        if self.max_intentos < 1:
            raise ErrorJob("Un job admite al menos un intento.")
        object.__setattr__(self, "codigos_transitorios", frozenset(self.codigos_transitorios))

    def es_transitorio(self, codigo: str | None) -> bool:
        return codigo is not None and codigo in self.codigos_transitorios


@dataclass(frozen=True)
class PoliticaCancelacion:
    """Cooperativa primero; el reloj decide cuando se fuerza.

    `timeout_cooperativo` NO se cuenta desde que se manda la orden, sino desde
    que el worker esta en condiciones de oirla. Un proceso recien lanzado todavia
    esta importando modulos y aun no tiene escucha de control: contar desde el
    envio castiga al worker por la lentitud del sistema de archivos y lo mata sin
    haberle dado ocasion de cooperar.

    `timeout_arranque` cubre el otro lado del mismo problema: un hijo que jamas
    llega a estar listo no puede quedarse vivo para siempre a la espera de una
    gracia que nunca empezara a contar.
    """

    timeout_cooperativo: float = 5.0
    timeout_arranque: float = 60.0

    def __post_init__(self) -> None:
        import math

        from .errors import ErrorJob

        # `NaN` es el caso interesante: toda comparacion con el es falsa, de modo
        # que un timeout `NaN` no vence nunca y la terminacion forzada no llega
        # jamas. Un job cancelado se quedaria colgado para siempre sin que nada
        # pareciera roto.
        if isinstance(self.timeout_cooperativo, bool) or not isinstance(
                self.timeout_cooperativo, (int, float)):
            raise ErrorJob("El timeout de cancelacion debe ser un numero.")
        if not math.isfinite(self.timeout_cooperativo) or self.timeout_cooperativo < 0:
            raise ErrorJob("El timeout de cancelacion debe ser finito y no negativo.")
        if isinstance(self.timeout_arranque, bool) or not isinstance(
                self.timeout_arranque, (int, float)):
            raise ErrorJob("El timeout de arranque debe ser un numero.")
        if not math.isfinite(self.timeout_arranque) or self.timeout_arranque <= 0:
            raise ErrorJob("El timeout de arranque debe ser finito y positivo.")


@dataclass(frozen=True)
class StageDefinition:
    """Contrato de una etapa: que consume, que produce y como se la trata."""

    nombre: str
    version_contrato: str
    entradas: tuple[str, ...] = ()
    salidas: tuple[str, ...] = ()
    capacidad: str | None = None
    recursos: Recursos = Recursos(cpu=1)
    soporta_checkpoint: bool = False
    politica_retry: PoliticaRetry = PoliticaRetry()
    politica_cancelacion: PoliticaCancelacion = PoliticaCancelacion()

    def __post_init__(self) -> None:
        from .errors import ErrorJob

        if not self.nombre or not self.version_contrato:
            raise ErrorJob("Una etapa necesita nombre y version de contrato.")


@dataclass(frozen=True)
class ArchivoSalida:
    """Archivo declarado por el worker, relativo a su directorio temporal."""

    ruta: str
    bytes: int

    def __post_init__(self) -> None:
        from .errors import ErrorValidacionSalida

        # `True` pasaria por entero y declararia un byte: el tipo se comprueba
        # antes que el valor porque `bool` es subclase de `int`.
        if not isinstance(self.ruta, str) or not self.ruta:
            raise ErrorValidacionSalida("La salida declara un archivo sin ruta.")
        if isinstance(self.bytes, bool) or not isinstance(self.bytes, int) or self.bytes < 0:
            raise ErrorValidacionSalida("La salida declara un tamano que no es un entero valido.")


@dataclass(frozen=True)
class ManifiestoSalida:
    archivos: tuple[ArchivoSalida, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class Job:
    """Unidad durable de trabajo. Un fallo no la borra ni borra lo ya publicado."""

    id: str
    stage: str
    version_contrato: str
    estado: EstadoJob = EstadoJob.QUEUED
    clave_materializacion: str = ""
    # Vacio por defecto: el peso lo declara la `StageDefinition`, y `Recursos()`
    # significa "lo que diga el contrato". Un default de `cpu=1` actuaria como
    # piso adicional y haria que un job de GPU reservase ademas una CPU que su
    # etapa nunca pidio.
    recursos: Recursos = Recursos()
    dependencias: tuple[str, ...] = ()
    prioridad: int = 0
    intentos_usados: int = 0
    max_intentos: int = 1
    # Presupuesto propio: una interrupcion no es un fallo de la etapa. El
    # intento interrumpido devuelve su credito a `intentos_usados` y carga aqui,
    # de modo que un job de un unico intento sigue pudiendo reanudarse tras un
    # corte de luz en vez de quedar encolado para siempre sin poder arrancar.
    interrupciones_usadas: int = 0
    max_interrupciones: int = 3
    cancelacion_solicitada: bool = False
    checkpoint: Mapping[str, object] | None = None
    error_codigo: str | None = None
    diagnostico: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        from .errors import ErrorDependenciaJob, ErrorJob
        from .identity import exigir_componente_interno

        if not self.id or not self.stage:
            raise ErrorJob("Un job necesita identidad y etapa.")
        # El id termina siendo un tramo de `cache/jobs/<job>/` y de
        # `artifacts/<job>/`. Validarlo aqui hace irrepresentable un job cuya
        # sola existencia permitiria escribir —o borrar— fuera del proyecto.
        exigir_componente_interno(self.id, "identificador de trabajo")
        for campo in ("intentos_usados", "max_intentos", "interrupciones_usadas",
                      "max_interrupciones", "prioridad"):
            valor = getattr(self, campo)
            if isinstance(valor, bool) or not isinstance(valor, int):
                raise ErrorJob(f"El campo '{campo}' de un job debe ser un entero.")
        if self.intentos_usados < 0 or self.interrupciones_usadas < 0:
            raise ErrorJob("Los contadores de un job no pueden ser negativos.")
        if self.max_interrupciones < 0:
            raise ErrorJob("El maximo de interrupciones no puede ser negativo.")
        if self.max_intentos < 1:
            raise ErrorJob("Un job admite al menos un intento.")
        if self.id in self.dependencias:
            raise ErrorDependenciaJob("Un job no puede depender de si mismo.")
        if len(set(self.dependencias)) != len(self.dependencias):
            raise ErrorDependenciaJob("Las dependencias de un job no pueden repetirse.")

    @property
    def intentos_restantes(self) -> int:
        return max(0, self.max_intentos - self.intentos_usados)

    @property
    def reanudaciones_restantes(self) -> int:
        return max(0, self.max_interrupciones - self.interrupciones_usadas)

    def con_estado(self, destino: EstadoJob) -> "Job":
        return replace(self, estado=exigir_transicion(self.estado, destino))


@dataclass(frozen=True)
class JobAttempt:
    """Ejecucion concreta de un job, con su lease y su worker."""

    id: str
    job_id: str
    numero: int
    estado: EstadoJob = EstadoJob.RUNNING
    lease_owner: str | None = None
    lease_expira_en: float | None = None
    pid: int | None = None
    reanudado_de: str | None = None
    error_codigo: str | None = None
    diagnostico: str | None = None

    def lease_vigente(self, propietario: str, ahora: float) -> bool:
        return (self.lease_owner == propietario
                and self.lease_expira_en is not None
                and self.lease_expira_en > ahora)


class TipoEvento(str, Enum):
    """Vocabulario de eventos consultables. La UI traduce claves, no textos."""

    ENCOLADO = "encolado"
    INICIO = "inicio"
    PROGRESO = "progreso"
    CHECKPOINT = "checkpoint"
    RESULTADO = "resultado"
    VALIDACION = "validacion"
    PUBLICACION = "publicacion"
    ERROR = "error"
    CANCELACION = "cancelacion"
    INTERRUPCION = "interrupcion"
    FIN = "fin"


@dataclass(frozen=True)
class EventoProgreso:
    """Evento estructurado: identidades, unidades y clave localizable.

    `clave` es un identificador estable para traducir; `datos` transporta el
    diagnostico. Nunca se promete que `detalle` sea legible por una maquina.
    """

    job_id: str
    tipo: TipoEvento
    instante: float
    clave: str = ""
    attempt_id: str | None = None
    unidades_hechas: float | None = None
    unidades_totales: float | None = None
    datos: Mapping[str, object] = field(default_factory=dict)
    secuencia: int = 0

    @property
    def fraccion(self) -> float | None:
        """Fraccion conocida, o `None` cuando el total no se conoce."""
        if self.unidades_hechas is None or not self.unidades_totales:
            return None
        return max(0.0, min(1.0, self.unidades_hechas / self.unidades_totales))


class TipoMensaje(str, Enum):
    """Mensajes que un worker puede emitir. Cualquier otra cosa es protocolo roto."""

    INICIO = "inicio"
    PROGRESO = "progreso"
    CHECKPOINT = "checkpoint"
    RESULTADO = "resultado"
    ERROR = "error"
    CANCELADO = "cancelado"


@dataclass(frozen=True)
class MensajeWorker:
    tipo: TipoMensaje
    clave: str = ""
    unidades_hechas: float | None = None
    unidades_totales: float | None = None
    checkpoint: Mapping[str, object] | None = None
    manifiesto: ManifiestoSalida | None = None
    codigo: str | None = None
    detalle: str | None = None
    pid: int | None = None
