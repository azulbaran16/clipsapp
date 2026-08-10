"""Repositorio SQLite de fuentes y artefactos de ingesta (esquema v6).

Hereda las dos reglas de T02/T03 y anade una tercera propia:

1. **Nada del disco es entrada confiable.** Una fila con una huella ilegible no
   se degrada a "fuente sin huella": se rechaza con un error tipado, porque una
   fuente sin identidad reutilizaria o invalidaria artefactos al azar.
2. **Cada cambio observable va en una transaccion.** Registrar una fuente,
   reemplazarla e invalidar sus descendientes son un unico hecho.
3. **Reemplazar no borra.** La fuente vieja queda `replaced` y sus artefactos
   `rebuildable`; momentos, drafts y revisiones siguen intactos y legibles. Lo
   que caduca es lo derivado del contenido, no la historia ni el trabajo humano.

`registrar_artefacto` es idempotente a proposito: publicar en disco y confirmar
en SQLite son dos operaciones y el proceso puede morir entre ambas. La ingesta
siguiente recalcula la misma clave, adopta la publicacion y vuelve a registrar;
si eso fuese un alta estricta, la recuperacion fallaria justo en el caso para el
que existe.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import sqlite3
import uuid

from clipperkick.application.ingest.ports import UbicacionFuente
from clipperkick.domain.ingest import (
    ESTADO_DISPONIBLE, ESTADO_RECONSTRUIBLE, ErrorFuenteIngesta, ErrorIngesta, EstadoFuente,
    HuellaFuente, LinajeArtefacto, ModoFuente, OrigenFuente, RegistroArtefacto, SourceAsset,
    referencia_de_apertura,
)
from clipperkick.domain.project.errors import ErrorProyecto
from clipperkick.infrastructure.project.persistence import RepositorioSqliteProyecto


_CAMPOS_FUENTE = ("id", "mode", "origin", "display_name", "relative_path", "external_path",
                  "source_uri", "fingerprint", "fingerprint_partial", "fingerprint_version",
                  "size_bytes", "mtime_ns", "state")
_SELECT_FUENTE = f"SELECT {', '.join(_CAMPOS_FUENTE)} FROM SourceAsset"

_CAMPOS_ARTEFACTO = ("id", "source_id", "relative_path", "artifact_path", "kind", "state",
                     "materialization_key", "checksum", "bytes", "stage", "contract_version",
                     "provider_version", "lineage_json")
_SELECT_ARTEFACTO = f"SELECT {', '.join(_CAMPOS_ARTEFACTO)} FROM AnalysisArtifact"

DIRECTORIO_ARTEFACTOS = "artifacts"


def _texto(valor: object, campo: str) -> str:
    if not isinstance(valor, str):
        raise ErrorIngesta(f"El proyecto guarda un '{campo}' que no es texto.")
    return valor


def _entero_opcional(valor: object, campo: str) -> int | None:
    if valor is None:
        return None
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise ErrorIngesta(f"El proyecto guarda un '{campo}' que no es un entero.")
    return valor


def _documento(texto: object, campo: str) -> dict[str, object]:
    if texto is None or texto == "":
        return {}
    if not isinstance(texto, str):
        raise ErrorIngesta(f"El proyecto guarda un '{campo}' que no es texto.")
    try:
        datos = json.loads(texto)
    except ValueError as error:
        raise ErrorIngesta(f"El proyecto guarda un '{campo}' que no es JSON.") from error
    if not isinstance(datos, dict):
        raise ErrorIngesta(f"El proyecto guarda un '{campo}' que no es un objeto.")
    return datos


def ruta_publicada(clave: str, artifact_path: str) -> str:
    return f"{DIRECTORIO_ARTEFACTOS}/{clave}/{artifact_path}"


class RepositorioSqliteFuentes:
    """Implementa `RepositorioFuentes` sobre la conexion abierta por T02."""

    def __init__(self, proyecto: RepositorioSqliteProyecto) -> None:
        self._proyecto = proyecto

    @property
    def conexion(self) -> sqlite3.Connection:
        return self._proyecto.conexion

    # ------------------------------------------------------------------ #
    # Lectura
    # ------------------------------------------------------------------ #

    def _consultar(self, sql: str, parametros: Sequence[object] = ()) -> list[tuple]:
        try:
            return list(self.conexion.execute(sql, tuple(parametros)))
        except sqlite3.Error as error:
            raise ErrorIngesta("No se pudo consultar el estado de las fuentes.") from error

    def _fuente(self, fila: tuple) -> SourceAsset:
        try:
            modo = ModoFuente(_texto(fila[1], "mode"))
            origen = OrigenFuente(_texto(fila[2], "origin"))
            estado = EstadoFuente(_texto(fila[12], "state"))
        except ValueError as error:
            raise ErrorIngesta(f"El proyecto guarda una fuente con valores desconocidos: {error}") from error
        tamano = _entero_opcional(fila[10], "size_bytes")
        if tamano is None:
            # Una fila heredada de v1..v5 no tiene con que reconciliarse. Se
            # rechaza en vez de inventarle un tamano: reutilizar sus artefactos
            # sobre una identidad desconocida es exactamente lo que el ticket
            # prohibe, y el camino correcto es volver a ingerirla.
            raise ErrorFuenteIngesta(
                f"La fuente {fila[0]!r} viene de un esquema anterior y no declara su huella;"
                " vuelve a ingerirla para reconciliarla.")
        huella = HuellaFuente(
            tamano=tamano, parcial=_texto(fila[8], "fingerprint_partial"),
            completa=_texto(fila[7], "fingerprint"), mtime_ns=_entero_opcional(fila[11], "mtime_ns"),
            version=_texto(fila[9], "fingerprint_version"))
        return SourceAsset(
            id=_texto(fila[0], "id"), nombre=_texto(fila[3], "display_name") or _texto(fila[0], "id"),
            modo=modo, origen=origen, huella=huella,
            ruta_relativa=fila[4] if modo is ModoFuente.COPIADA else None,
            ruta_externa=fila[5] if modo is ModoFuente.REFERENCIADA else None,
            entrada_original=_texto(fila[6], "source_uri"), estado=estado)

    def buscar_por_identidad(self, identidad: str) -> SourceAsset | None:
        """Fuente vigente con esa identidad de contenido, si la hay.

        Se excluyen las reemplazadas: siguen ahi para que su historia se pueda
        leer, pero no vuelven a ser el destino de una ingesta. Si alguien
        reingiere los bytes viejos, lo correcto es un alta nueva y no resucitar
        una fila que ya declaro haber sido sustituida.
        """
        filas = self._consultar(f"{_SELECT_FUENTE} WHERE fingerprint=? AND state<>? ORDER BY rowid",
                                (identidad, EstadoFuente.REEMPLAZADA.value))
        return self._fuente(filas[0]) if filas else None

    def buscar_por_origen(self, entrada: str) -> SourceAsset | None:
        """Ultima fuente vigente que llego por esa misma entrada del mundo."""
        if not entrada:
            return None
        filas = self._consultar(
            f"{_SELECT_FUENTE} WHERE source_uri=? AND state<>? ORDER BY rowid DESC LIMIT 1",
            (entrada, EstadoFuente.REEMPLAZADA.value))
        return self._fuente(filas[0]) if filas else None

    def listar(self) -> tuple[SourceAsset, ...]:
        return tuple(self._fuente(fila)
                     for fila in self._consultar(f"{_SELECT_FUENTE} ORDER BY rowid"))

    # ------------------------------------------------------------------ #
    # Referencias de apertura en el manifiesto
    # ------------------------------------------------------------------ #

    def _referencia(self, fila: tuple) -> dict[str, object]:
        """Referencia de apertura de una fila, incluidas las que v6 heredo.

        Una fila migrada de v5 no se puede modelar —no declara tamano ni huella
        parcial—, pero *si* dice donde estaba su archivo, y eso es justo lo que
        el manifiesto existe para conservar. Omitirla dejaria una fuente que el
        proyecto conoce y la carpeta no menciona, que es la peor combinacion
        para diagnosticar un proyecto que no abre.
        """
        try:
            return referencia_de_apertura(self._fuente(fila))
        except ErrorIngesta:
            return {"id": fila[0], "modo": fila[1], "origen": fila[2],
                    "ruta_relativa": fila[4], "ruta_externa": fila[5],
                    "tamano": None, "mtime_ns": None, "huella_parcial": "",
                    "version_huella": fila[9] or "legacy/0"}

    def publicar_referencias(self) -> tuple[dict[str, object], ...]:
        """Deja en `project.json` lo minimo para abrir y diagnosticar las fuentes.

        Solo viajan modo, ruta, tamano, mtime y huella parcial: lo que permite
        *encontrar* la fuente y notar que ya no es la misma. La huella completa,
        los avisos y la metadata se quedan donde son autoridad —SQLite y el
        artefacto publicado—, porque duplicarlos crearia dos verdades sobre lo
        mismo y la del manifiesto quedaria vieja al primer cambio.

        Las fuentes reemplazadas no se listan: el manifiesto sirve para abrir, y
        una fuente jubilada no es un archivo que haya que localizar.
        """
        referencias = tuple(
            self._referencia(fila) for fila in self._consultar(
                f"{_SELECT_FUENTE} WHERE state<>? ORDER BY rowid",
                (EstadoFuente.REEMPLAZADA.value,)))
        self._proyecto.actualizar_manifiesto({"fuentes": list(referencias)})
        return referencias

    def _publicar_referencias_derivadas(self) -> None:
        """Actualiza el manifiesto sin poder deshacer lo que ya se confirmo.

        Se llama despues del commit, y por eso no puede propagar su fallo: quien
        llama interpreta una excepcion como "la fila no llego a existir" y
        limpiaria los bytes de una fuente que si quedo registrada. El manifiesto
        es una vista derivada de SQLite —el proyecto abre y funciona con uno
        viejo—, de modo que el peor caso de tragarse el fallo es una vista
        desactualizada que la siguiente escritura corrige, mientras que
        propagarlo deja el proyecto y su carpeta contradiciendose.

        Quien necesite la garantia fuerte llama a `publicar_referencias`, que si
        informa de su fallo.
        """
        try:
            self.publicar_referencias()
        except (ErrorProyecto, ErrorIngesta):
            pass

    # ------------------------------------------------------------------ #
    # Escritura de fuentes
    # ------------------------------------------------------------------ #

    def _insertar(self, tx: sqlite3.Connection, fuente: SourceAsset, proyecto: str,
                  reemplaza_a: str | None) -> None:
        """El `INSERT` de una fuente, sin transaccion propia.

        Se aisla para que `registrar` y `suceder` escriban exactamente la misma
        fila: si cada uno tuviera su sentencia, la sucesora podria acabar con un
        conjunto de columnas distinto del alta normal y la diferencia solo se
        veria mucho despues, al leerla.
        """
        tx.execute(
            "INSERT INTO SourceAsset (id, project_id, mode, relative_path, external_path,"
            " fingerprint, state, source_uri, origin, display_name, size_bytes, mtime_ns,"
            " fingerprint_partial, fingerprint_version, supersedes)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (fuente.id, proyecto, fuente.modo.value, fuente.ruta_relativa,
             fuente.ruta_externa, fuente.huella.identidad, fuente.estado.value,
             fuente.entrada_original, fuente.origen.value, fuente.nombre,
             fuente.huella.tamano, fuente.huella.mtime_ns, fuente.huella.parcial,
             fuente.huella.version, reemplaza_a))

    def registrar(self, fuente: SourceAsset, reemplaza_a: str | None = None) -> SourceAsset:
        proyecto = self._proyecto.informacion().project_id
        try:
            with self._proyecto.transaccion() as tx:
                self._insertar(tx, fuente, proyecto, reemplaza_a)
        except ErrorProyecto as error:
            causa = error.__cause__
            if isinstance(causa, sqlite3.IntegrityError):
                raise ErrorFuenteIngesta(
                    f"El proyecto ya conoce una fuente con la identidad {fuente.huella.identidad!r}."
                ) from error
            raise ErrorIngesta(f"No se pudo registrar la fuente {fuente.id!r}.") from error
        self._publicar_referencias_derivadas()
        return fuente

    def relocalizar(self, fuente_id: str, modo: str, ruta_relativa: str | None,
                    ruta_externa: str | None) -> SourceAsset:
        """Apunta una fuente conocida a donde estan sus bytes ahora.

        No toca la huella: quien llama ya comprobo que el contenido es el mismo,
        y reescribirla aqui permitiria mover una fuente a bytes distintos sin que
        nada lo notase.
        """
        try:
            with self._proyecto.transaccion() as tx:
                cursor = tx.execute(
                    "UPDATE SourceAsset SET mode=?, relative_path=?, external_path=?, state=?"
                    " WHERE id=? AND state<>?",
                    (modo, ruta_relativa, ruta_externa, EstadoFuente.DISPONIBLE.value, fuente_id,
                     EstadoFuente.REEMPLAZADA.value))
                if cursor.rowcount != 1:
                    raise ErrorFuenteIngesta(
                        f"No hay una fuente vigente {fuente_id!r} que relocalizar.")
        except ErrorProyecto as error:
            raise ErrorIngesta(f"No se pudo relocalizar la fuente {fuente_id!r}.") from error
        filas = self._consultar(f"{_SELECT_FUENTE} WHERE id=?", (fuente_id,))
        if not filas:
            raise ErrorFuenteIngesta(f"No existe la fuente {fuente_id!r}.")
        self._publicar_referencias_derivadas()
        return self._fuente(filas[0])

    def suceder(self, nueva: SourceAsset,
                anterior_id: str) -> tuple[SourceAsset, tuple[str, ...]]:
        """Alta de la sucesora, jubilacion de la anterior e invalidacion, **juntas**.

        Los tres hechos son uno solo y por eso comparten transaccion. Repartidos
        en dos commits, un fallo entre ambos deja las dos fuentes `available`
        —dos filas vigentes para la misma entrada, una de ellas apuntando a
        bytes que ya no existen— y los descendientes viejos declarandose validos
        sobre un contenido que cambio. No es un estado transitorio que la
        siguiente apertura arregle: nada lo distingue de un proyecto sano.

        Que la operacion viva en el repositorio y no en el caso de uso no es una
        preferencia de capas: la atomicidad es una propiedad del almacen, y solo
        aqui existe la transaccion que la puede dar.

        Los artefactos de otras fuentes no se tocan: el ticket exige que los
        hermanos validos sigan disponibles, y una invalidacion global seria la
        forma mas facil de tirar trabajo que nadie pidio rehacer.
        """
        proyecto = self._proyecto.informacion().project_id
        invalidados: list[str] = []
        try:
            with self._proyecto.transaccion() as tx:
                vigente = tx.execute(
                    "SELECT state FROM SourceAsset WHERE id=?", (anterior_id,)).fetchone()
                if vigente is None:
                    raise ErrorFuenteIngesta(
                        f"No existe la fuente {anterior_id!r} a la que suceder.")
                if vigente[0] == EstadoFuente.REEMPLAZADA.value:
                    raise ErrorFuenteIngesta(
                        f"La fuente {anterior_id!r} ya fue reemplazada; no se puede suceder dos veces.")
                filas = tx.execute(
                    "SELECT id, materialization_key FROM AnalysisArtifact"
                    " WHERE source_id=? AND state=? ORDER BY rowid",
                    (anterior_id, ESTADO_DISPONIBLE)).fetchall()
                self._insertar(tx, nueva, proyecto, anterior_id)
                tx.execute("UPDATE SourceAsset SET state=? WHERE id=?",
                           (EstadoFuente.REEMPLAZADA.value, anterior_id))
                tx.execute("UPDATE AnalysisArtifact SET state=? WHERE source_id=? AND state=?",
                           (ESTADO_RECONSTRUIBLE, anterior_id, ESTADO_DISPONIBLE))
                invalidados = [fila[1] or fila[0] for fila in filas]
        except ErrorProyecto as error:
            causa = error.__cause__
            if isinstance(causa, sqlite3.IntegrityError):
                raise ErrorFuenteIngesta(
                    f"El proyecto ya conoce una fuente con la identidad"
                    f" {nueva.huella.identidad!r}.") from error
            raise ErrorIngesta(
                f"No se pudo suceder a la fuente {anterior_id!r}.") from error
        self._publicar_referencias_derivadas()
        # Se ordenan y deduplican: varios archivos de un mismo artefacto
        # comparten clave y la lista es un diagnostico, no un inventario.
        return nueva, tuple(sorted(set(invalidados)))

    # ------------------------------------------------------------------ #
    # Artefactos
    # ------------------------------------------------------------------ #

    def artefacto(self, clave: str) -> RegistroArtefacto | None:
        filas = self._consultar(
            f"{_SELECT_ARTEFACTO} WHERE materialization_key=? ORDER BY rowid", (clave,))
        if not filas:
            return None
        linaje_bruto = _documento(filas[0][12], "lineage_json")
        try:
            linaje = LinajeArtefacto(
                stage=_texto(linaje_bruto.get("stage"), "linaje.stage"),
                version_contrato=_texto(linaje_bruto.get("version_contrato"),
                                        "linaje.version_contrato"),
                version_proveedor=_texto(linaje_bruto.get("version_proveedor"),
                                         "linaje.version_proveedor"),
                clave=_texto(linaje_bruto.get("clave"), "linaje.clave"),
                entradas=linaje_bruto.get("entradas") or {})
        except Exception as error:
            raise ErrorIngesta(
                f"El artefacto {clave!r} guarda un linaje ilegible.") from error
        estados = {_texto(fila[5], "state") for fila in filas}
        return RegistroArtefacto(
            clave=clave, tipo=_texto(filas[0][4], "kind"),
            rutas=tuple(_texto(fila[3], "artifact_path") for fila in filas),
            checksums={_texto(fila[3], "artifact_path"): _texto(fila[7], "checksum")
                       for fila in filas},
            linaje=linaje,
            # Basta que un archivo del conjunto haya caducado para que el
            # artefacto entero deje de ser reutilizable: publicar es atomico y
            # medio artefacto no es una entrada valida de nada.
            estado=ESTADO_DISPONIBLE if estados == {ESTADO_DISPONIBLE} else ESTADO_RECONSTRUIBLE,
            fuente_id=filas[0][1])

    def registrar_artefacto(self, registro: RegistroArtefacto,
                            bytes_totales: int = 0) -> RegistroArtefacto:
        """Alta idempotente del artefacto publicado bajo una clave."""
        linaje = registro.linaje.como_documento()
        try:
            with self._proyecto.transaccion() as tx:
                tx.execute("DELETE FROM AnalysisArtifact WHERE materialization_key=?",
                           (registro.clave,))
                for ruta in registro.rutas:
                    tx.execute(
                        "INSERT INTO AnalysisArtifact (id, source_id, relative_path, kind, state,"
                        " job_id, materialization_key, artifact_path, checksum, bytes, stage,"
                        " contract_version, provider_version, lineage_json)"
                        " VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (str(uuid.uuid4()), registro.fuente_id,
                         ruta_publicada(registro.clave, ruta), registro.tipo, registro.estado,
                         registro.clave, ruta, registro.checksums.get(ruta, ""), bytes_totales,
                         registro.linaje.stage, registro.linaje.version_contrato,
                         registro.linaje.version_proveedor,
                         json.dumps(linaje, ensure_ascii=False, sort_keys=True)))
        except ErrorProyecto as error:
            raise ErrorIngesta(
                f"No se pudo registrar el artefacto {registro.clave!r}.") from error
        return registro

    def artefactos_de(self, fuente_id: str) -> tuple[tuple[str, str], ...]:
        """Clave y estado de cada artefacto que cuelga de una fuente."""
        return tuple((fila[0] or "", fila[1]) for fila in self._consultar(
            "SELECT materialization_key, state FROM AnalysisArtifact WHERE source_id=?"
            " ORDER BY rowid", (fuente_id,)))


def ubicacion_de(fuente: SourceAsset, raiz: str) -> UbicacionFuente:
    """Ubicacion equivalente a la de una fuente ya registrada."""
    if fuente.ruta_relativa is not None:
        return UbicacionFuente(ruta_absoluta=f"{raiz}/{fuente.ruta_relativa}",
                               ruta_relativa=fuente.ruta_relativa, nombre=fuente.nombre)
    return UbicacionFuente(ruta_absoluta=str(fuente.ruta_externa), ruta_relativa=None,
                           nombre=fuente.nombre)


def checksums_de(registro: RegistroArtefacto) -> Mapping[str, str]:
    return dict(registro.checksums)
