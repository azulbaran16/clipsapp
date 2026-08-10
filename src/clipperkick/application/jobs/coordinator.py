"""Coordinador durable de trabajos.

Sustituye a la hebra daemon efimera por un bucle explicito y sin hilos propios:
cada `paso()` despacha lo que cabe, drena los mensajes disponibles, vigila las
cancelaciones y renueva los leases. Que no haya concurrencia *dentro* del
coordinador es deliberado —el paralelismo real esta en los procesos hijos—:
asi el estado durable se muta desde un unico punto y una prueba puede avanzar el
mundo un paso a la vez.

Cuatro invariantes gobiernan el archivo:

1. **Nada corre sin lease.** Adquirirlo es la unica forma de pasar a `running`, y
   cada mutacion posterior lo vuelve a exigir; un coordinador que perdio la
   propiedad no puede cerrar el trabajo de otro.
2. **Nada se publica sin validar.** `succeeded` se escribe en la misma
   transaccion que los artefactos, y solo despues de que el almacen confirme que
   la salida declarada existe y mide lo que dice medir.
3. **El reintento es una excepcion nombrada.** Solo un codigo declarado
   transitorio por la etapa vuelve a la cola, y solo mientras quede presupuesto.
4. **Un fallo es local.** Un job que fracasa marca imposibles a sus descendientes
   y a nadie mas; los hermanos validos siguen su curso.
5. **La etapa gobierna al job.** Lo que se lee de disco se concilia con la
   `StageDefinition` vigente antes de planificar: version de contrato, techo de
   reintentos y peso de recursos salen del contrato, no de lo que un caller
   —o una sesion anterior— dejo escrito.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
import math
import os
import time
import uuid

from clipperkick.domain.jobs import (
    CODIGO_ARRANQUE, CODIGO_CANCELADO, CODIGO_CONTRATO, CODIGO_CRASH, CODIGO_DEPENDENCIA,
    CODIGO_DEPENDENCIA_INVALIDA, CODIGO_INTERRUMPIDO, CODIGO_PRESUPUESTO,
    CODIGO_LANZAMIENTO, CODIGO_PROTOCOLO, CODIGO_RECURSOS, CODIGO_SALIDA_INVALIDA, CODIGO_STAGE,
    CODIGO_TIMEOUT_CANCELACION, LIMITES_POR_DEFECTO, ErrorJob, ErrorLeaseJob, ErrorStageDesconocido,
    ErrorValidacionSalida, EstadoDependencias, EstadoJob, EventoProgreso, Job, JobAttempt,
    Recursos, StageDefinition, TipoEvento, TipoMensaje, clasificar_dependencias,
    conciliar_con_stage, contrato_compatible, excede_capacidad, exigir_dag, recursos_en_curso,
    seleccionar_ejecutables,
)
from clipperkick.domain.jobs.models import MensajeWorker

from .ports import (
    AlmacenArtefactos, EjecutorStage, HandleWorker, RepositorioJobs, Reloj, SolicitudEjecucion,
)


class RelojSistema:
    """Reloj de pared. Es el unico que sirve para lo que se le pide.

    Un lease se escribe en disco y se compara despues de cerrar y reabrir la
    aplicacion; un reloj monotono no significa nada fuera del proceso que lo
    leyo, de modo que un vencimiento persistido con `monotonic()` seria basura al
    reabrir. El riesgo asociado —un ajuste de hora— esta acotado: la propiedad
    del lease la decide la identidad del arrendatario, y el vencimiento solo es
    la segunda red.
    """

    def ahora(self) -> float:
        return time.time()

    def dormir(self, segundos: float) -> None:
        time.sleep(max(0.0, segundos))


def propietario_de_este_proceso() -> str:
    """Identidad de lease irrepetible entre reaperturas del mismo proyecto.

    Incluye el PID para diagnostico y un UUID porque un PID se recicla: si el
    proceso muere y otro hereda su numero, el heredero no debe parecer el duenio
    legitimo de un intento huerfano.
    """
    return f"pid:{os.getpid()}:{uuid.uuid4()}"


@dataclass
class _EnCurso:
    """Un intento vivo tal y como lo ve el coordinador."""

    job: Job
    attempt: JobAttempt
    stage: StageDefinition
    handle: HandleWorker
    directorio: str
    expira_en: float
    lanzado_en: float = 0.0
    #: Instante en que el worker confirmo `inicio`. Hasta entonces el proceso
    #: existe pero no escucha: no se le puede exigir que coopere.
    listo_en: float | None = None
    cancelacion_enviada: float | None = None
    forzado: bool = False
    cerrado: bool = False

    def gracia_desde(self) -> float | None:
        """Instante en que empieza a correr la gracia cooperativa, si empezo."""
        if self.cancelacion_enviada is None or self.listo_en is None:
            return None
        return max(self.cancelacion_enviada, self.listo_en)


@dataclass(frozen=True)
class ResumenCoordinador:
    jobs: tuple[Job, ...] = ()
    en_curso: tuple[str, ...] = ()
    recursos: Recursos = field(default_factory=Recursos)


class Coordinador:
    def __init__(self, repositorio: RepositorioJobs, ejecutor: EjecutorStage,
                 almacen: AlmacenArtefactos, catalogo: Mapping[str, StageDefinition], *,
                 limites: Recursos = LIMITES_POR_DEFECTO, propietario: str | None = None,
                 reloj: Reloj | None = None, duracion_lease: float = 30.0) -> None:
        self._repositorio = repositorio
        self._ejecutor = ejecutor
        self._almacen = almacen
        self._catalogo = dict(catalogo)
        self._limites = limites
        self._propietario = propietario or propietario_de_este_proceso()
        self._reloj = reloj or RelojSistema()
        # Un lease no positivo nace vencido: el propio coordinador se declararia
        # huerfano en el paso siguiente y ningun job avanzaria nunca.
        if isinstance(duracion_lease, bool) or not isinstance(duracion_lease, (int, float)):
            raise ErrorJob("La duracion del lease debe ser un numero.")
        if not math.isfinite(duracion_lease) or duracion_lease <= 0:
            raise ErrorJob("La duracion del lease debe ser finita y positiva.")
        self._duracion_lease = float(duracion_lease)
        self._en_curso: dict[str, _EnCurso] = {}

    # ------------------------------------------------------------------ #
    # API publica
    # ------------------------------------------------------------------ #

    @property
    def propietario(self) -> str:
        return self._propietario

    def encolar(self, job: Job) -> Job:
        """Registra un job validando el DAG completo antes de tocar nada.

        La validacion se hace contra el grafo *resultante*, no solo contra las
        aristas nuevas: un ciclo solo existe en relacion con lo ya persistido.
        """
        stage = self._catalogo.get(job.stage)
        if stage is None:
            raise ErrorStageDesconocido(f"Este coordinador no conoce la etapa {job.stage!r}.")
        # Conciliar *antes* de persistir: lo que quede en disco ya cumple el
        # contrato, y el planificador no tiene que corregirlo en cada paso.
        job = conciliar_con_stage(job, stage)
        existentes = self._repositorio.listar()
        dependencias = {otro.id: otro.dependencias for otro in existentes}
        dependencias[job.id] = job.dependencias
        exigir_dag(dependencias, tuple(dependencias))
        # El evento viaja *dentro* del comando: si se escribiese despues, un
        # fallo entre ambos dejaria un job encolado que su propio historial no
        # explica, y la vista observable contradiria al estado.
        registrado = self._repositorio.encolar(job, self._construir_evento(
            job.id, TipoEvento.ENCOLADO, clave="job.encolado",
            datos={"stage": job.stage, "dependencias": list(job.dependencias)}))
        return registrado

    def solicitar_cancelacion(self, job_id: str) -> bool:
        """Marca la intencion. El efecto lo aplica el siguiente `paso()`."""
        return self._repositorio.solicitar_cancelacion(job_id, self._construir_evento(
            job_id, TipoEvento.CANCELACION, clave="cancelacion.solicitada"))

    def recuperar(self) -> tuple[str, ...]:
        """Reclama los intentos huerfanos de sesiones anteriores.

        Solo tiene sentido con el lock de escritor del proyecto en la mano: si
        esta sesion escribe, ningun otro coordinador esta vivo sobre estos datos,
        de modo que todo intento activo que no sea nuestro quedo huerfano.
        """
        ahora = self._reloj.ahora()
        self._repositorio.recuperar_huerfanos(
            self._propietario, ahora,
            evento=lambda job_id, attempt_id: self._construir_evento(
                job_id, TipoEvento.INTERRUPCION, attempt_id=attempt_id,
                clave="intento.interrumpido", datos={"codigo": CODIGO_INTERRUMPIDO}))
        # Se recorre el *estado*, no solo lo que esta llamada acaba de cambiar.
        # Marcar el intento y decidir su desenlace son dos transacciones; si el
        # proceso muere entre ambas, el job queda `interrupted` y esta segunda
        # mitad tiene que poder retomarlo en la apertura siguiente. Recorrer el
        # estado hace la recuperacion idempotente y capaz de curarse sola.
        interrumpidos = tuple(job.id for job in self._repositorio.listar()
                              if job.estado is EstadoJob.INTERRUPTED)
        # La interrupcion en si ya quedo escrita junto a su cambio de estado.
        # Aqui solo se decide el desenlace, que viaja con su propio evento en la
        # transaccion que lo aplica.
        for job_id in interrumpidos:
            job = self._repositorio.obtener(job_id)
            datos = {"checkpoint": job.checkpoint is not None,
                     "reanudaciones_restantes": job.reanudaciones_restantes}
            if job.cancelacion_solicitada:
                self._repositorio.marcar_fallo_directo(
                    job_id, ahora, CODIGO_CANCELADO,
                    eventos=(self._construir_evento(job_id, TipoEvento.FIN, clave="job.cancelado",
                                                    datos={"codigo": CODIGO_CANCELADO, **datos}),))
            elif job.reanudaciones_restantes > 0:
                self._repositorio.reprogramar(job_id, ahora, eventos=(
                    self._construir_evento(job_id, TipoEvento.ENCOLADO, clave="job.reprogramado",
                                           datos={"motivo": CODIGO_INTERRUMPIDO, **datos}),))
            else:
                self._repositorio.marcar_fallo_directo(
                    job_id, ahora, CODIGO_INTERRUMPIDO,
                    eventos=(self._construir_evento(job_id, TipoEvento.FIN, clave="job.fallido",
                                                    datos={"codigo": CODIGO_INTERRUMPIDO,
                                                           **datos}),))
        return interrumpidos

    def paso(self) -> bool:
        """Avanza el mundo una vez. Devuelve si queda trabajo por hacer."""
        ahora = self._reloj.ahora()
        self._resolver_pendientes(self._conciliados(), ahora)
        self._despachar(ahora)
        self._bombear(self._reloj.ahora())
        self._vigilar_arranque(self._reloj.ahora())
        self._vigilar_cancelaciones(self._reloj.ahora())
        self._renovar(self._reloj.ahora())
        return bool(self._en_curso) or bool(self._pendientes())

    def ejecutar(self, limite_segundos: float | None = None, intervalo: float = 0.01) -> bool:
        """Bombea hasta vaciar la cola. Devuelve `False` si vencio el limite.

        Un limite invalido se rechaza *antes* del primer paso: con un limite
        negativo el bucle despacharia una tanda de trabajo y solo despues
        descubriria que ya habia vencido, lo que deja workers arrancados por una
        llamada que el llamador creia inofensiva.
        """
        if limite_segundos is not None:
            if isinstance(limite_segundos, bool) or not isinstance(limite_segundos, (int, float)):
                raise ErrorJob("El limite de ejecucion debe ser un numero.")
            if not math.isfinite(limite_segundos) or limite_segundos < 0:
                raise ErrorJob("El limite de ejecucion debe ser finito y no negativo.")
        limite = None if limite_segundos is None else self._reloj.ahora() + limite_segundos
        while self.paso():
            if limite is not None and self._reloj.ahora() >= limite:
                return False
            self._reloj.dormir(intervalo)
        return True

    def job(self, job_id: str) -> Job:
        """Estado durable de un job. Es la lectura que la UI necesita."""
        return self._repositorio.obtener(job_id)

    def jobs(self) -> tuple[Job, ...]:
        return self._repositorio.listar()

    def intentos(self, job_id: str) -> tuple[JobAttempt, ...]:
        return self._repositorio.intentos(job_id)

    def resumen(self) -> ResumenCoordinador:
        jobs = self._repositorio.listar()
        return ResumenCoordinador(jobs, tuple(sorted(self._en_curso)), recursos_en_curso(jobs))

    def eventos(self, job_id: str | None = None, desde: int = 0) -> tuple[EventoProgreso, ...]:
        return self._repositorio.eventos(job_id, desde)

    def cerrar(self) -> None:
        """Suelta los workers vivos sin declararlos terminados.

        No se marca nada `failed`: la sesion se va, el trabajo no fracaso. Los
        intentos quedan `running` con lease vencido y la proxima apertura los
        reconoce como huerfanos, que es exactamente el caso que el ticket exige.
        """
        for curso in list(self._en_curso.values()):
            try:
                curso.handle.terminar()
            finally:
                curso.handle.cerrar()
        self._en_curso.clear()

    def __enter__(self) -> "Coordinador":
        return self

    def __exit__(self, *_: object) -> None:
        self.cerrar()

    # ------------------------------------------------------------------ #
    # Fases del paso
    # ------------------------------------------------------------------ #

    def _pendientes(self) -> tuple[Job, ...]:
        return tuple(job for job in self._repositorio.listar() if job.estado is EstadoJob.QUEUED)

    def _conciliados(self) -> tuple[Job, ...]:
        """Los jobs tal y como su etapa admite ejecutarlos.

        Una fila puede venir de otra sesion, de una version anterior de la
        aplicacion o de una herramienta externa. Reconciliar aqui —y no solo al
        encolar— evita que el planificador reparta capacidad segun un peso que
        el contrato no reconoce. Los incompatibles se devuelven intactos: los
        cierra `_resolver_pendientes` con su propio codigo.
        """
        conciliados = []
        for job in self._repositorio.listar():
            stage = self._catalogo.get(job.stage)
            conciliados.append(conciliar_con_stage(job, stage)
                               if stage is not None and contrato_compatible(job, stage)
                               else job)
        return tuple(conciliados)

    def _resolver_pendientes(self, jobs: Sequence[Job], ahora: float) -> None:
        """Cierra lo que nunca podra correr, antes de repartir capacidad."""
        estados = {job.id: job.estado for job in jobs}
        invalidadas = self._repositorio.entradas_invalidadas()
        for job in jobs:
            if job.estado is not EstadoJob.QUEUED:
                continue
            stage = self._catalogo.get(job.stage)
            if job.cancelacion_solicitada:
                self._cerrar_sin_intento(job, ahora, CODIGO_CANCELADO, "job.cancelado")
            elif stage is None:
                self._cerrar_sin_intento(job, ahora, CODIGO_STAGE, "job.fallido")
            elif not contrato_compatible(job, stage):
                # Ni se lanza worker ni se publica nada: correr codigo nuevo
                # bajo una version fijada es justo lo que el versionado evita.
                self._cerrar_sin_intento(job, ahora, CODIGO_CONTRATO, "job.fallido")
            elif excede_capacidad(job, self._limites):
                self._cerrar_sin_intento(job, ahora, CODIGO_RECURSOS, "job.fallido")
            elif job.intentos_restantes <= 0:
                # Solo alcanzable con una fila manipulada: sin este cierre el job
                # quedaria encolado y sin poder adquirir lease jamas, girando el
                # bucle en vacio.
                self._cerrar_sin_intento(job, ahora, CODIGO_PRESUPUESTO, "job.fallido")
            elif any(dependencia in invalidadas for dependencia in job.dependencias):
                # Entrada `succeeded` cuyo artefacto ya no esta publicado. Correr
                # sobre ella produciria un resultado construido sobre nada, y
                # esperar en silencio dejaria al job encolado para siempre.
                self._cerrar_sin_intento(job, ahora, CODIGO_DEPENDENCIA_INVALIDA, "job.fallido")
            elif clasificar_dependencias(job, estados) is EstadoDependencias.IMPOSIBLES:
                self._cerrar_sin_intento(job, ahora, CODIGO_DEPENDENCIA, "job.fallido")

    def _cerrar_sin_intento(self, job: Job, ahora: float, codigo: str, clave: str) -> None:
        self._repositorio.marcar_fallo_directo(
            job.id, ahora, codigo,
            eventos=(self._construir_evento(job.id, TipoEvento.FIN, clave=clave,
                                            datos={"codigo": codigo}),))

    def _despachar(self, ahora: float) -> None:
        jobs = self._conciliados()
        invalidadas = self._repositorio.entradas_invalidadas()
        # Segunda red sobre `_resolver_pendientes`: aunque la reconciliacion
        # ocurra entre ambas fases, nada con una entrada rota llega a arrancar.
        elegibles = tuple(job for job in jobs
                          if not any(d in invalidadas for d in job.dependencias))
        elegidos = seleccionar_ejecutables(elegibles, self._limites, recursos_en_curso(jobs))
        for job_id in elegidos:
            self._arrancar(job_id, ahora)

    def _arrancar(self, job_id: str, ahora: float) -> None:
        stage = self._catalogo[self._repositorio.obtener(job_id).stage]
        # Se concilia *antes* de adquirir para que el techo de intentos que se
        # impone en la adquisicion sea el de la etapa y no el de la fila: una
        # base manipulada no puede comprarse un intento mas.
        job = conciliar_con_stage(self._repositorio.obtener(job_id), stage)
        checkpoint = job.checkpoint if stage.soporta_checkpoint else None
        try:
            # El evento describe exactamente lo que esta transaccion confirma: se
            # gano el lease y hay un intento preparado. Todavia no se ha lanzado
            # nada, asi que afirmar que el proceso "inicio" seria falso; ese otro
            # hecho lo registra `intento.iniciado` cuando el worker se presenta.
            attempt = self._repositorio.adquirir_lease(
                job_id, self._propietario, self._duracion_lease, ahora,
                max_intentos=job.max_intentos,
                evento=self._construir_evento(
                    job_id, TipoEvento.INICIO, clave="intento.preparado",
                    datos={"reanudado": checkpoint is not None, "stage": stage.nombre}))
        except ErrorLeaseJob:
            return  # El estado cambio bajo nuestros pies; el proximo paso reevalua.
        directorio = ""
        try:
            directorio = self._almacen.preparar(job_id, attempt.id)
            handle = self._ejecutor.lanzar(SolicitudEjecucion(
                job_id=job_id, attempt_id=attempt.id, stage=stage.nombre,
                version_contrato=stage.version_contrato, directorio=directorio,
                payload=job.payload, checkpoint=checkpoint))
        except Exception as error:  # el lanzamiento es frontera: nada puede escapar
            if directorio:
                self._almacen.descartar(directorio)
            try:
                # Mismo trato que los demas terminales: el estado y su historia
                # entran juntos o no entra ninguno.
                self._repositorio.finalizar(
                    attempt.id, self._propietario, EstadoJob.FAILED, ahora,
                    error_codigo=CODIGO_LANZAMIENTO, diagnostico=str(error),
                    eventos=(self._construir_evento(
                        job_id, TipoEvento.ERROR, attempt_id=attempt.id,
                        clave="worker.lanzamiento",
                        datos={"codigo": CODIGO_LANZAMIENTO, "detalle": str(error)}),
                        self._construir_evento(job_id, TipoEvento.FIN, attempt_id=attempt.id,
                                               clave="job.fallido",
                                               datos={"codigo": CODIGO_LANZAMIENTO})))
            except ErrorLeaseJob:
                return  # el intento ya no es nuestro: no se cierra trabajo ajeno
            self._considerar_reintento(job_id, stage, CODIGO_LANZAMIENTO, ahora)
            return
        self._en_curso[job_id] = _EnCurso(job=job, attempt=attempt, stage=stage, handle=handle,
                                          directorio=directorio, lanzado_en=ahora,
                                          expira_en=ahora + self._duracion_lease)

    def _bombear(self, ahora: float) -> None:
        for _job_id, curso in list(self._en_curso.items()):
            with self._propiedad(curso):
                for mensaje in curso.handle.mensajes():
                    self._aplicar(curso, mensaje, ahora)
                    if curso.cerrado:
                        break
                if not curso.cerrado and not curso.handle.vivo() and curso.handle.agotado():
                    self._cerrar_por_muerte(curso, ahora)

    def _vigilar_cancelaciones(self, ahora: float) -> None:
        for curso in list(self._en_curso.values()):
            job = self._repositorio.obtener(curso.job.id)
            if not job.cancelacion_solicitada or curso.cerrado:
                continue
            with self._propiedad(curso):
                self._aplicar_cancelacion(curso, job, ahora)

    def _vigilar_arranque(self, ahora: float) -> None:
        """Un hijo que nunca confirma `inicio` no puede quedarse vivo eternamente.

        Es la contrapartida de no contar la gracia cooperativa desde el
        lanzamiento: si esperar a que el worker este listo fuese incondicional,
        un proceso que jamas arranca —o que arranca y muere sin hablar— dejaria
        su job ocupando una clase de recurso sin final posible.
        """
        for curso in list(self._en_curso.values()):
            if curso.listo_en is not None or curso.cerrado:
                continue
            if ahora - curso.lanzado_en < curso.stage.politica_cancelacion.timeout_arranque:
                continue
            with self._propiedad(curso):
                curso.handle.terminar()
                curso.forzado = True
                self._fallar(curso, CODIGO_ARRANQUE,
                             "el worker no confirmo inicio en"
                             f" {curso.stage.politica_cancelacion.timeout_arranque}s", ahora)

    def _aplicar_cancelacion(self, curso: _EnCurso, job: Job, ahora: float) -> None:
        """Coopera primero; el reloj decide cuando deja de pedirlo por las buenas.

        La orden se escribe en cuanto se conoce —la tuberia la retiene y el hijo
        la leera en cuanto tenga escucha—, pero el cronometro de la gracia no
        arranca hasta que el worker confirma `inicio`. Sin esa distincion, un
        arranque mas lento que el propio timeout (un interprete sobre un sistema
        de archivos montado tarda facilmente el doble) hace que se mate al worker
        antes de que pudiera oir nada: una cancelacion perfectamente cooperativa
        se reporta entonces como forzada, y el diagnostico culpa a la etapa de
        algo que hizo el reloj.
        """
        if curso.cancelacion_enviada is None:
            # `cancel_requested` es una transicion observable: entra con su
            # evento o no entra.
            self._repositorio.marcar_estado(
                curso.attempt.id, self._propietario, EstadoJob.CANCEL_REQUESTED, ahora,
                eventos=(self._construir_evento(
                    job.id, TipoEvento.CANCELACION, attempt_id=curso.attempt.id,
                    clave="cancelacion.cooperativa",
                    datos={"timeout": curso.stage.politica_cancelacion.timeout_cooperativo,
                           "worker_listo": curso.listo_en is not None}),))
            curso.handle.solicitar_cancelacion()
            curso.cancelacion_enviada = ahora
            return
        desde = curso.gracia_desde()
        if desde is None or curso.forzado or not curso.handle.vivo():
            return  # todavia no escucha: de eso se ocupa el vigilante de arranque
        if ahora - desde >= curso.stage.politica_cancelacion.timeout_cooperativo:
            curso.handle.terminar()
            curso.forzado = True
            self._evento(job.id, TipoEvento.CANCELACION, attempt_id=curso.attempt.id,
                         clave="cancelacion.forzada", datos={"espera": ahora - desde})

    def _renovar(self, ahora: float) -> None:
        for curso in list(self._en_curso.values()):
            if ahora + self._duracion_lease / 2 < curso.expira_en:
                continue
            with self._propiedad(curso):
                self._repositorio.renovar_lease(curso.attempt.id, self._propietario,
                                                self._duracion_lease, ahora)
                curso.expira_en = ahora + self._duracion_lease

    # ------------------------------------------------------------------ #

    @contextmanager
    def _propiedad(self, curso: _EnCurso) -> Iterator[None]:
        """Ejecuta trabajo sobre un intento y abandona si el lease ya no es nuestro.

        Perder el lease no es un fallo del job: es la prueba de que otro manda
        sobre ese intento. Lo unico correcto entonces es callar —no escribir un
        estado terminal sobre trabajo ajeno— y retirar nuestro worker duplicado.
        Sin esto, un `ErrorLeaseJob` a mitad de `paso()` derribaria el bucle
        entero y con el a todos los demas jobs en curso.
        """
        try:
            yield
        except ErrorLeaseJob:
            self._abandonar(curso)

    def _abandonar(self, curso: _EnCurso) -> None:
        self._evento(curso.job.id, TipoEvento.INTERRUPCION, attempt_id=curso.attempt.id,
                     clave="lease.perdido")
        curso.cerrado = True
        self._en_curso.pop(curso.job.id, None)
        curso.handle.terminar()
        curso.handle.cerrar()

    # ------------------------------------------------------------------ #
    # Mensajes del worker
    # ------------------------------------------------------------------ #

    #: Mensajes que solo tienen sentido despues de que el worker se presente.
    _EXIGEN_INICIO = frozenset({TipoMensaje.PROGRESO, TipoMensaje.CHECKPOINT,
                                TipoMensaje.RESULTADO, TipoMensaje.CANCELADO})

    def _orden_valido(self, curso: _EnCurso, mensaje: MensajeWorker) -> str | None:
        """Comprueba el orden del protocolo. Devuelve el motivo si se rompio.

        Un `resultado` sin `inicio` previo describe un worker que este
        coordinador nunca vio arrancar: aceptarlo significaria publicar una
        salida sin saber siquiera que proceso la produjo. Un segundo `inicio`
        significa que dos emisores hablan por la misma tuberia.
        """
        if mensaje.tipo is TipoMensaje.INICIO and curso.listo_en is not None:
            return "inicio duplicado"
        if mensaje.tipo in self._EXIGEN_INICIO and curso.listo_en is None:
            return f"{mensaje.tipo.value} antes de inicio"
        return None

    def _aplicar(self, curso: _EnCurso, mensaje: MensajeWorker, ahora: float) -> None:
        motivo = self._orden_valido(curso, mensaje)
        if motivo is not None:
            self._fallar(curso, CODIGO_PROTOCOLO, f"protocolo fuera de orden: {motivo}", ahora)
            return
        if mensaje.tipo is TipoMensaje.INICIO:
            # A partir de aqui el hijo tiene su escucha de control en marcha: es
            # el unico instante desde el que tiene sentido exigirle que coopere.
            # Este si es el arranque real del proceso, y por eso es un evento
            # distinto del que confirmo la preparacion del intento.
            curso.listo_en = ahora
            self._repositorio.registrar_pid(curso.attempt.id, self._propietario, mensaje.pid, ahora)
            self._evento(curso.job.id, TipoEvento.INICIO, attempt_id=curso.attempt.id,
                         clave="intento.iniciado",
                         datos={"intento": curso.attempt.numero, "pid": mensaje.pid})
        elif mensaje.tipo is TipoMensaje.PROGRESO:
            self._evento(curso.job.id, TipoEvento.PROGRESO, attempt_id=curso.attempt.id,
                         clave=mensaje.clave or "stage.progreso",
                         unidades_hechas=mensaje.unidades_hechas,
                         unidades_totales=mensaje.unidades_totales)
        elif mensaje.tipo is TipoMensaje.CHECKPOINT:
            # El checkpoint decide desde donde reanuda la proxima apertura: su
            # rastro no puede quedar fuera de la transaccion que lo guarda.
            self._repositorio.registrar_checkpoint(
                curso.attempt.id, self._propietario, mensaje.checkpoint or {}, ahora,
                eventos=(self._construir_evento(
                    curso.job.id, TipoEvento.CHECKPOINT, attempt_id=curso.attempt.id,
                    clave=mensaje.clave or "stage.checkpoint",
                    datos=dict(mensaje.checkpoint or {})),))
        elif mensaje.tipo is TipoMensaje.RESULTADO:
            self._publicar(curso, mensaje, ahora)
        elif mensaje.tipo is TipoMensaje.ERROR:
            self._fallar(curso, mensaje.codigo or CODIGO_PROTOCOLO, mensaje.detalle, ahora)
        elif mensaje.tipo is TipoMensaje.CANCELADO:
            self._fallar(curso, CODIGO_CANCELADO, mensaje.detalle, ahora)

    def _publicar(self, curso: _EnCurso, mensaje: MensajeWorker, ahora: float) -> None:
        """Valida y publica antes de escribir `succeeded`. Nunca al reves."""
        if self._repositorio.obtener(curso.job.id).cancelacion_solicitada:
            # La cancelacion gana aunque el worker haya llegado a producir: el
            # resultado de un trabajo que la persona detuvo no debe publicarse.
            self._fallar(curso, CODIGO_CANCELADO, "resultado descartado por cancelacion", ahora)
            return
        self._repositorio.marcar_estado(
            curso.attempt.id, self._propietario, EstadoJob.VALIDATING, ahora,
            eventos=(self._construir_evento(curso.job.id, TipoEvento.VALIDACION,
                                            attempt_id=curso.attempt.id,
                                            clave="salida.validando"),))
        # El worker se recolecta *antes* de tocar su directorio. Mientras vive
        # puede conservar descriptores abiertos —y en Windows eso basta para que
        # mover o renombrar la carpeta falle—, de modo que validar y publicar
        # sobre un temporal todavia habitado seria una carrera con el hijo.
        self._recolectar(curso)
        manifiesto = mensaje.manifiesto
        if manifiesto is None:
            self._fallar(curso, CODIGO_SALIDA_INVALIDA, "resultado sin manifiesto", ahora)
            return
        try:
            self._almacen.validar(curso.directorio, manifiesto, salidas=curso.stage.salidas)
            publicados = self._almacen.publicar(
                curso.job.id, curso.directorio, manifiesto,
                attempt_id=curso.attempt.id, clave=curso.job.clave_materializacion,
                salidas=curso.stage.salidas, stage=curso.stage.nombre,
                version_contrato=curso.stage.version_contrato)
        except ErrorValidacionSalida as error:
            self._fallar(curso, CODIGO_SALIDA_INVALIDA, str(error), ahora)
            return
        except OSError as error:
            self._fallar(curso, CODIGO_SALIDA_INVALIDA, f"publicacion imposible: {error}", ahora)
            return
        artefactos = tuple((ruta, str(manifiesto.metadata.get("kind", curso.stage.nombre)))
                           for ruta in publicados)
        try:
            self._repositorio.finalizar(
                curso.attempt.id, self._propietario, EstadoJob.SUCCEEDED, ahora,
                artefactos=artefactos,
                eventos=(self._construir_evento(
                    curso.job.id, TipoEvento.PUBLICACION, attempt_id=curso.attempt.id,
                    clave="salida.publicada", datos={"archivos": list(publicados)}),
                    self._construir_evento(curso.job.id, TipoEvento.FIN,
                                           attempt_id=curso.attempt.id,
                                           clave="job.completado")))
        except ErrorJob as error:
            # Los bytes ya estan publicados y llevan su identidad; lo que falto
            # fue confirmarlo. No se marca `failed` —seria mentir sobre un
            # trabajo que existe— ni se borra lo publicado. El intento queda
            # `validating` y la proxima apertura lo recupera: su sucesor
            # encontrara la publicacion, la adoptara y confirmara entonces.
            self._evento(curso.job.id, TipoEvento.ERROR, attempt_id=curso.attempt.id,
                         clave="publicacion.sin_confirmar",
                         datos={"detalle": str(error), "archivos": list(publicados)})
            return

    def _cerrar_por_muerte(self, curso: _EnCurso, ahora: float) -> None:
        """El proceso se fue sin decir como. La causa la decide el contexto."""
        codigo = CODIGO_CRASH
        if curso.forzado and curso.cancelacion_enviada is not None:
            codigo = CODIGO_TIMEOUT_CANCELACION
        elif curso.forzado:
            codigo = CODIGO_ARRANQUE
        elif curso.cancelacion_enviada is not None:
            codigo = CODIGO_CANCELADO
        detalle = f"salida={curso.handle.codigo_salida()} {curso.handle.diagnostico()}".strip()
        self._fallar(curso, codigo, detalle, ahora)

    def _fallar(self, curso: _EnCurso, codigo: str, detalle: str | None, ahora: float) -> None:
        # Mismo orden que en la publicacion y por la misma razon: el temporal no
        # se toca mientras su duenio respire.
        self._recolectar(curso)
        cuarentena = self._almacen.cuarentena(curso.directorio)
        self._repositorio.finalizar(
            curso.attempt.id, self._propietario, EstadoJob.FAILED, ahora,
            error_codigo=codigo, diagnostico=detalle,
            eventos=(self._construir_evento(
                curso.job.id, TipoEvento.ERROR, attempt_id=curso.attempt.id,
                clave=f"job.{codigo}", datos={"codigo": codigo, "detalle": detalle or "",
                                              "cuarentena": cuarentena or ""}),
                self._construir_evento(
                    curso.job.id, TipoEvento.FIN, attempt_id=curso.attempt.id,
                    clave="job.cancelado" if codigo == CODIGO_CANCELADO else "job.fallido",
                    datos={"codigo": codigo})))
        self._considerar_reintento(curso.job.id, curso.stage, codigo, ahora)

    def _considerar_reintento(self, job_id: str, stage: StageDefinition, codigo: str,
                              ahora: float) -> None:
        if not stage.politica_retry.es_transitorio(codigo):
            return
        # El presupuesto se lee del job *conciliado*: un `max_intentos` guardado
        # por encima de la politica de la etapa no compra reintentos extra.
        job = conciliar_con_stage(self._repositorio.obtener(job_id), stage)
        if job.cancelacion_solicitada or job.intentos_restantes <= 0:
            return
        self._repositorio.reprogramar(job_id, ahora, eventos=(self._construir_evento(
            job_id, TipoEvento.ENCOLADO, clave="job.reintentado",
            datos={"codigo": codigo, "intentos_restantes": job.intentos_restantes}),))

    def _recolectar(self, curso: _EnCurso) -> None:
        """Retira el intento del bucle y recolecta su proceso. Idempotente.

        `cerrar()` concede una gracia antes de matar: en el camino feliz el
        worker ya termino y matarlo sin esperar dejaria un codigo de salida
        enganoso en el diagnostico.
        """
        if curso.cerrado:
            return
        curso.cerrado = True
        self._en_curso.pop(curso.job.id, None)
        curso.handle.cerrar()

    # ------------------------------------------------------------------ #

    def _construir_evento(self, job_id: str, tipo: TipoEvento, *, clave: str = "",
                          attempt_id: str | None = None, unidades_hechas: float | None = None,
                          unidades_totales: float | None = None,
                          datos: Mapping[str, object] | None = None) -> EventoProgreso:
        return EventoProgreso(
            job_id=job_id, tipo=tipo, instante=self._reloj.ahora(), clave=clave,
            attempt_id=attempt_id, unidades_hechas=unidades_hechas,
            unidades_totales=unidades_totales, datos=dict(datos or {}))

    def _evento(self, job_id: str, tipo: TipoEvento, *, clave: str = "",
                attempt_id: str | None = None, unidades_hechas: float | None = None,
                unidades_totales: float | None = None,
                datos: Mapping[str, object] | None = None) -> None:
        self._repositorio.registrar_evento(self._construir_evento(
            job_id, tipo, clave=clave, attempt_id=attempt_id, unidades_hechas=unidades_hechas,
            unidades_totales=unidades_totales, datos=datos))


def catalogo_por_nombre(definiciones: Iterable[StageDefinition]) -> dict[str, StageDefinition]:
    return {definicion.nombre: definicion for definicion in definiciones}


def jobs_por_estado(jobs: Sequence[Job]) -> dict[EstadoJob, tuple[str, ...]]:
    agrupado: dict[EstadoJob, list[str]] = {estado: [] for estado in EstadoJob}
    for job in jobs:
        agrupado[job.estado].append(job.id)
    return {estado: tuple(sorted(ids)) for estado, ids in agrupado.items()}
