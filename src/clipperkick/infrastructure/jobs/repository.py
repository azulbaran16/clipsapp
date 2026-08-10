"""Repositorio SQLite de jobs, intentos, dependencias y eventos (esquema v3).

Dos reglas explican casi todo el archivo:

1. **Toda mutacion de un intento lleva el lease en el `WHERE`.** No se lee el
   duenio para despues escribir: se escribe condicionando por duenio y vigencia,
   y un `rowcount` distinto de uno es `ErrorLeaseJob`. Asi no existe ventana
   entre comprobar la propiedad y ejercerla.
2. **La transicion se valida en el dominio antes de tocar SQL.** La tabla no
   sabe de maquinas de estado; que un `UPDATE` sea posible no significa que sea
   legitimo, y una transicion prohibida debe fallar igual aunque la escriba un
   adaptador nuevo.

Los instantes son de reloj de pared a proposito. Un lease debe seguir siendo
comparable despues de cerrar y reabrir la aplicacion, y un reloj monotono pierde
todo significado al cambiar de proceso.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
import json
import sqlite3
import uuid

from clipperkick.domain.jobs import (
    CODIGO_INTERRUMPIDO, ErrorJob, ErrorLeaseJob, ErrorTransicionJob, EstadoJob, EventoProgreso,
    Job, JobAttempt, Recursos, TipoEvento, clave_canonica, exigir_transicion,
)
from clipperkick.domain.project.errors import ErrorProyecto
from clipperkick.infrastructure.project.persistence import RepositorioSqliteProyecto


_CAMPOS_JOB = ("id", "stage", "contract_version", "state", "materialization_key", "resources_json",
               "priority", "attempts_used", "max_attempts", "interruptions_used",
               "max_interruptions", "cancel_requested", "checkpoint_json", "error_code",
               "diagnostics", "payload_json")
_SELECT_JOB = f"SELECT {', '.join(_CAMPOS_JOB)} FROM Job"

_CAMPOS_ATTEMPT = ("id", "job_id", "attempt_number", "state", "lease_owner", "lease_expires_at",
                   "worker_pid", "resumed_from", "error_code", "diagnostics")
_SELECT_ATTEMPT = f"SELECT {', '.join(_CAMPOS_ATTEMPT)} FROM JobAttempt"

#: Estados en los que un intento sigue reclamando un worker vivo.
_ESTADOS_VIVOS = (EstadoJob.RUNNING.value, EstadoJob.VALIDATING.value,
                  EstadoJob.CANCEL_REQUESTED.value)


def _json(valor: object) -> str:
    return json.dumps(valor, ensure_ascii=False, sort_keys=True)


def _documento(texto: str | None, campo: str) -> dict[str, object]:
    """Un documento ausente es vacio; uno presente y roto es un error.

    T02 fijo la regla que aqui se hereda: nada del disco es entrada confiable.
    Degradar un JSON invalido a `{}` haria que una fila corrupta pareciese un
    job legitimo sin recursos, sin payload y sin checkpoint.
    """
    if texto is None or texto == "":
        return {}
    try:
        datos = json.loads(texto)
    except ValueError as error:
        raise ErrorJob(f"El proyecto guarda un '{campo}' que no es JSON.") from error
    if not isinstance(datos, dict):
        raise ErrorJob(f"El proyecto guarda un '{campo}' que no es un objeto.")
    return datos


def _entero(valor: object, campo: str) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise ErrorJob(f"El proyecto guarda un '{campo}' que no es un entero.")
    return valor


def _entero_opcional(valor: object, campo: str) -> int | None:
    return None if valor is None else _entero(valor, campo)


def _real(valor: object, campo: str) -> float:
    """Un instante persistido tiene que ser un numero, no lo que haya en la celda.

    SQLite no tiene tipos rigidos: una columna `REAL` acepta texto sin quejarse.
    Convertir con `float()` a ciegas deja escapar un `ValueError` crudo desde una
    simple consulta de eventos, que es justo lo que las fronteras tipadas de
    T01/T02 prohiben.
    """
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise ErrorJob(f"El proyecto guarda un '{campo}' que no es un numero.")
    return float(valor)


def _real_opcional(valor: object, campo: str) -> float | None:
    return None if valor is None else _real(valor, campo)


def _texto_opcional(valor: object, campo: str) -> str | None:
    if valor is not None and not isinstance(valor, str):
        raise ErrorJob(f"El proyecto guarda un '{campo}' que no es texto.")
    return valor


def _exigir_historia(eventos: Sequence[EventoProgreso], comando: str) -> tuple[EventoProgreso, ...]:
    """Ninguna transicion observable se confirma sin su historia.

    La invariante vive aqui —en el repositorio— y no en el coordinador a
    proposito. Mientras fuese una convencion del llamador, bastaba con que un
    caller nuevo omitiese el argumento para reabrir el agujero: un `running` o un
    `interrupted` visibles que ninguna consulta puede explicar. Con la exigencia
    en el puerto, una llamada sin eventos ni siquiera llega a abrir transaccion.

    La comprobacion es doble por necesidad: la firma sin valor por defecto impide
    *olvidar* el argumento, pero no impide pasar una lista vacia, que es la misma
    omision escrita de otra forma.
    """
    materializados = tuple(eventos)
    if not materializados:
        raise ErrorJob(f"'{comando}' cambia el estado observable y exige al menos un evento.")
    for evento in materializados:
        if not isinstance(evento, EventoProgreso):
            raise ErrorJob(f"'{comando}' recibio una historia que no son eventos.")
    return materializados


def _ligar_a_job(historia: Sequence[EventoProgreso], job_id: str) -> tuple[EventoProgreso, ...]:
    """Comando de nivel job: la historia no cuelga de ningun intento.

    `attempt_id` se fuerza a `None` en vez de respetarse. Un `encolar` o un
    `reprogramar` no tocan ningun intento, asi que conservar el que trajera el
    llamador solo puede producir una afirmacion falsa —y, con la referencia
    relacional de v5, una que la base ya ni siquiera acepta.
    """
    return tuple(replace(evento, job_id=job_id, attempt_id=None) for evento in historia)


def _ligar_a_intento(historia: Sequence[EventoProgreso], job_id: str,
                     attempt_id: str) -> tuple[EventoProgreso, ...]:
    """Comando de nivel intento: el comando fija los dos extremos.

    Quien llama sabe *que* quiere contar; el comando sabe *de que* lo cuenta.
    Separar los dos ligados por nivel elimina el caso ambiguo que quedaba —un
    `attempt_id` suministrado que nadie reconciliaba— sin dejarlo a criterio de
    cada punto de uso.
    """
    return tuple(replace(evento, job_id=job_id, attempt_id=attempt_id) for evento in historia)


def _estado(valor: str) -> EstadoJob:
    try:
        return EstadoJob(valor)
    except ValueError as error:
        raise ErrorJob(f"El proyecto guarda un estado de job desconocido: {valor!r}.") from error


class RepositorioSqliteJobs:
    """Implementa `RepositorioJobs` sobre la conexion abierta por T02."""

    def __init__(self, proyecto: RepositorioSqliteProyecto) -> None:
        self._proyecto = proyecto

    @property
    def conexion(self) -> sqlite3.Connection:
        return self._proyecto.conexion

    # ------------------------------------------------------------------ #
    # Lectura
    # ------------------------------------------------------------------ #

    def _dependencias(self) -> dict[str, tuple[str, ...]]:
        agrupadas: dict[str, list[str]] = {}
        for job_id, depende_de in self._consultar(
                "SELECT job_id, depends_on FROM JobDependency ORDER BY job_id, depends_on"):
            agrupadas.setdefault(job_id, []).append(depende_de)
        return {job_id: tuple(aristas) for job_id, aristas in agrupadas.items()}

    def _consultar(self, sql: str, parametros: Sequence[object] = ()) -> list[tuple]:
        try:
            return list(self.conexion.execute(sql, tuple(parametros)))
        except sqlite3.Error as error:
            raise ErrorJob("No se pudo consultar el estado de los trabajos.") from error

    def _job(self, fila: tuple, dependencias: Mapping[str, tuple[str, ...]]) -> Job:
        try:
            recursos = Recursos.desde_dict(_documento(fila[5], "resources_json"))
        except ErrorJob:
            raise
        except Exception as error:  # ErrorRecursoJob y cualquier sorpresa del decoder
            raise ErrorJob(f"El proyecto guarda recursos ilegibles para {fila[0]!r}.") from error
        return Job(
            id=fila[0], stage=fila[1], version_contrato=fila[2], estado=_estado(fila[3]),
            clave_materializacion=fila[4], recursos=recursos,
            dependencias=dependencias.get(fila[0], ()), prioridad=_entero(fila[6], "priority"),
            intentos_usados=_entero(fila[7], "attempts_used"),
            max_intentos=_entero(fila[8], "max_attempts"),
            interrupciones_usadas=_entero(fila[9], "interruptions_used"),
            max_interrupciones=_entero(fila[10], "max_interruptions"),
            cancelacion_solicitada=bool(_entero(fila[11], "cancel_requested")),
            checkpoint=None if fila[12] is None else _documento(fila[12], "checkpoint_json"),
            error_codigo=fila[13], diagnostico=fila[14],
            payload=_documento(fila[15], "payload_json"))

    def listar(self) -> tuple[Job, ...]:
        dependencias = self._dependencias()
        return tuple(self._job(fila, dependencias)
                     for fila in self._consultar(f"{_SELECT_JOB} ORDER BY rowid"))

    def obtener(self, job_id: str) -> Job:
        filas = self._consultar(f"{_SELECT_JOB} WHERE id=?", (job_id,))
        if not filas:
            raise ErrorJob(f"No existe el trabajo {job_id!r}.")
        return self._job(filas[0], self._dependencias())

    def intentos(self, job_id: str) -> tuple[JobAttempt, ...]:
        return tuple(
            JobAttempt(id=fila[0], job_id=fila[1], numero=_entero(fila[2], "attempt_number"),
                       estado=_estado(fila[3]),
                       lease_owner=_texto_opcional(fila[4], "lease_owner"),
                       lease_expira_en=_real_opcional(fila[5], "lease_expires_at"),
                       pid=_entero_opcional(fila[6], "worker_pid"),
                       reanudado_de=_texto_opcional(fila[7], "resumed_from"),
                       error_codigo=_texto_opcional(fila[8], "error_code"),
                       diagnostico=_texto_opcional(fila[9], "diagnostics"))
            for fila in self._consultar(
                f"{_SELECT_ATTEMPT} WHERE job_id=? ORDER BY attempt_number, rowid", (job_id,)))

    # ------------------------------------------------------------------ #
    # Escritura
    # ------------------------------------------------------------------ #

    def _transaccion(self):
        return self._proyecto.transaccion()

    def encolar(self, job: Job, evento: EventoProgreso) -> Job:
        """Registra el job y su evento de encolado en una sola transaccion.

        Si el evento se escribiese aparte, un fallo entre ambos dejaria un job
        encolado del que la vista observable no tiene noticia: la UI mostraria
        una cola que su propio historial no explica.
        """
        if job.estado is not EstadoJob.QUEUED:
            raise ErrorJob("Un trabajo se registra encolado; los demas estados los produce el coordinador.")
        # El evento pertenece al job que esta transaccion crea: la identidad la
        # fija el comando, no el llamador. Aceptar un `job_id` ajeno solo podria
        # producir una historia colgada de otro trabajo.
        historia = _ligar_a_job(_exigir_historia((evento,), "encolar"), job.id)
        try:
            with self._transaccion() as tx:
                tx.execute(
                    "INSERT INTO Job (id, kind, state, payload_json, stage, contract_version,"
                    " materialization_key, priority, resources_json, attempts_used, max_attempts,"
                    " interruptions_used, max_interruptions, cancel_requested, canonical_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 0, ?, 0, ?)",
                    (job.id, job.stage, job.estado.value, _json(dict(job.payload)), job.stage,
                     job.version_contrato, job.clave_materializacion, job.prioridad,
                     _json(job.recursos.como_dict()), job.max_intentos, job.max_interrupciones,
                     clave_canonica(job.id)))
                tx.executemany("INSERT INTO JobDependency (job_id, depends_on) VALUES (?, ?)",
                               [(job.id, dependencia) for dependencia in job.dependencias])
                self._insertar_eventos(tx, historia)
        except ErrorProyecto as error:
            raise ErrorJob(f"No se pudo registrar el trabajo {job.id!r}.") from error
        return self.obtener(job.id)

    def adquirir_lease(self, job_id: str, propietario: str, duracion: float,
                       ahora: float, evento: EventoProgreso,
                       max_intentos: int | None = None) -> JobAttempt:
        """Pasa a `running` creando el intento, o falla sin dejar rastro.

        El `UPDATE` condiciona por el estado leido: si algo cambio entre la
        lectura y la escritura, no se arranca un worker sobre un job que ya no
        estaba disponible.

        El evento entra en esta misma transaccion. Es la primera transicion del
        job y la que mas caro sale dejar a medias: si `running` se confirmase por
        su cuenta y el evento fallase despues, quedaria un job en marcha —con su
        worker ya lanzado— del que la historia no dice nada. El llamador lo
        entrega sin `attempt_id` porque el intento nace aqui; se completa antes
        de escribirlo.
        """
        historia = _exigir_historia((evento,), "adquirir_lease")
        attempt_id = str(uuid.uuid4())
        try:
            with self._transaccion() as tx:
                fila = tx.execute("SELECT state, attempts_used, max_attempts, cancel_requested,"
                                  " checkpoint_json FROM Job WHERE id=?", (job_id,)).fetchone()
                if fila is None:
                    raise ErrorJob(f"No existe el trabajo {job_id!r}.")
                estado = _estado(fila[0])
                if estado is not EstadoJob.QUEUED:
                    raise ErrorLeaseJob(f"El trabajo {job_id!r} no esta encolado.")
                if fila[3]:
                    raise ErrorLeaseJob(f"El trabajo {job_id!r} tiene una cancelacion pendiente.")
                # El techo lo impone quien llama (el coordinador, con el valor ya
                # conciliado con la etapa). Confiar solo en `max_attempts` de la
                # fila permitiria que una base manipulada comprase intentos.
                persistido = _entero(fila[2], "max_attempts")
                usados = _entero(fila[1], "attempts_used")
                techo = persistido if max_intentos is None else min(persistido, max_intentos)
                if usados >= techo:
                    raise ErrorLeaseJob(f"El trabajo {job_id!r} agoto sus intentos.")
                # La numeracion no puede derivarse del presupuesto: un intento
                # interrumpido devuelve su credito, asi que `attempts_used` baja
                # y dos intentos distintos acabarian compartiendo numero.
                anterior = tx.execute(
                    "SELECT id, attempt_number FROM JobAttempt WHERE job_id=?"
                    " ORDER BY attempt_number DESC LIMIT 1", (job_id,)).fetchone()
                numero = 1 if anterior is None else _entero(anterior[1], "attempt_number") + 1
                tx.execute(
                    "INSERT INTO JobAttempt (id, job_id, state, attempt_number, lease_owner,"
                    " lease_expires_at, started_at, resumed_from, checkpoint_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (attempt_id, job_id, EstadoJob.RUNNING.value, numero, propietario,
                     ahora + duracion, ahora, None if anterior is None else anterior[0], fila[4]))
                cursor = tx.execute(
                    "UPDATE Job SET state=?, attempts_used=attempts_used+1, error_code=NULL,"
                    " diagnostics=NULL WHERE id=? AND state=?",
                    (EstadoJob.RUNNING.value, job_id, estado.value))
                if cursor.rowcount != 1:
                    raise ErrorLeaseJob(f"El trabajo {job_id!r} cambio de estado durante el arranque.")
                self._insertar_eventos(tx, _ligar_a_intento(historia, job_id, attempt_id))
        except ErrorProyecto as error:
            raise ErrorJob(f"No se pudo arrancar el trabajo {job_id!r}.") from error
        return JobAttempt(id=attempt_id, job_id=job_id, numero=numero, estado=EstadoJob.RUNNING,
                          lease_owner=propietario, lease_expira_en=ahora + duracion,
                          reanudado_de=None if anterior is None else anterior[0])

    # -- mutaciones que exigen el lease -------------------------------- #

    def _con_lease(self, tx: sqlite3.Connection, attempt_id: str, propietario: str, ahora: float,
                   sql: str, parametros: Sequence[object]) -> None:
        cursor = tx.execute(
            f"{sql} AND id=? AND lease_owner=? AND lease_expires_at IS NOT NULL"
            " AND lease_expires_at > ?",
            (*parametros, attempt_id, propietario, ahora))
        if cursor.rowcount != 1:
            raise ErrorLeaseJob(f"El intento {attempt_id!r} no pertenece a {propietario!r}"
                                " o su lease vencio.")

    def _estado_intento(self, tx: sqlite3.Connection, attempt_id: str) -> tuple[str, EstadoJob]:
        fila = tx.execute("SELECT job_id, state FROM JobAttempt WHERE id=?", (attempt_id,)).fetchone()
        if fila is None:
            raise ErrorJob(f"No existe el intento {attempt_id!r}.")
        return fila[0], _estado(fila[1])

    def renovar_lease(self, attempt_id: str, propietario: str, duracion: float,
                      ahora: float) -> None:
        with self._envuelto():
            with self._transaccion() as tx:
                self._con_lease(tx, attempt_id, propietario, ahora,
                                "UPDATE JobAttempt SET lease_expires_at=? WHERE 1=1",
                                (ahora + duracion,))

    def registrar_pid(self, attempt_id: str, propietario: str, pid: int | None,
                      ahora: float) -> None:
        with self._envuelto():
            with self._transaccion() as tx:
                self._con_lease(tx, attempt_id, propietario, ahora,
                                "UPDATE JobAttempt SET worker_pid=? WHERE 1=1", (pid,))

    def registrar_checkpoint(self, attempt_id: str, propietario: str,
                             checkpoint: Mapping[str, object], ahora: float,
                             eventos: Sequence[EventoProgreso] = ()) -> None:
        """El checkpoint se guarda en el intento *y* en el job.

        Reanudar es una decision del job, no del intento muerto: si solo viviese
        en el intento, la recuperacion tendria que salir a buscar cual fue el
        ultimo y confiar en ese orden.
        """
        documento = _json(dict(checkpoint))
        with self._envuelto():
            with self._transaccion() as tx:
                job_id, _ = self._estado_intento(tx, attempt_id)
                self._con_lease(tx, attempt_id, propietario, ahora,
                                "UPDATE JobAttempt SET checkpoint_json=? WHERE 1=1", (documento,))
                tx.execute("UPDATE Job SET checkpoint_json=? WHERE id=?", (documento, job_id))
                self._insertar_eventos(tx, _ligar_a_intento(eventos, job_id, attempt_id))

    def marcar_estado(self, attempt_id: str, propietario: str, destino: EstadoJob,
                      ahora: float, eventos: Sequence[EventoProgreso]) -> None:
        historia = _exigir_historia(eventos, "marcar_estado")
        with self._envuelto():
            with self._transaccion() as tx:
                job_id, actual = self._estado_intento(tx, attempt_id)
                exigir_transicion(actual, destino)
                self._con_lease(tx, attempt_id, propietario, ahora,
                                "UPDATE JobAttempt SET state=? WHERE 1=1", (destino.value,))
                tx.execute("UPDATE Job SET state=? WHERE id=?", (destino.value, job_id))
                self._insertar_eventos(tx, _ligar_a_intento(historia, job_id, attempt_id))

    def finalizar(self, attempt_id: str, propietario: str, destino: EstadoJob, ahora: float,
                  eventos: Sequence[EventoProgreso], error_codigo: str | None = None,
                  diagnostico: str | None = None,
                  artefactos: Sequence[tuple[str, str]] = ()) -> None:
        """Cierra el intento y publica sus artefactos en la misma transaccion.

        Si el registro del artefacto fallara despues de escribir `succeeded`, el
        proyecto afirmaria tener una salida que nadie puede encontrar. Van juntos
        o no va ninguno.
        """
        if destino not in (EstadoJob.SUCCEEDED, EstadoJob.FAILED, EstadoJob.INTERRUPTED):
            raise ErrorTransicionJob(f"{destino.value} no cierra un intento.")
        historia = _exigir_historia(eventos, "finalizar")
        with self._envuelto():
            with self._transaccion() as tx:
                job_id, actual = self._estado_intento(tx, attempt_id)
                exigir_transicion(actual, destino)
                self._con_lease(
                    tx, attempt_id, propietario, ahora,
                    "UPDATE JobAttempt SET state=?, finished_at=?, error_code=?, diagnostics=?,"
                    " lease_owner=NULL, lease_expires_at=NULL WHERE 1=1",
                    (destino.value, ahora, error_codigo, diagnostico))
                tx.execute("UPDATE Job SET state=?, error_code=?, diagnostics=? WHERE id=?",
                           (destino.value, error_codigo, diagnostico, job_id))
                for ruta, tipo in artefactos:
                    tx.execute("INSERT INTO AnalysisArtifact (id, source_id, relative_path, kind,"
                               " state, job_id) VALUES (?, NULL, ?, ?, 'available', ?)",
                               (str(uuid.uuid4()), ruta, tipo, job_id))
                self._insertar_eventos(tx, _ligar_a_intento(historia, job_id, attempt_id))

    # -- mutaciones de job sin intento vivo ----------------------------- #

    def solicitar_cancelacion(self, job_id: str, evento: EventoProgreso) -> bool:
        historia = _exigir_historia((evento,), "solicitar_cancelacion")
        with self._envuelto():
            with self._transaccion() as tx:
                cursor = tx.execute(
                    "UPDATE Job SET cancel_requested=1 WHERE id=? AND cancel_requested=0"
                    " AND state NOT IN (?, ?)",
                    (job_id, EstadoJob.SUCCEEDED.value, EstadoJob.FAILED.value))
                if cursor.rowcount != 1:
                    return False
                self._insertar_eventos(tx, _ligar_a_job(historia, job_id))
                return True

    def reprogramar(self, job_id: str, ahora: float,
                    eventos: Sequence[EventoProgreso]) -> bool:
        """Devuelve el job a la cola. Una interrupcion gasta su propio credito."""
        historia = _exigir_historia(eventos, "reprogramar")
        with self._envuelto():
            with self._transaccion() as tx:
                fila = tx.execute("SELECT state, interruptions_used FROM Job WHERE id=?",
                                  (job_id,)).fetchone()
                if fila is None:
                    raise ErrorJob(f"No existe el trabajo {job_id!r}.")
                actual = _estado(fila[0])
                exigir_transicion(actual, EstadoJob.QUEUED)
                interrupciones = (_entero(fila[1], "interruptions_used")
                                  + (1 if actual is EstadoJob.INTERRUPTED else 0))
                cursor = tx.execute(
                    "UPDATE Job SET state=?, error_code=NULL, diagnostics=NULL,"
                    " interruptions_used=? WHERE id=? AND state=?",
                    (EstadoJob.QUEUED.value, interrupciones, job_id, actual.value))
                if cursor.rowcount != 1:
                    return False
                self._insertar_eventos(tx, _ligar_a_job(historia, job_id))
                return True

    def marcar_fallo_directo(self, job_id: str, ahora: float, error_codigo: str,
                             eventos: Sequence[EventoProgreso],
                             diagnostico: str | None = None) -> None:
        historia = _exigir_historia(eventos, "marcar_fallo_directo")
        with self._envuelto():
            with self._transaccion() as tx:
                fila = tx.execute("SELECT state FROM Job WHERE id=?", (job_id,)).fetchone()
                if fila is None:
                    raise ErrorJob(f"No existe el trabajo {job_id!r}.")
                exigir_transicion(_estado(fila[0]), EstadoJob.FAILED)
                tx.execute("UPDATE Job SET state=?, error_code=?, diagnostics=? WHERE id=?",
                           (EstadoJob.FAILED.value, error_codigo, diagnostico, job_id))
                self._insertar_eventos(tx, _ligar_a_job(historia, job_id))

    def recuperar_huerfanos(
            self, propietario: str, ahora: float,
            evento: Callable[[str, str], EventoProgreso]) -> tuple[str, ...]:
        """Marca `interrupted` todo intento activo que no sea de esta sesion.

        El criterio principal es la identidad del arrendatario, no el reloj: un
        coordinador nuevo tiene un `propietario` que nunca existio antes, de modo
        que cualquier intento vivo con otro duenio quedo huerfano por definicion.
        El vencimiento se conserva como segunda red para un lease que se perdio
        dentro de la propia sesion.

        Aqui vive la *devolucion* del presupuesto de reintentos. Un intento se
        cobra a `attempts_used` al adquirir el lease porque en ese momento aun no
        se sabe como terminara; si termina interrumpido —un corte, no un fallo de
        la etapa— el cargo se deshace y el credito que se gasta es el de
        reanudaciones. Sin esta devolucion, un job con `max_intentos=1` quedaria
        encolado por la recuperacion y despues incapaz de adquirir lease jamas:
        la cola no avanzaria nunca y el bucle giraria en vacio.
        """
        afectados: list[str] = []
        with self._envuelto():
            with self._transaccion() as tx:
                filas = tx.execute(
                    f"SELECT id, job_id FROM JobAttempt WHERE state IN (?, ?, ?)"
                    " AND (lease_owner IS NULL OR lease_owner <> ?"
                    "      OR lease_expires_at IS NULL OR lease_expires_at <= ?)"
                    " ORDER BY rowid", (*_ESTADOS_VIVOS, propietario, ahora)).fetchall()
                for attempt_id, job_id in filas:
                    tx.execute(
                        "UPDATE JobAttempt SET state=?, finished_at=?, error_code=?,"
                        " lease_owner=NULL, lease_expires_at=NULL WHERE id=?",
                        (EstadoJob.INTERRUPTED.value, ahora, CODIGO_INTERRUMPIDO, attempt_id))
                    cursor = tx.execute(
                        "UPDATE Job SET state=?, error_code=?,"
                        " attempts_used=max(0, attempts_used-1)"
                        " WHERE id=? AND state IN (?, ?, ?)",
                        (EstadoJob.INTERRUPTED.value, CODIGO_INTERRUMPIDO, job_id, *_ESTADOS_VIVOS))
                    if cursor.rowcount == 1:
                        afectados.append(job_id)
                        # El evento acompana al cambio de estado dentro de la
                        # misma transaccion: `interrupted` no puede ser
                        # observable sin la historia que lo explica. Si la
                        # fabrica no entrega un evento util, la transaccion
                        # entera se deshace y el intento sigue activo.
                        # La pareja efectiva la pone la fila que se acaba de
                        # interrumpir, no la fabrica: si el llamador se equivoca
                        # de job, la interrupcion seguiria contandose bajo el
                        # trabajo que si cambio de estado.
                        self._insertar_eventos(tx, _ligar_a_intento(
                            _exigir_historia((evento(job_id, attempt_id),),
                                             "recuperar_huerfanos"),
                            job_id, attempt_id))
        return tuple(afectados)

    # ------------------------------------------------------------------ #
    # Eventos
    # ------------------------------------------------------------------ #

    def artefactos(self, job_id: str) -> tuple[tuple[str, str], ...]:
        """Artefactos publicados por un job, con su estado de reconciliacion."""
        return tuple((fila[0], fila[1]) for fila in self._consultar(
            "SELECT relative_path, state FROM AnalysisArtifact WHERE job_id=? ORDER BY rowid",
            (job_id,)))

    def entradas_invalidadas(self) -> frozenset[str]:
        """Jobs con alguna salida que ya no esta publicada.

        Se devuelve el conjunto *roto* y no el valido a proposito: una etapa
        puede terminar sin producir archivos, y preguntar por "los que tienen
        artefactos disponibles" dejaria fuera a esos jobs perfectamente sanos.
        Lo que invalida una entrada es que algo suyo falte, no que no tenga nada.
        """
        return frozenset(fila[0] for fila in self._consultar(
            "SELECT DISTINCT job_id FROM AnalysisArtifact"
            " WHERE job_id IS NOT NULL AND state <> 'available'"))

    def _insertar_eventos(self, tx, eventos: Sequence[EventoProgreso]) -> None:
        for evento in eventos:
            tx.execute(
                "INSERT INTO JobEvent (job_id, attempt_id, kind, at, message_key, units_done,"
                " units_total, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (evento.job_id, evento.attempt_id, evento.tipo.value, evento.instante,
                 evento.clave, evento.unidades_hechas, evento.unidades_totales,
                 _json(dict(evento.datos))))

    def registrar_evento(self, evento: EventoProgreso) -> EventoProgreso:
        with self._envuelto():
            with self._transaccion() as tx:
                cursor = tx.execute(
                    "INSERT INTO JobEvent (job_id, attempt_id, kind, at, message_key, units_done,"
                    " units_total, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (evento.job_id, evento.attempt_id, evento.tipo.value, evento.instante,
                     evento.clave, evento.unidades_hechas, evento.unidades_totales,
                     _json(dict(evento.datos))))
                secuencia = int(cursor.lastrowid)
        return EventoProgreso(job_id=evento.job_id, tipo=evento.tipo, instante=evento.instante,
                              clave=evento.clave, attempt_id=evento.attempt_id,
                              unidades_hechas=evento.unidades_hechas,
                              unidades_totales=evento.unidades_totales,
                              datos=dict(evento.datos), secuencia=secuencia)

    def eventos(self, job_id: str | None = None, desde: int = 0) -> tuple[EventoProgreso, ...]:
        sql = ("SELECT seq, job_id, attempt_id, kind, at, message_key, units_done, units_total,"
               " payload_json FROM JobEvent WHERE seq > ?")
        parametros: list[object] = [desde]
        if job_id is not None:
            sql += " AND job_id=?"
            parametros.append(job_id)
        return tuple(self._evento(fila) for fila in self._consultar(sql + " ORDER BY seq",
                                                                    parametros))

    def _evento(self, fila: tuple) -> EventoProgreso:
        try:
            tipo = TipoEvento(fila[3])
        except ValueError as error:
            raise ErrorJob(f"El proyecto guarda un evento de tipo desconocido: {fila[3]!r}.") from error
        return EventoProgreso(job_id=fila[1], tipo=tipo, instante=_real(fila[4], "at"),
                              clave=_texto_opcional(fila[5], "message_key") or "",
                              attempt_id=_texto_opcional(fila[2], "attempt_id"),
                              unidades_hechas=_real_opcional(fila[6], "units_done"),
                              unidades_totales=_real_opcional(fila[7], "units_total"),
                              datos=_documento(fila[8], "payload_json"),
                              secuencia=_entero(fila[0], "seq"))

    # ------------------------------------------------------------------ #

    def _envuelto(self):
        return _TraduccionDeErrores()


class _TraduccionDeErrores:
    """Convierte un fallo de proyecto en uno de jobs sin ocultar los propios."""

    def __enter__(self) -> "_TraduccionDeErrores":
        return self

    def __exit__(self, tipo, valor, _traza) -> bool:
        if tipo is not None and issubclass(tipo, ErrorProyecto):
            raise ErrorJob("La operacion sobre los trabajos no pudo confirmarse.") from valor
        return False
