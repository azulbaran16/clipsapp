"""Adaptador SQLite/filesystem para la carpeta portable .clipsapp.

Protocolo durable, en tres invariantes:

1. **El lock manda.** Nadie toca el proyecto sin poseer el lock de escritor.
   `abrir_proyecto` lo adquiere antes de recuperar o migrar; una instancia
   read-only jamas escribe, ni siquiera para reparar.
2. **Un pivote por publicacion.** `project.json` publica una creacion; el
   marcador de migracion declara una migracion en curso y se borra *antes* que
   sus respaldos. Cada corte de energia deja el proyecto en un estado que la
   siguiente apertura sabe reanudar.
3. **Nada del disco es entrada confiable.** El marcador no transporta rutas, los
   respaldos viven en nombres fijos y toda ruta interna pasa por `_ruta_interna`.

T04 anade el esquema v6: la fuente deja de guardar una huella opaca y pasa a
declarar origen, tamano, mtime y huella parcial junto a la completa, y el
artefacto pasa a declarar su clave de materializacion, su checksum y su linaje.
Ambas cosas se vuelven invariantes de esquema —dos fuentes no pueden compartir
identidad de contenido, y una clave no puede materializarse dos veces— porque
son las dos formas de que la reutilizacion mienta sin que nada parezca roto.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
import json
try:
    import msvcrt
except ImportError:  # POSIX CI y herramientas de desarrollo.
    msvcrt = None
    import fcntl
import os
from pathlib import Path
import shutil
import sqlite3
import uuid

from clipperkick.domain.jobs.errors import ErrorIdentificadorJob
from clipperkick.domain.jobs.identity import clave_canonica, exigir_componente_interno
from clipperkick.domain.project import InformacionProyecto, ResultadoReconciliacion
from clipperkick.domain.project.errors import ErrorFuenteProyecto, ErrorMigracionProyecto, ErrorProyecto


VERSION_ESQUEMA = 6
DIRECTORIOS_PROYECTO = ("sources", "artifacts", "cache", "presets", "outputs")
NOMBRE_MANIFIESTO = "project.json"
NOMBRE_BASE = "state.sqlite3"
NOMBRE_LOCK = ".clipsapp.lock"
NOMBRE_MARCADOR_MIGRACION = ".migration.pending.json"
NOMBRE_RESPALDO_BASE = NOMBRE_BASE + ".bak"
NOMBRE_RESPALDO_MANIFIESTO = NOMBRE_MANIFIESTO + ".bak"
# Todo lo que SQLite puede dejar junto a la base. `-journal` es el diario de
# rollback que existe antes de que WAL tome efecto: omitirlo dejaria que una
# imagen recien restaurada fuese revertida por el diario de la imagen anterior.
SUFIJOS_SIDECAR = ("-wal", "-shm", "-journal")
FORMATO_MARCADOR = "clipsapp-migration/1"
SUFIJO_TEMPORAL = ".tmp"
Replace = Callable[[str, str], None]


_DDL_V1 = (
    "CREATE TABLE Project (id TEXT PRIMARY KEY, name TEXT NOT NULL, config_json TEXT NOT NULL DEFAULT '{}')",
    """CREATE TABLE SourceAsset (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES Project(id), mode TEXT NOT NULL,
        relative_path TEXT, external_path TEXT, fingerprint TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'available'
    )""",
)

_DDL_V2 = (
    "CREATE TABLE AnalysisArtifact (id TEXT PRIMARY KEY, source_id TEXT REFERENCES SourceAsset(id), relative_path TEXT NOT NULL, kind TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'available')",
    "CREATE TABLE Moment (id TEXT PRIMARY KEY, source_id TEXT REFERENCES SourceAsset(id), start_seconds REAL NOT NULL, end_seconds REAL NOT NULL, score REAL)",
    # La revision actual es una relacion de pertenencia, no una referencia
    # suelta: la revision debe existir Y pertenecer al mismo Draft. Se expresa
    # con claves foraneas compuestas en vez de triggers porque la invariante
    # tiene mas lados mutables que el propio `current_revision_id` —mover una
    # Variant de Draft, renombrar un Draft, reasignar o borrar la revision
    # apuntada— y una FK los cubre todos, incluido el DELETE. Ademas es
    # declarativa: `PRAGMA foreign_key_check` detecta una violacion inyectada,
    # cosa que un trigger no puede hacer.
    """CREATE TABLE Draft (
        id TEXT PRIMARY KEY, moment_id TEXT REFERENCES Moment(id), current_revision_id TEXT,
        FOREIGN KEY (current_revision_id, id) REFERENCES EditRevision(id, draft_id)
    )""",
    """CREATE TABLE Variant (
        id TEXT PRIMARY KEY, draft_id TEXT NOT NULL REFERENCES Draft(id), name TEXT NOT NULL,
        current_revision_id TEXT,
        FOREIGN KEY (current_revision_id, draft_id) REFERENCES EditRevision(id, draft_id)
    )""",
    # `draft_id` es NOT NULL y (id, draft_id) es UNIQUE: sin ambas, una revision
    # huerfana no podria satisfacer ninguna pertenencia y la FK compuesta no
    # tendria indice padre al que apuntar.
    """CREATE TABLE EditRevision (
        id TEXT PRIMARY KEY, draft_id TEXT NOT NULL REFERENCES Draft(id), document_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (id, draft_id)
    )""",
    "CREATE TABLE Job (id TEXT PRIMARY KEY, kind TEXT NOT NULL, state TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}')",
    "CREATE TABLE JobAttempt (id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES Job(id), state TEXT NOT NULL, diagnostics TEXT)",
    "CREATE TABLE Output (id TEXT PRIMARY KEY, revision_id TEXT REFERENCES EditRevision(id), relative_path TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'published')",
    "CREATE TABLE CapabilityBinding (id TEXT PRIMARY KEY, capability TEXT NOT NULL, provider TEXT NOT NULL, version TEXT NOT NULL)",
)

# v3 convierte los marcadores `Job`/`JobAttempt` de v2 en el modelo real del
# coordinador. Se hace con `ALTER TABLE ADD COLUMN` y no reconstruyendo las
# tablas: reconstruir obligaria a renombrar un padre de clave foranea con
# `foreign_keys=ON`, que es precisamente la operacion que SQLite documenta como
# delicada, y no aporta nada —las columnas v2 siguen siendo utiles y las nuevas
# nacen con un default que describe con exactitud a un job recien encolado.
_ALTER_JOB = (
    "stage TEXT NOT NULL DEFAULT ''",
    "contract_version TEXT NOT NULL DEFAULT ''",
    "materialization_key TEXT NOT NULL DEFAULT ''",
    "priority INTEGER NOT NULL DEFAULT 0",
    """resources_json TEXT NOT NULL DEFAULT '{"cpu": 0, "disk": 0, "gpu": 0, "network": 0}'""",
    "attempts_used INTEGER NOT NULL DEFAULT 0",
    "max_attempts INTEGER NOT NULL DEFAULT 1",
    "interruptions_used INTEGER NOT NULL DEFAULT 0",
    "max_interruptions INTEGER NOT NULL DEFAULT 3",
    "cancel_requested INTEGER NOT NULL DEFAULT 0",
    "checkpoint_json TEXT",
    "error_code TEXT",
    "diagnostics TEXT",
    # Clave de colision de ruta: NFC + casefold del id. Windows y macOS no
    # distinguen `A` de `a` en el sistema de archivos, de modo que dos ids que
    # solo difieran en eso compartirian carpeta de staging y de artefactos. Se
    # persiste como columna con indice UNIQUE para que la colision sea
    # irrepresentable, y no una comprobacion que alguien pueda olvidar.
    "canonical_id TEXT NOT NULL DEFAULT ''",
)
_ALTER_ATTEMPT = (
    "attempt_number INTEGER NOT NULL DEFAULT 1",
    "lease_owner TEXT",
    "lease_expires_at REAL",
    "worker_pid INTEGER",
    "started_at REAL",
    "finished_at REAL",
    "error_code TEXT",
    "checkpoint_json TEXT",
    "resumed_from TEXT",
)

_DDL_V3 = (
    *(f"ALTER TABLE Job ADD COLUMN {columna}" for columna in _ALTER_JOB),
    *(f"ALTER TABLE JobAttempt ADD COLUMN {columna}" for columna in _ALTER_ATTEMPT),
    # Una fila v2 llega sin etapa. El default vacio la dejaria irrepresentable
    # para el modelo de dominio, asi que la migracion la completa con lo unico
    # que v2 sabia de ella: su `kind`. Sobre una base recien creada no afecta a
    # nada porque no hay filas.
    "UPDATE Job SET stage=kind WHERE stage=''",
    "UPDATE Job SET contract_version='0' WHERE contract_version=''",
    "UPDATE Job SET canonical_id=lower(id) WHERE canonical_id=''",
    # Un documento de recursos vacio ya no es legible: el decoder se niega a
    # inventar ceros porque eso convertiria una fila corrupta en un job que no
    # consume capacidad. La migracion escribe el documento completo.
    """UPDATE Job SET resources_json='{"cpu": 0, "disk": 0, "gpu": 0, "network": 0}'"""
    """ WHERE resources_json='{}' OR resources_json=''""",
    # La arista se modela como fila y no como lista JSON para que un ciclo o una
    # dependencia rota sea consultable con SQL y verificable con FKs.
    """CREATE TABLE JobDependency (
        job_id TEXT NOT NULL REFERENCES Job(id),
        depends_on TEXT NOT NULL REFERENCES Job(id),
        PRIMARY KEY (job_id, depends_on)
    )""",
    # El progreso es una tabla append-only: la UI consulta por `seq` y nunca
    # necesita interpretar texto humano para saber que paso.
    """CREATE TABLE JobEvent (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES Job(id),
        attempt_id TEXT,
        kind TEXT NOT NULL,
        at REAL NOT NULL,
        message_key TEXT NOT NULL DEFAULT '',
        units_done REAL,
        units_total REAL,
        payload_json TEXT NOT NULL DEFAULT '{}'
    )""",
    "CREATE UNIQUE INDEX Job_identidad_canonica ON Job (canonical_id)",
    # El artefacto sabe de que job salio: sin ese vinculo no hay forma de saber
    # si las entradas de un dependiente siguen publicadas y validas.
    "ALTER TABLE AnalysisArtifact ADD COLUMN job_id TEXT REFERENCES Job(id)",
    "CREATE INDEX AnalysisArtifact_por_job ON AnalysisArtifact (job_id)",
    "CREATE INDEX JobEvent_por_job ON JobEvent (job_id, seq)",
    "CREATE INDEX Job_por_estado ON Job (state)",
    "CREATE INDEX JobAttempt_por_job ON JobAttempt (job_id)",
)

def _canonicalizar_jobs(conexion: sqlite3.Connection) -> None:
    """Recalcula `canonical_id` con la misma regla que usa el dominio.

    v3 lo rellenaba con `lower()` de SQLite, que solo baja mayusculas ASCII.
    `Straße` y `STRASSE` sobrevivian como filas distintas y, sin embargo,
    comparten carpeta en cualquier sistema de archivos: la clave Python de ambas
    es `strasse`. Aqui se recalcula con `clave_canonica` —NFC + `casefold`— y una
    colision detiene la migracion en vez de confirmarla.
    """
    filas = conexion.execute("SELECT id FROM Job ORDER BY rowid").fetchall()
    vistos: dict[str, str] = {}
    for (identificador,) in filas:
        if not isinstance(identificador, str):
            raise ErrorMigracionProyecto("Un trabajo guardado no tiene identificador de texto.")
        # La misma gramatica que exige el dominio, y aplicada *antes* de tocar
        # nada. Canonicalizar un id que el dominio no sabe representar produce
        # una imagen v4 que abre pero cuya primera lectura de trabajos falla:
        # una sola fila heredada —un NFD, un `..`, un `CON`— dejaria el
        # repositorio entero inutilizable y sin vuelta atras.
        #
        # Migrar la PK y sus claves foraneas para renombrar esos ids es una
        # decision de producto que este corte no toma. Lo seguro es detenerse
        # con un diagnostico accionable y devolver el proyecto a como estaba.
        try:
            exigir_componente_interno(identificador, "identificador de trabajo")
        except ErrorIdentificadorJob as error:
            raise ErrorMigracionProyecto(
                f"El trabajo {identificador!r} tiene un identificador que esta version no puede"
                f" representar ({error}); el proyecto se conserva en su version anterior."
            ) from error
        clave = clave_canonica(identificador)
        anterior = vistos.get(clave)
        if anterior is not None:
            raise ErrorMigracionProyecto(
                f"Los trabajos {anterior!r} y {identificador!r} comparten identidad en disco"
                f" ({clave!r}); el proyecto no puede migrarse sin resolverlo.")
        vistos[clave] = identificador
    # El diagnostico se completa *antes* de escribir nada. Si se actualizase fila
    # a fila, la primera colision la detectaria el indice UNIQUE que v3 dejo
    # puesto y el error diria "UNIQUE constraint failed" en lugar de nombrar los
    # dos trabajos implicados, que es lo unico accionable para quien lo lea.
    conexion.execute("DROP INDEX IF EXISTS Job_identidad_canonica")
    for clave, identificador in vistos.items():
        conexion.execute("UPDATE Job SET canonical_id=? WHERE id=?", (clave, identificador))
    conexion.execute("CREATE UNIQUE INDEX Job_identidad_canonica ON Job (canonical_id)")


def _renumerar_intentos(conexion: sqlite3.Connection) -> None:
    """Da a cada intento un numero propio y estable dentro de su job.

    Las filas heredadas de v2 recibieron el default `attempt_number=1`, de modo
    que la historia de un job con varios intentos era indistinguible. Se
    renumera por orden cronologico —y por `rowid` cuando no hay instante, que es
    el orden de insercion— para que el resultado sea el mismo en cada ejecucion.
    """
    for (job_id,) in conexion.execute(
            "SELECT DISTINCT job_id FROM JobAttempt ORDER BY job_id").fetchall():
        intentos = conexion.execute(
            "SELECT id FROM JobAttempt WHERE job_id=?"
            " ORDER BY COALESCE(started_at, 0), rowid", (job_id,)).fetchall()
        for numero, (attempt_id,) in enumerate(intentos, start=1):
            conexion.execute("UPDATE JobAttempt SET attempt_number=? WHERE id=?",
                             (numero, attempt_id))


def _validar_historia_ligada(conexion: sqlite3.Connection) -> None:
    """Ningun evento heredado puede apuntar a un intento ajeno o inexistente.

    La reconstruccion de `JobEvent` con clave foranea compuesta rechazaria esas
    filas de todos modos, pero lo haria con un `FOREIGN KEY constraint failed`
    que no dice cuales son. Se comprueba antes para poder nombrarlas, y se aborta
    en vez de reasignarlas: una historia mal ligada es un dato que alguien tiene
    que mirar, no algo que esta capa deba adivinar.
    """
    incoherentes = conexion.execute(
        "SELECT e.seq, e.job_id, e.attempt_id FROM JobEvent e"
        " LEFT JOIN JobAttempt a ON a.id = e.attempt_id"
        " WHERE e.attempt_id IS NOT NULL AND (a.id IS NULL OR a.job_id <> e.job_id)"
        " ORDER BY e.seq LIMIT 5").fetchall()
    if incoherentes:
        detalle = ", ".join(f"seq={fila[0]} job={fila[1]!r} intento={fila[2]!r}"
                            for fila in incoherentes)
        raise ErrorMigracionProyecto(
            f"El historial guarda eventos ligados a intentos que no pertenecen a su trabajo"
            f" ({detalle}); el proyecto no puede migrarse sin resolverlo.")


#: `JobEvent` se reconstruye entera porque SQLite no sabe anadir una clave
#: foranea a una tabla existente. Se preserva `seq` explicitamente: es la
#: secuencia por la que la UI lee el historial, y regenerarla reescribiria el
#: orden de todo lo ya ocurrido.
_RECONSTRUIR_JOBEVENT = (
    """CREATE TABLE JobEvent_v5 (
        seq INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL REFERENCES Job(id),
        attempt_id TEXT,
        kind TEXT NOT NULL,
        at REAL NOT NULL,
        message_key TEXT NOT NULL DEFAULT '',
        units_done REAL,
        units_total REAL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        -- Compuesta a proposito: con `attempt_id` no nulo exige que el intento
        -- exista *y* pertenezca al mismo job. Con `attempt_id` NULL la
        -- restriccion se satisface sola, que es justo lo que necesita un evento
        -- de nivel job.
        FOREIGN KEY (attempt_id, job_id) REFERENCES JobAttempt(id, job_id)
    )""",
    """INSERT INTO JobEvent_v5 (seq, job_id, attempt_id, kind, at, message_key, units_done,
                              units_total, payload_json)
       SELECT seq, job_id, attempt_id, kind, at, message_key, units_done, units_total,
              payload_json FROM JobEvent ORDER BY seq""",
    "DROP TABLE JobEvent",
    "ALTER TABLE JobEvent_v5 RENAME TO JobEvent",
    "CREATE INDEX JobEvent_por_job ON JobEvent (job_id, seq)",
)

NOMBRE_SECUENCIA_EVENTOS = "JobEvent"


def _alto_de_eventos(conexion: sqlite3.Connection) -> int | None:
    """Lee el contador durable de `JobEvent`, o `None` si nunca hubo uno.

    `sqlite_sequence` guarda el mayor `seq` *jamas entregado*, no el mayor que
    hoy sigue en la tabla. La diferencia importa: si se borraron eventos, el
    contador queda por encima de `MAX(seq)` y es lo unico que impide volver a
    entregar un numero que alguien ya vio.
    """
    try:
        fila = conexion.execute("SELECT seq FROM sqlite_sequence WHERE name=?",
                                (NOMBRE_SECUENCIA_EVENTOS,)).fetchone()
    except sqlite3.Error:
        return None  # la tabla solo existe si algo con AUTOINCREMENT se creo antes
    if fila is None:
        return None
    alto = fila[0]
    if isinstance(alto, bool) or not isinstance(alto, int) or alto < 0:
        raise ErrorMigracionProyecto(
            f"El contador de eventos guarda un valor ilegible ({alto!r});"
            f" el proyecto no puede migrarse sin resolverlo.")
    maximo = conexion.execute("SELECT MAX(seq) FROM JobEvent").fetchone()[0]
    if maximo is not None and alto < maximo:
        raise ErrorMigracionProyecto(
            f"El contador de eventos ({alto}) es menor que el ultimo evento guardado"
            f" ({maximo}); el proyecto no puede migrarse sin resolverlo.")
    return alto


def _restaurar_alto_de_eventos(conexion: sqlite3.Connection, alto: int | None) -> None:
    """Devuelve el contador al valor exacto que tenia antes de reconstruir.

    Reconstruir la tabla lo reduce al mayor `seq` copiado —y a nada si la tabla
    quedo vacia—, de modo que sin esto una historia con huecos volveria a
    entregar numeros ya usados. `eventos(desde=...)` es un cursor: reutilizar una
    secuencia le haria saltarse eventos nuevos o repetir viejos.

    Que `alto` sea `None` es una respuesta legitima y no se corrige: significa
    que ese contador nunca existio, y en SQLite eso equivale exactamente a
    continuar desde el mayor `seq` presente. Inventar una fila no anadiria
    garantia ninguna.
    """
    if alto is None:
        return
    actualizado = conexion.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?",
                                   (alto, NOMBRE_SECUENCIA_EVENTOS))
    if actualizado.rowcount == 0:
        # La tabla se reconstruyo vacia: no hay fila que actualizar y hay que
        # reponer el contador entero, que es justo el caso en que perderlo
        # reemitiria la historia completa desde 1.
        conexion.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                         (NOMBRE_SECUENCIA_EVENTOS, alto))


def _reconstruir_jobevent(conexion: sqlite3.Connection) -> None:
    """Reconstruye `JobEvent` sin perder ni una identidad ya entregada.

    Captura y restauracion viven en el mismo paso —y por tanto en la misma
    transaccion que el resto de la migracion— para que un fallo entre ambos no
    pueda dejar la tabla nueva con un contador rebajado.
    """
    alto = _alto_de_eventos(conexion)
    _aplicar(conexion, _RECONSTRUIR_JOBEVENT)
    _restaurar_alto_de_eventos(conexion, alto)


_DDL_V5 = (
    _validar_historia_ligada,
    # El padre que la clave foranea compuesta necesita para poder existir.
    "CREATE UNIQUE INDEX JobAttempt_identidad ON JobAttempt (id, job_id)",
    _reconstruir_jobevent,
)

_DDL_V4 = (
    _canonicalizar_jobs,
    _renumerar_intentos,
    # La secuencia de intentos pasa a ser una invariante declarada, no una
    # convencion del codigo que la escribe.
    "CREATE UNIQUE INDEX JobAttempt_secuencia ON JobAttempt (job_id, attempt_number)",
)

# --------------------------------------------------------------------------- #
# v6: identidad de contenido de las fuentes y materializacion de los artefactos
# --------------------------------------------------------------------------- #

#: v1 dejo `SourceAsset` con una huella opaca y sin nada con que reconciliarla.
#: v6 anade lo que el plan exige de una fuente externa —ruta normalizada,
#: tamano, mtime y huella parcial— mas su procedencia y el enlace al asset al
#: que sustituye. Se hace con `ALTER TABLE ADD COLUMN` por la misma razon que en
#: v3: reconstruir la tabla obligaria a renombrar un padre de clave foranea con
#: `foreign_keys=ON`, y las columnas nuevas nacen con un default que describe con
#: exactitud a una fila heredada.
_ALTER_SOURCE = (
    # Entrada del mundo que produjo la fuente: la ruta local normalizada o la
    # URL. Es la clave con la que se detecta un reemplazo "mismo nombre, otros
    # bytes", que en modo copiado no se puede detectar por la ruta interna
    # —cada copia estrena nombre— y por tanto no tiene otra senal.
    "source_uri TEXT NOT NULL DEFAULT ''",
    "origin TEXT NOT NULL DEFAULT 'local'",
    "display_name TEXT NOT NULL DEFAULT ''",
    "size_bytes INTEGER",
    "mtime_ns INTEGER",
    "fingerprint_partial TEXT NOT NULL DEFAULT ''",
    "fingerprint_version TEXT NOT NULL DEFAULT ''",
    # Una fuente reemplazada no se borra: la nueva apunta a la vieja y la vieja
    # queda `replaced` con sus momentos, drafts y revisiones intactos.
    "supersedes TEXT REFERENCES SourceAsset(id)",
)

#: El artefacto pasa a declarar *por que* es reutilizable: de que clave salio,
#: que bytes tiene y quien lo produjo. Sin esas tres cosas, "reutilizar" solo
#: puede significar "existe un archivo con ese nombre".
_ALTER_ARTIFACT = (
    "materialization_key TEXT NOT NULL DEFAULT ''",
    # Ruta dentro del directorio del artefacto. `relative_path` sigue siendo la
    # ruta relativa al proyecto porque es la que reconcilia con el disco; esta
    # es la que el manifiesto declara y con la que se comparan los checksums.
    "artifact_path TEXT NOT NULL DEFAULT ''",
    "checksum TEXT NOT NULL DEFAULT ''",
    "bytes INTEGER",
    "stage TEXT NOT NULL DEFAULT ''",
    "contract_version TEXT NOT NULL DEFAULT ''",
    "provider_version TEXT NOT NULL DEFAULT ''",
    "lineage_json TEXT NOT NULL DEFAULT '{}'",
)


def _validar_identidad_de_fuentes(conexion: sqlite3.Connection) -> None:
    """Ninguna pareja de fuentes puede compartir identidad de contenido.

    El indice UNIQUE que v6 declara lo haria irrepresentable de todos modos,
    pero fallando con un `UNIQUE constraint failed` que no dice cuales son. Se
    comprueba antes para poder nombrarlas, y se aborta en vez de fusionarlas: dos
    filas con la misma huella pueden tener momentos y drafts distintos colgando,
    y decidir cual sobrevive no es una decision que una migracion deba tomar.
    """
    duplicadas = conexion.execute(
        "SELECT project_id, fingerprint, COUNT(*) FROM SourceAsset"
        " GROUP BY project_id, fingerprint HAVING COUNT(*) > 1 LIMIT 5").fetchall()
    if duplicadas:
        detalle = ", ".join(f"huella={fila[1]!r} veces={fila[2]}" for fila in duplicadas)
        raise ErrorMigracionProyecto(
            f"El proyecto guarda fuentes que comparten identidad de contenido ({detalle});"
            f" no puede migrarse sin resolverlo.")


_DDL_V6 = (
    _validar_identidad_de_fuentes,
    *(f"ALTER TABLE SourceAsset ADD COLUMN {columna}" for columna in _ALTER_SOURCE),
    *(f"ALTER TABLE AnalysisArtifact ADD COLUMN {columna}" for columna in _ALTER_ARTIFACT),
    # Una fila heredada trae una huella que esta version no sabe reproducir: no
    # se sabe con que algoritmo ni sobre que ventanas se calculo. Se marca como
    # tal en vez de copiarla a `fingerprint_partial`, que afirmaria que la
    # parcial y la completa son la misma cosa y haria que la reconciliacion
    # diese por buena una fuente que nadie ha vuelto a leer.
    "UPDATE SourceAsset SET fingerprint_version='legacy/0' WHERE fingerprint_version=''",
    "UPDATE SourceAsset SET source_uri=external_path"
    " WHERE source_uri='' AND external_path IS NOT NULL",
    # `artifact_path` de una fila heredada es el ultimo tramo de su ruta: es lo
    # unico cierto que se puede decir de ella sin inventarse un directorio de
    # artefacto que nunca existio.
    "UPDATE AnalysisArtifact SET artifact_path=replace(relative_path, '\\', '/')"
    " WHERE artifact_path=''",
    "CREATE UNIQUE INDEX SourceAsset_identidad ON SourceAsset (project_id, fingerprint)",
    # Parcial a proposito: las filas heredadas y las de T03 no tienen clave, y
    # exigirles unicidad sobre la cadena vacia haria que dos artefactos legitimos
    # de dos jobs distintos colisionasen.
    "CREATE UNIQUE INDEX AnalysisArtifact_materializacion"
    " ON AnalysisArtifact (materialization_key, artifact_path)"
    " WHERE materialization_key <> ''",
    "CREATE INDEX SourceAsset_por_origen ON SourceAsset (source_uri)",
    "CREATE INDEX AnalysisArtifact_por_fuente ON AnalysisArtifact (source_id)",
)

MIGRACIONES = {1: _DDL_V1, 2: _DDL_V2, 3: _DDL_V3, 4: _DDL_V4, 5: _DDL_V5, 6: _DDL_V6}
_TABLAS_V2 = frozenset({"Project", "SourceAsset", "AnalysisArtifact", "Moment", "Draft", "Variant",
                        "EditRevision", "Job", "JobAttempt", "Output", "CapabilityBinding"})
_TABLAS_V3 = _TABLAS_V2 | frozenset({"JobDependency", "JobEvent"})
TABLAS_REQUERIDAS = {
    1: frozenset({"Project", "SourceAsset"}),
    2: _TABLAS_V2,
    3: _TABLAS_V3,
    4: _TABLAS_V3,
    5: _TABLAS_V3,
    6: _TABLAS_V3,
}


def _nombre_columna(declaracion: str) -> str:
    return declaracion.split(maxsplit=1)[0]


# Las tablas de v3 no son nuevas, son ampliadas: comprobar solo su presencia
# aceptaria una base que declara v3 pero perdio las columnas del coordinador.
COLUMNAS_REQUERIDAS = {
    1: {},
    2: {},
    3: {"Job": frozenset(_nombre_columna(columna) for columna in _ALTER_JOB),
        "JobAttempt": frozenset(_nombre_columna(columna) for columna in _ALTER_ATTEMPT),
        "AnalysisArtifact": frozenset({"job_id"})},
}
COLUMNAS_REQUERIDAS[4] = COLUMNAS_REQUERIDAS[3]
COLUMNAS_REQUERIDAS[5] = COLUMNAS_REQUERIDAS[3]
# v6 amplia dos tablas que ya existian. Igual que en v3, comprobar solo su
# presencia aceptaria una base que declara v6 y perdio las columnas con las que
# se reconcilia una fuente o se reutiliza un artefacto.
COLUMNAS_REQUERIDAS[6] = {
    **COLUMNAS_REQUERIDAS[3],
    "SourceAsset": frozenset(_nombre_columna(columna) for columna in _ALTER_SOURCE),
    "AnalysisArtifact": (COLUMNAS_REQUERIDAS[3]["AnalysisArtifact"]
                         | frozenset(_nombre_columna(columna) for columna in _ALTER_ARTIFACT)),
}

# Los indices UNIQUE son invariantes, no optimizaciones: sin ellos dos jobs
# pueden compartir carpeta y dos intentos pueden compartir numero. Se validan
# como el esquema valida sus claves foraneas, por columnas y no por nombre.
INDICES_UNICOS_REQUERIDOS = {
    1: frozenset(),
    2: frozenset(),
    3: frozenset({("Job", ("canonical_id",))}),
    4: frozenset({("Job", ("canonical_id",)), ("JobAttempt", ("job_id", "attempt_number"))}),
    # v5 anade el indice padre que la clave foranea compuesta de `JobEvent`
    # necesita para poder declararse.
    5: frozenset({("Job", ("canonical_id",)), ("JobAttempt", ("job_id", "attempt_number")),
                  ("JobAttempt", ("id", "job_id"))}),
    # v6 hace irrepresentables las dos formas de que la reutilizacion mienta:
    # dos fuentes con la misma identidad de contenido —los artefactos de una
    # acabarian atribuidos a la otra— y una misma clave de materializacion
    # publicada dos veces.
    6: frozenset({("Job", ("canonical_id",)), ("JobAttempt", ("job_id", "attempt_number")),
                  ("JobAttempt", ("id", "job_id")),
                  ("SourceAsset", ("project_id", "fingerprint")),
                  ("AnalysisArtifact", ("materialization_key", "artifact_path"))}),
}
# La invariante de pertenencia se valida como esquema, no solo como conducta: una
# base que perdio estas FKs acepta estados que el ticket prohibe, asi que no se
# devuelve un repositorio sobre ella.
_FKS_V2 = frozenset({
    ("Draft", "EditRevision", (("current_revision_id", "id"), ("id", "draft_id"))),
    ("Variant", "EditRevision", (("current_revision_id", "id"), ("draft_id", "draft_id"))),
})
FKS_REQUERIDAS = {
    1: frozenset(),
    2: _FKS_V2,
    3: _FKS_V2 | frozenset({
        ("AnalysisArtifact", "Job", (("job_id", "id"),)),
        ("JobAttempt", "Job", (("job_id", "id"),)),
        ("JobDependency", "Job", (("job_id", "id"),)),
        ("JobDependency", "Job", (("depends_on", "id"),)),
        ("JobEvent", "Job", (("job_id", "id"),)),
    }),
}
FKS_REQUERIDAS[4] = FKS_REQUERIDAS[3]
# v5 hace irrepresentable un evento colgado de un intento ajeno: la clave es
# compuesta, de modo que un `attempt_id` no nulo obliga a que ese intento exista
# *y* pertenezca al mismo trabajo. Con `attempt_id` NULL la restriccion se
# satisface sola, que es lo que necesita un evento de nivel job.
FKS_REQUERIDAS[5] = FKS_REQUERIDAS[3] | frozenset({
    ("JobEvent", "JobAttempt", (("attempt_id", "id"), ("job_id", "job_id"))),
})
# v6 exige ademas las dos pertenencias sobre las que se apoya la ingesta: una
# fuente pertenece a su proyecto y un artefacto de analisis a su fuente. Ambas
# se declaran desde v1/v2, pero hasta ahora nadie dependia de ellas; a partir de
# aqui una base que las perdio acepta artefactos huerfanos y fuentes de nadie.
FKS_REQUERIDAS[6] = FKS_REQUERIDAS[5] | frozenset({
    ("SourceAsset", "Project", (("project_id", "id"),)),
    ("AnalysisArtifact", "SourceAsset", (("source_id", "id"),)),
})


def _aplicar(conexion: sqlite3.Connection, sentencias: tuple) -> None:
    """Ejecuta el DDL sentencia por sentencia.

    `executescript` haria un COMMIT implicito antes del script y dejaria cada
    `CREATE TABLE` confirmado por su cuenta: un corte a mitad de migracion
    sobreviviria como DDL parcial. Con `execute` el lote entero vive dentro de la
    transaccion que abrio el llamador y un rollback lo deshace por completo.
    """
    for sentencia in sentencias:
        # Un paso de migracion puede necesitar decidir en Python —canonicalizar
        # con las reglas Unicode del dominio, renumerar por orden— y no solo
        # ejecutar DDL. Corre dentro de la misma transaccion que el resto.
        if callable(sentencia):
            sentencia(conexion)
        else:
            conexion.execute(sentencia)


def _sql_v1(conexion: sqlite3.Connection) -> None:
    _aplicar(conexion, _DDL_V1)


def _sql_v2(conexion: sqlite3.Connection) -> None:
    _aplicar(conexion, _DDL_V2)


def _sql_v3(conexion: sqlite3.Connection) -> None:
    _aplicar(conexion, _DDL_V3)


def _sql_v4(conexion: sqlite3.Connection) -> None:
    _aplicar(conexion, _DDL_V4)


def _sql_v5(conexion: sqlite3.Connection) -> None:
    _aplicar(conexion, _DDL_V5)


def _sql_v6(conexion: sqlite3.Connection) -> None:
    _aplicar(conexion, _DDL_V6)


def _conexion(ruta: Path, solo_lectura: bool = False) -> sqlite3.Connection:
    modo = "ro" if solo_lectura else "rw"
    conexion = sqlite3.connect(f"file:{ruta.as_posix()}?mode={modo}", uri=True)
    try:
        if not solo_lectura:
            conexion.execute("PRAGMA journal_mode=WAL")
        conexion.execute("PRAGMA foreign_keys=ON")
        return conexion
    except BaseException:
        conexion.close()
        raise


def _ruta_interna(raiz: Path, relativa: str) -> Path:
    candidata = Path(relativa)
    if candidata.is_absolute() or ".." in candidata.parts:
        raise ErrorProyecto("La ruta interna del proyecto no es valida.")
    resuelta = (raiz / candidata).resolve()
    if raiz.resolve() not in resuelta.parents and resuelta != raiz.resolve():
        raise ErrorProyecto("La ruta interna escapa del proyecto.")
    return resuelta


def _fsync_directorio(ruta: Path) -> None:
    """Confirma la entrada de directorio; en Windows no es abrible ni necesario."""
    if os.name == "nt":
        return
    try:
        descriptor = os.open(str(ruta), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _temporal_de(destino: Path) -> Path:
    return destino.with_name(destino.name + SUFIJO_TEMPORAL)


def _escribir_bytes_durable(destino: Path, datos: bytes, replace: Replace = os.replace) -> None:
    temporal = _temporal_de(destino)
    try:
        with temporal.open("wb") as archivo:
            archivo.write(datos)
            archivo.flush()
            os.fsync(archivo.fileno())
        replace(str(temporal), str(destino))
        _fsync_directorio(destino.parent)
    except BaseException:
        temporal.unlink(missing_ok=True)
        raise


def _copiar_durable(origen: Path, destino: Path) -> None:
    temporal = _temporal_de(destino)
    try:
        shutil.copyfile(origen, temporal)
        with temporal.open("rb+") as archivo:
            os.fsync(archivo.fileno())
        os.replace(temporal, destino)
        _fsync_directorio(destino.parent)
    except BaseException:
        temporal.unlink(missing_ok=True)
        raise


def _escribir_manifest(ruta: Path, datos: dict[str, object], replace: Replace = os.replace) -> None:
    texto = json.dumps(datos, ensure_ascii=False, indent=2) + "\n"
    _escribir_bytes_durable(ruta, texto.encode("utf-8"), replace)


def _leer_manifest(ruta: Path) -> dict[str, object]:
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
        if not isinstance(datos, dict):
            raise ValueError("el manifiesto no es un objeto")
        if not isinstance(datos.get("project_id"), str) or not isinstance(datos.get("schema_version"), int):
            raise ValueError("campos obligatorios ausentes")
        return datos
    except (OSError, ValueError, TypeError) as error:
        raise ErrorProyecto("El manifiesto del proyecto no es legible.") from error


# --------------------------------------------------------------------------- #
# Lock de escritor
# --------------------------------------------------------------------------- #

if msvcrt is not None:
    def _bloquear(archivo) -> None:
        archivo.seek(0)
        msvcrt.locking(archivo.fileno(), msvcrt.LK_NBLCK, 1)

    def _desbloquear(archivo) -> None:
        archivo.seek(0)
        msvcrt.locking(archivo.fileno(), msvcrt.LK_UNLCK, 1)
else:
    def _bloquear(archivo) -> None:
        fcntl.flock(archivo.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _desbloquear(archivo) -> None:
        fcntl.flock(archivo.fileno(), fcntl.LOCK_UN)


class _LockEscritor:
    """Lock de escritor por proyecto, sostenido por el sistema operativo.

    El archivo **nunca se desvincula**. En POSIX `flock` protege un inodo, no una
    ruta: borrarlo al salir permite que un proceso entrante quede bloqueando un
    inodo ya desvinculado mientras un tercero crea otro archivo en la misma ruta
    y tambien "gana". Dejarlo en su sitio elimina esa carrera por completo, y no
    cuesta nada: un lock queda liberado por el sistema operativo al morir su
    proceso, de modo que un archivo remanente jamas impide una reapertura.
    """

    def __init__(self, ruta: Path, archivo) -> None:
        self.ruta, self.archivo = ruta, archivo

    def close(self) -> None:
        archivo, self.archivo = self.archivo, None
        if archivo is None:
            return
        try:
            _desbloquear(archivo)
        except OSError:
            pass
        finally:
            archivo.close()


def _adquirir_lock(raiz: Path) -> _LockEscritor | None:
    """Devuelve el lock, o `None` si otra instancia ya escribe este proyecto."""
    ruta = raiz / NOMBRE_LOCK
    try:
        archivo = ruta.open("a+", encoding="utf-8")
    except OSError:
        return None
    try:
        if os.fstat(archivo.fileno()).st_size == 0:
            archivo.write(" ")
            archivo.flush()
        _bloquear(archivo)
    except OSError:
        archivo.close()
        return None
    except BaseException:
        archivo.close()
        raise
    return _LockEscritor(ruta, archivo)


# --------------------------------------------------------------------------- #
# Migracion y recuperacion
# --------------------------------------------------------------------------- #

def _neutralizar_sidecars(base: Path) -> None:
    """Elimina `-wal`/`-shm`: pertenecen a la imagen que se esta descartando."""
    for sufijo in SUFIJOS_SIDECAR:
        base.with_name(base.name + sufijo).unlink(missing_ok=True)


def _limpiar_respaldos(ruta: Path) -> None:
    for nombre in (NOMBRE_RESPALDO_BASE, NOMBRE_RESPALDO_MANIFIESTO):
        (ruta / nombre).unlink(missing_ok=True)
        _temporal_de(ruta / nombre).unlink(missing_ok=True)


def _respaldar(ruta: Path) -> None:
    """Copia coherente de base y manifiesto, con los commits que viven en el WAL.

    `Connection.backup` lee la base a traves de SQLite, de modo que incluye lo
    confirmado en el WAL; una copia suelta del archivo principal no lo haria.
    """
    base = ruta / NOMBRE_BASE
    respaldo_base = ruta / NOMBRE_RESPALDO_BASE
    temporal = _temporal_de(respaldo_base)
    temporal.unlink(missing_ok=True)
    try:
        origen = _conexion(base)
        try:
            destino = sqlite3.connect(temporal)
            try:
                origen.backup(destino)
            finally:
                destino.close()
        finally:
            origen.close()
        _neutralizar_sidecars(temporal)
        with temporal.open("rb+") as archivo:
            os.fsync(archivo.fileno())
        os.replace(temporal, respaldo_base)
        _fsync_directorio(ruta)
    except BaseException:
        temporal.unlink(missing_ok=True)
        respaldo_base.unlink(missing_ok=True)
        raise
    _copiar_durable(ruta / NOMBRE_MANIFIESTO, ruta / NOMBRE_RESPALDO_MANIFIESTO)


def _restaurar(ruta: Path) -> None:
    """Instala la imagen respaldada. Idempotente y reanudable en cada corte.

    El orden es el contrato: sidecars fuera antes de tocar la base (SQLite
    reaplicaria el WAL huerfano sobre la imagen restaurada), base, manifiesto,
    marcador —el pivote— y solo entonces los respaldos. Un corte antes del
    pivote deja el marcador puesto y la siguiente apertura repite el mismo
    trabajo desde el principio.
    """
    base = ruta / NOMBRE_BASE
    _neutralizar_sidecars(base)
    _copiar_durable(ruta / NOMBRE_RESPALDO_BASE, base)
    _neutralizar_sidecars(base)
    _copiar_durable(ruta / NOMBRE_RESPALDO_MANIFIESTO, ruta / NOMBRE_MANIFIESTO)
    (ruta / NOMBRE_MARCADOR_MIGRACION).unlink(missing_ok=True)
    _fsync_directorio(ruta)
    _limpiar_respaldos(ruta)


def _leer_marcador(ruta: Path) -> dict[str, object] | None:
    """Lee el marcador sin confiar en el.

    El documento solo declara *que* hay una migracion pendiente; los respaldos
    viven en nombres fijos. Un contenido corrupto no cambia que la accion segura
    sea restaurar, asi que se degrada a `None` en vez de bloquear el proyecto. Lo
    unico que se propaga es un fallo real de lectura, ya tipado.
    """
    try:
        datos = json.loads(ruta.read_text(encoding="utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return None
    except OSError as error:
        raise ErrorMigracionProyecto("El marcador de migracion no es legible.") from error
    return datos if isinstance(datos, dict) else None


def _recuperar_migracion(ruta: Path) -> None:
    """Recupera una migracion interrumpida. Solo la invoca quien posee el lock."""
    marcador = ruta / NOMBRE_MARCADOR_MIGRACION
    if not marcador.is_file():
        return
    _leer_marcador(marcador)
    respaldo_base = ruta / NOMBRE_RESPALDO_BASE
    respaldo_manifest = ruta / NOMBRE_RESPALDO_MANIFIESTO
    if not (respaldo_base.is_file() and respaldo_manifest.is_file()):
        # El marcador se publica despues de los respaldos y se borra antes que
        # ellos: sin respaldos no hay migracion que deshacer. Se descarta el
        # marcador huerfano y la coherencia real la valida la apertura.
        marcador.unlink(missing_ok=True)
        _limpiar_respaldos(ruta)
        return
    try:
        _restaurar(ruta)
    except OSError as error:
        raise ErrorMigracionProyecto("No se pudo recuperar una migracion interrumpida.") from error


def _migrar(ruta: Path, manifest: dict[str, object], fallo: Callable[[int], None] | None = None) -> dict[str, object]:
    """Migra base y manifiesto juntos, bajo respaldo y marcador durables.

    Antes de tocar nada se exige que la base *ya sea* coherente con la version
    que declara. Migrar una imagen incoherente convertiria un diagnostico
    preciso ("faltan las tablas de tu version") en un fallo de migracion
    generico, y ademas gastaria un respaldo y un marcador para descubrirlo.
    """
    base = ruta / NOMBRE_BASE
    marcador = ruta / NOMBRE_MARCADOR_MIGRACION
    try:
        conexion_previa = _conexion(base)
    except (OSError, sqlite3.Error) as error:
        raise ErrorProyecto("No se pudo abrir la base del proyecto.") from error
    try:
        _validar_esquema(conexion_previa, conexion_previa.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conexion_previa.close()
    try:
        _limpiar_respaldos(ruta)
        _respaldar(ruta)
        _escribir_manifest(marcador, {"formato": FORMATO_MARCADOR,
                                      "desde": int(manifest["schema_version"]),
                                      "hasta": VERSION_ESQUEMA})
    except (OSError, sqlite3.Error) as error:
        marcador.unlink(missing_ok=True)
        _limpiar_respaldos(ruta)
        raise ErrorMigracionProyecto("No se pudo preparar el respaldo de la migracion.") from error

    conexion: sqlite3.Connection | None = None
    try:
        conexion = _conexion(base)
        version = conexion.execute("PRAGMA user_version").fetchone()[0]
        conexion.execute("BEGIN IMMEDIATE")
        while version < VERSION_ESQUEMA:
            siguiente = version + 1
            _aplicar(conexion, MIGRACIONES[siguiente])
            conexion.execute(f"PRAGMA user_version={siguiente}")
            if fallo:
                fallo(siguiente)
            version = siguiente
        conexion.commit()
    except BaseException as error:
        if conexion is not None:
            try:
                conexion.rollback()
            except sqlite3.Error:
                pass
            conexion.close()
            conexion = None
        try:
            _restaurar(ruta)
        except OSError as fallo_restauracion:
            raise ErrorMigracionProyecto("La migracion fallo y el respaldo no pudo restaurarse.") from fallo_restauracion
        # Un aborto *diagnosticado* conserva su explicacion. Aplastarlo bajo el
        # mensaje generico dejaria a quien lo lea sin lo unico accionable que
        # habia —que trabajo hay que resolver y por que—, y le haria creer que la
        # migracion se rompio sola en vez de detenerse a proposito.
        if isinstance(error, ErrorMigracionProyecto):
            raise ErrorMigracionProyecto(
                f"{error} Se restauro el proyecto anterior.") from error
        raise ErrorMigracionProyecto("La migracion fallo; se restauro el proyecto anterior.") from error
    finally:
        if conexion is not None:
            conexion.close()  # checkpoint: la imagen en disco queda completa.

    actualizado = dict(manifest, schema_version=VERSION_ESQUEMA)
    _escribir_manifest(ruta / NOMBRE_MANIFIESTO, actualizado)
    marcador.unlink(missing_ok=True)  # pivote: desde aqui la migracion esta hecha.
    _fsync_directorio(ruta)
    _limpiar_respaldos(ruta)
    return actualizado


# --------------------------------------------------------------------------- #
# Repositorio
# --------------------------------------------------------------------------- #

def _nombres(conexion: sqlite3.Connection, tipo: str) -> set[str]:
    return {fila[0] for fila in conexion.execute("SELECT name FROM sqlite_master WHERE type=?", (tipo,))}


def _claves_foraneas(conexion: sqlite3.Connection, tabla: str) -> set[tuple]:
    """Cada FK declarada como (tabla, padre, ((columna_hija, columna_padre), ...))."""
    grupos: dict[int, tuple[str, list[tuple[str, str]]]] = {}
    for identificador, _seq, padre, hija, columna, *_ in conexion.execute(f"PRAGMA foreign_key_list({tabla})"):
        grupos.setdefault(identificador, (padre, []))[1].append((hija, columna))
    return {(tabla, padre, tuple(sorted(columnas))) for padre, columnas in grupos.values()}


def _columnas(conexion: sqlite3.Connection, tabla: str) -> set[str]:
    return {fila[1] for fila in conexion.execute(f"PRAGMA table_info({tabla})")}


def _indices_unicos(conexion: sqlite3.Connection, tabla: str) -> set[tuple]:
    """Indices UNIQUE de una tabla, como (tabla, (columnas en orden))."""
    encontrados = set()
    for _seq, nombre, unico, *_ in conexion.execute(f"PRAGMA index_list({tabla})"):
        if not unico:
            continue
        columnas = tuple(fila[2] for fila in conexion.execute(f"PRAGMA index_info({nombre})"))
        if all(columna is not None for columna in columnas):
            encontrados.add((tabla, columnas))
    return encontrados


def _validar_esquema(conexion: sqlite3.Connection, version: int) -> None:
    """Ningun repositorio sale de aqui sin las tablas, columnas y FKs de su version."""
    if version not in TABLAS_REQUERIDAS:
        raise ErrorProyecto("La base del proyecto declara una version de esquema desconocida.")
    try:
        tablas = _nombres(conexion, "table")
        if not TABLAS_REQUERIDAS[version] <= tablas:
            raise ErrorProyecto("La base del proyecto no contiene las tablas de su version de esquema.")
        for tabla, requeridas in COLUMNAS_REQUERIDAS[version].items():
            if not requeridas <= _columnas(conexion, tabla):
                raise ErrorProyecto("La base del proyecto no contiene las columnas de su version de esquema.")
        indices = set()
        for tabla in {requerido[0] for requerido in INDICES_UNICOS_REQUERIDOS[version]}:
            indices |= _indices_unicos(conexion, tabla)
        if not INDICES_UNICOS_REQUERIDOS[version] <= indices:
            raise ErrorProyecto("La base del proyecto no contiene los indices de su version de esquema.")
        declaradas = set()
        for tabla in {requerida[0] for requerida in FKS_REQUERIDAS[version]}:
            declaradas |= _claves_foraneas(conexion, tabla)
    except sqlite3.Error as error:
        raise ErrorProyecto("No se pudo inspeccionar el esquema del proyecto.") from error
    if not FKS_REQUERIDAS[version] <= declaradas:
        raise ErrorProyecto("La base del proyecto no contiene las validaciones de su version de esquema.")


class RepositorioSqliteProyecto:
    def __init__(self, raiz: Path, conexion: sqlite3.Connection, solo_lectura: bool) -> None:
        self.raiz, self.conexion, self.solo_lectura = raiz, conexion, solo_lectura

    @contextmanager
    def transaccion(self) -> Iterator[sqlite3.Connection]:
        if self.solo_lectura:
            raise ErrorProyecto("El proyecto esta abierto en solo lectura.")
        try:
            self.conexion.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as error:
            raise ErrorProyecto("No se pudo iniciar una transaccion del proyecto.") from error
        try:
            yield self.conexion
            self.conexion.commit()
        except BaseException as error:
            try:
                self.conexion.rollback()
            except sqlite3.Error:
                pass
            if isinstance(error, sqlite3.Error):
                raise ErrorProyecto("La operacion sobre el proyecto no pudo confirmarse.") from error
            raise

    def informacion(self) -> InformacionProyecto:
        try:
            fila = self.conexion.execute("SELECT id, name FROM Project LIMIT 1").fetchone()
            version = self.conexion.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.Error as error:
            raise ErrorProyecto("No se pudo leer la identidad del proyecto.") from error
        if fila is None:
            raise ErrorProyecto("La base del proyecto no contiene su identidad.")
        return InformacionProyecto(fila[0], fila[1], version)

    def actualizar_manifiesto(self, cambios: Mapping[str, object]) -> None:
        """Reescribe `project.json` conservando lo que este cambio no toca.

        El manifiesto es una vista derivada, no la autoridad: SQLite ya
        confirmo el hecho antes de llegar aqui. Por eso se reescribe *despues*
        del commit y un fallo no deshace nada —el proyecto sigue siendo correcto
        con un manifiesto viejo, y la siguiente escritura lo pone al dia—,
        mientras que el orden inverso publicaria en la carpeta un estado que la
        base todavia no tiene.

        La escritura es durable y atomica (temporal + `replace` + `fsync`), de
        modo que nunca queda un `project.json` a medias: o el anterior o el
        nuevo, nunca uno truncado que impida abrir el proyecto.
        """
        if self.solo_lectura:
            raise ErrorProyecto("El proyecto esta abierto en solo lectura.")
        ruta = self.raiz / NOMBRE_MANIFIESTO
        datos = _leer_manifest(ruta)
        datos.update(cambios)
        try:
            _escribir_manifest(ruta, datos)
        except OSError as error:
            raise ErrorProyecto("No se pudo actualizar el manifiesto del proyecto.") from error

    def agregar_fuente(self, ruta: str, fingerprint: str, copiar: bool = False) -> str:
        if self.solo_lectura:
            raise ErrorProyecto("El proyecto esta abierto en solo lectura.")
        if not fingerprint:
            raise ErrorFuenteProyecto("La fuente requiere un fingerprint suministrado.")
        origen = Path(ruta)
        if not origen.is_file():
            raise ErrorFuenteProyecto("No encuentro la fuente indicada.")
        # La identidad se resuelve antes de copiar: un fallo aqui no puede dejar
        # un archivo huerfano porque todavia no se escribio nada.
        proyecto = self.informacion().project_id
        fuente_id = str(uuid.uuid4())
        destino: Path | None = None
        if copiar:
            destino = _ruta_interna(self.raiz, f"sources/{fuente_id}_{origen.name}")
            temporal = _temporal_de(destino)
            try:
                shutil.copy2(origen, temporal)
                os.replace(temporal, destino)
            except OSError as error:
                temporal.unlink(missing_ok=True)
                destino.unlink(missing_ok=True)
                raise ErrorFuenteProyecto("No se pudo copiar la fuente al proyecto.") from error
            modo, relativa, externa = "copied", destino.relative_to(self.raiz).as_posix(), None
        else:
            modo, relativa, externa = "referenced", None, str(origen.resolve())
        try:
            with self.transaccion() as conexion:
                conexion.execute(
                    "INSERT INTO SourceAsset (id, project_id, mode, relative_path, external_path, fingerprint) VALUES (?, ?, ?, ?, ?, ?)",
                    (fuente_id, proyecto, modo, relativa, externa, fingerprint))
        except BaseException as error:
            if destino is not None:
                destino.unlink(missing_ok=True)
            if isinstance(error, ErrorFuenteProyecto):
                raise
            raise ErrorFuenteProyecto("No se pudo registrar la fuente.") from error
        return fuente_id

    def relocalizar_fuente(self, fuente_id: str, ruta: str,
                           obtener_huella_confiable: Callable[[Path], str]) -> None:
        """T04 proveera la huella; esta capa nunca acepta una afirmacion de UI."""
        if self.solo_lectura:
            raise ErrorProyecto("El proyecto esta abierto en solo lectura.")
        try:
            fila = self.conexion.execute("SELECT fingerprint, mode FROM SourceAsset WHERE id=?", (fuente_id,)).fetchone()
        except sqlite3.Error as error:
            raise ErrorFuenteProyecto("No se pudo consultar la fuente indicada.") from error
        if fila is None or fila[1] != "referenced":
            raise ErrorFuenteProyecto("La fuente no coincide con el fingerprint esperado.")
        candidata = Path(ruta)
        if not candidata.is_file():
            raise ErrorFuenteProyecto("No encuentro la fuente indicada.")
        try:
            fingerprint = obtener_huella_confiable(candidata)
        except Exception as error:
            raise ErrorFuenteProyecto("No fue posible verificar la huella de la fuente candidata.") from error
        if fila[0] != fingerprint:
            raise ErrorFuenteProyecto("La fuente no coincide con el fingerprint esperado.")
        try:
            with self.transaccion() as conexion:
                conexion.execute("UPDATE SourceAsset SET external_path=?, state='available' WHERE id=?",
                                 (str(candidata.resolve()), fuente_id))
        except ErrorFuenteProyecto:
            raise
        except ErrorProyecto as error:
            raise ErrorFuenteProyecto("No se pudo relocalizar la fuente.") from error

    def reconciliar(self) -> ResultadoReconciliacion:
        """Detecta siempre; persiste solo si esta sesion es la escritora."""
        try:
            tablas = _nombres(self.conexion, "table")
            fuentes = list(self.conexion.execute("SELECT id, mode, relative_path, external_path FROM SourceAsset")) if "SourceAsset" in tablas else []
            artefactos = list(self.conexion.execute("SELECT id, relative_path FROM AnalysisArtifact")) if "AnalysisArtifact" in tablas else []
        except sqlite3.Error as error:
            raise ErrorProyecto("No se pudo inspeccionar el estado del proyecto.") from error
        faltantes = reconstruibles = 0
        cambios: list[tuple[str, str]] = []
        for fila in fuentes:
            try:
                ruta = _ruta_interna(self.raiz, fila[2] or "") if fila[1] == "copied" else Path(fila[3] or "")
            except ErrorProyecto:
                ruta = Path()
            if not ruta.is_file():
                cambios.append(("source", fila[0]))
                faltantes += 1
        for fila in artefactos:
            try:
                existe = _ruta_interna(self.raiz, fila[1] or "").is_file()
            except ErrorProyecto:
                existe = False
            if not existe:
                cambios.append(("artifact", fila[0]))
                reconstruibles += 1
        if not self.solo_lectura and cambios:
            with self.transaccion() as conexion:
                for tipo, identificador in cambios:
                    if tipo == "source":
                        conexion.execute("UPDATE SourceAsset SET state='missing' WHERE id=?", (identificador,))
                    else:
                        conexion.execute("UPDATE AnalysisArtifact SET state='rebuildable' WHERE id=?", (identificador,))
        return ResultadoReconciliacion(faltantes, reconstruibles)


class ProyectoAbierto:
    def __init__(self, repositorio: RepositorioSqliteProyecto, lock: _LockEscritor | None = None) -> None:
        self.repositorio, self.solo_lectura, self._lock = repositorio, repositorio.solo_lectura, lock

    def close(self) -> None:
        try:
            self.repositorio.conexion.close()
        finally:
            if self._lock is not None:
                self._lock.close()
                self._lock = None

    def __enter__(self) -> "ProyectoAbierto":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


# --------------------------------------------------------------------------- #
# Creacion y apertura
# --------------------------------------------------------------------------- #

def _con_sidecars(nombre: str) -> set[str]:
    return {nombre} | {nombre + sufijo for sufijo in SUFIJOS_SIDECAR}


# Se deriva en vez de enumerarse a mano: la base temporal tambien abre en WAL
# durante la creacion, y omitir sus sidecars haria que un observador concurrente
# tomase un residuo propio por contenido ajeno.
_RESIDUOS_DE_CREACION = frozenset(
    set(DIRECTORIOS_PROYECTO)
    | {NOMBRE_LOCK, _temporal_de(Path(NOMBRE_MANIFIESTO)).name}
    | _con_sidecars(NOMBRE_BASE)
    | _con_sidecars(_temporal_de(Path(NOMBRE_BASE)).name)
)


def _residuo_reconocible(hijo: Path) -> bool:
    """Un hijo es residuo propio solo si su nombre esta permitido y, cuando es
    directorio, esta **vacio**.

    La creacion nunca deposita nada dentro de los directorios del proyecto antes
    de publicar: `sources/` y sus hermanos nacen y mueren vacios. Por eso un
    directorio permitido *con* contenido no es residuo, es contenido
    desconocido, y basta un nieto inesperado para que la carpeta entera deje de
    ser reclamable. Comprobar solo el nombre del hijo dejaria que un
    `sources/user.txt` viajase de polizon dentro de un nombre autorizado.

    Un enlace simbolico nunca es residuo: seguirlo permitiria juzgar —y luego
    retirar— algo que vive fuera del destino.
    """
    if hijo.name not in _RESIDUOS_DE_CREACION or hijo.is_symlink():
        return False
    if hijo.is_dir():
        try:
            return not any(hijo.iterdir())
        except OSError:
            return False
    return True


def _es_creacion_abortada(destino: Path) -> bool:
    """Sin `project.json` nunca hubo publicacion, luego no hay datos del usuario.

    Se exige ademas que todo lo presente sea residuo reconocible de esta capa:
    una carpeta con contenido ajeno no se reclama jamas.

    Fuera del lock esto es una cortesia —evita sembrar un `.clipsapp.lock` en una
    carpeta que claramente no es nuestra—; la respuesta que decide es la que se
    vuelve a pedir con el lock en mano, donde nadie mas puede estar escribiendo.
    """
    if (destino / NOMBRE_MANIFIESTO).exists():
        return False
    return all(_residuo_reconocible(hijo) for hijo in destino.iterdir())


def _reclamar_destino(destino: Path) -> None:
    """`mkdir` es el guardia: atomico, y especifico de este destino y solo de el."""
    try:
        destino.mkdir()
        return
    except FileExistsError as error:
        try:
            reclamable = destino.is_dir() and _es_creacion_abortada(destino)
        except OSError:
            reclamable = False
        if not reclamable:
            raise ErrorProyecto("La carpeta de proyecto ya contiene archivos.") from error
    except OSError as error:
        raise ErrorProyecto("No se pudo preparar la carpeta de proyecto.") from error


def _limpiar_residuos(destino: Path) -> None:
    """Retira residuos de creacion. Exige el lock y jamas borra el directorio.

    Ni la carpeta ni `.clipsapp.lock` se eliminan nunca. Borrarlos desvincularia
    un inodo que otro creador puede haber reclamado ya —y, si la limpieza corre
    tras soltar el lock, se lo arrebataria en pleno uso—, de modo que el estado
    reintentable es exactamente "carpeta con su lock y nada mas". Un solo hijo
    que no sea residuo reconocible cancela la limpieza entera: el contenido
    ajeno no se toca ni se inventaria, ni siquiera para acompanar a un residuo
    propio.

    No hay borrado recursivo en ningun punto. Los directorios se retiran con
    `rmdir`, que se niega si algo quedo dentro: aunque el juicio de arriba
    fallase, el peor caso posible es no borrar, nunca consumir contenido
    desconocido.
    """
    if (destino / NOMBRE_MANIFIESTO).exists():
        return
    try:
        hijos = list(destino.iterdir())
    except OSError:
        return
    if not all(_residuo_reconocible(hijo) for hijo in hijos):
        return
    for hijo in hijos:
        if hijo.name == NOMBRE_LOCK:
            continue
        try:
            hijo.rmdir() if hijo.is_dir() else hijo.unlink()
        except OSError:
            pass  # Un handle vivo solo difiere la limpieza; el destino sigue reintentable.


def crear_proyecto(raiz: str | Path, nombre: str, replace: Replace = os.replace) -> ProyectoAbierto:
    """Crea y devuelve el proyecto con su lock de escritor ya retenido.

    `mkdir` sobre el destino excluye a otros creadores sin acoplar proyectos
    vecinos. El lock se toma inmediatamente despues y ya no se suelta, de modo
    que no existe ventana entre publicar y poseer. `project.json` se escribe al
    final: mientras no exista, el destino no es un proyecto y sus residuos se
    retiran —siempre bajo el lock y sin borrar la carpeta— para dejarlo
    reintentable.
    """
    destino = Path(raiz)
    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise ErrorProyecto("No se pudo preparar la carpeta contenedora.") from error
    _reclamar_destino(destino)
    lock = _adquirir_lock(destino)
    if lock is None:
        # Sin lock no se borra nada: el duenio legitimo puede estar creando aqui.
        raise ErrorProyecto("Otra instancia esta usando esta carpeta de proyecto.")
    conexion: sqlite3.Connection | None = None
    try:
        # Se reverifica bajo el lock: entre el reclamo y esta linea otra sesion
        # pudo publicar y cerrar, y vaciar entonces borraria un proyecto real.
        if not _es_creacion_abortada(destino):
            raise ErrorProyecto("La carpeta de proyecto ya contiene archivos.")
        _limpiar_residuos(destino)
        for directorio in DIRECTORIOS_PROYECTO:
            (destino / directorio).mkdir(exist_ok=True)
        project_id = str(uuid.uuid4())
        base_final = destino / NOMBRE_BASE
        base_temporal = _temporal_de(base_final)
        base_temporal.touch(exist_ok=False)
        conexion = _conexion(base_temporal)
        conexion.execute("BEGIN IMMEDIATE")
        _sql_v1(conexion)
        _sql_v2(conexion)
        _sql_v3(conexion)
        _sql_v4(conexion)
        _sql_v5(conexion)
        _sql_v6(conexion)
        conexion.execute(f"PRAGMA user_version={VERSION_ESQUEMA}")
        conexion.execute("INSERT INTO Project (id, name) VALUES (?, ?)", (project_id, nombre))
        conexion.commit()
        conexion.close()
        conexion = None
        _neutralizar_sidecars(base_temporal)
        os.replace(base_temporal, base_final)
        conexion = _conexion(base_final)
        # Ultimo paso: publicar el manifiesto. Un fallo previo no deja proyecto.
        _escribir_manifest(destino / NOMBRE_MANIFIESTO,
                           {"project_id": project_id, "name": nombre, "schema_version": VERSION_ESQUEMA},
                           replace)
        _fsync_directorio(destino)
        return ProyectoAbierto(RepositorioSqliteProyecto(destino, conexion, False), lock)
    except BaseException as error:
        if conexion is not None:
            conexion.close()
        try:
            _limpiar_residuos(destino)  # Con el lock aun en mano: nadie mas esta dentro.
        finally:
            lock.close()
        if isinstance(error, ErrorProyecto) or not isinstance(error, (sqlite3.Error, OSError)):
            raise
        raise ErrorProyecto("No se pudo crear el proyecto.") from error


def abrir_proyecto(raiz: str | Path, fallo_migracion: Callable[[int], None] | None = None) -> ProyectoAbierto:
    """Abre el proyecto: escritor si consigue el lock, read-only estricto si no."""
    carpeta = Path(raiz)
    if not carpeta.is_dir() or not (carpeta / NOMBRE_MANIFIESTO).is_file():
        raise ErrorProyecto("La carpeta indicada no contiene un proyecto ClipperKick.")
    # El lock precede a todo efecto: recuperar y migrar son privilegios del escritor.
    lock = _adquirir_lock(carpeta)
    solo_lectura = lock is None
    conexion: sqlite3.Connection | None = None
    try:
        if solo_lectura:
            if (carpeta / NOMBRE_MARCADOR_MIGRACION).is_file():
                raise ErrorProyecto("El proyecto tiene una migracion pendiente y no puede abrirse en solo lectura.")
        else:
            _recuperar_migracion(carpeta)
        manifest = _leer_manifest(carpeta / NOMBRE_MANIFIESTO)
        version_manifest = int(manifest["schema_version"])
        if version_manifest > VERSION_ESQUEMA:
            raise ErrorProyecto("El proyecto requiere una version mas reciente de ClipperKick.")
        if not solo_lectura and version_manifest < VERSION_ESQUEMA:
            manifest = _migrar(carpeta, manifest, fallo_migracion)
        conexion = _conexion(carpeta / NOMBRE_BASE, solo_lectura)
        repositorio = RepositorioSqliteProyecto(carpeta, conexion, solo_lectura)
        info = repositorio.informacion()
        if info.project_id != manifest["project_id"] or info.schema_version != manifest["schema_version"]:
            raise ErrorProyecto("El manifiesto y la base del proyecto no son coherentes.")
        _validar_esquema(conexion, info.schema_version)
        abierto = ProyectoAbierto(repositorio, lock)
        conexion, lock = None, None
        return abierto
    except BaseException as error:
        if conexion is not None:
            conexion.close()
        if lock is not None:
            lock.close()
        if isinstance(error, ErrorProyecto) or not isinstance(error, (sqlite3.Error, OSError)):
            raise
        raise ErrorProyecto("No se pudo abrir la base del proyecto.") from error
