"""Repositorio de edicion sobre las tablas Draft/Variant/EditRevision de v6.

El esquema v6 ya tiene las tres tablas y las claves foraneas de pertenencia,
pero no columnas para las aristas del grafo. Esa metadata viaja en un sobre
canonico dentro de ``EditRevision.document_json``. El decoder conserva
compatibilidad con filas anteriores que guardaban solo el documento.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import sqlite3

from clipperkick.domain.edit.errors import ErrorHistorialEdicion
from clipperkick.domain.edit.history import Draft, EditRevision, Variant
from clipperkick.domain.edit.schema import deserializar, texto_canonico
from clipperkick.infrastructure.project.persistence import RepositorioSqliteProyecto


FORMATO_REVISION = "clipperkick-edit-revision/1"


def _codificar(revision: EditRevision) -> str:
    return texto_canonico({
        "format": FORMATO_REVISION,
        "metadata": {
            "variant_id": revision.variant_id,
            "parent_revision_id": revision.parent_revision_id,
            "reason": revision.reason,
            "group": revision.group,
            "created_at": revision.created_at,
        },
        "document": revision.document.como_documento(),
    })


def _decodificar(fila: tuple[object, ...]) -> EditRevision:
    revision_id, draft_id, contenido, created_at = fila
    if not all(isinstance(valor, str) for valor in (revision_id, draft_id, contenido, created_at)):
        raise ErrorHistorialEdicion("Una revision guardada tiene columnas ilegibles.")
    try:
        datos = json.loads(contenido)
    except ValueError as error:
        raise ErrorHistorialEdicion(f"La revision {revision_id!r} no contiene JSON legible.") from error

    if isinstance(datos, Mapping) and datos.get("format") == FORMATO_REVISION:
        metadata, document_data = datos.get("metadata"), datos.get("document")
        if not isinstance(metadata, Mapping):
            raise ErrorHistorialEdicion(f"La revision {revision_id!r} no contiene metadata legible.")
        try:
            document = deserializar(texto_canonico(document_data))  # type: ignore[arg-type]
            if document.revision_id is None:
                document = document.con(revision_id=revision_id)
            elif document.revision_id != revision_id:
                raise ErrorHistorialEdicion(
                    f"La revision {revision_id!r} contiene otra identidad canonica.")
            return EditRevision(
                id=revision_id, draft_id=draft_id,
                variant_id=metadata.get("variant_id"),
                parent_revision_id=metadata.get("parent_revision_id"),
                document=document,
                created_at=metadata.get("created_at", created_at),
                reason=metadata.get("reason", "edicion"),
                group=metadata.get("group"),
            )
        except (TypeError, ValueError) as error:
            raise ErrorHistorialEdicion(
                f"La revision {revision_id!r} tiene un sobre invalido.") from error

    # Compatibilidad con una fila v6 temprana: document_json era el documento
    # directamente. No se inventa filiacion que los bytes no declaran.
    document = deserializar(contenido)
    if document.revision_id is None:
        document = document.con(revision_id=revision_id)
    elif document.revision_id != revision_id:
        raise ErrorHistorialEdicion(
            f"La revision {revision_id!r} contiene otra identidad canonica.")
    return EditRevision(
        id=revision_id, draft_id=draft_id, variant_id=None, parent_revision_id=None,
        document=document, created_at=created_at,
        reason="migrada", group=None,
    )


class RepositorioSqliteEdicion:
    def __init__(self, proyecto: RepositorioSqliteProyecto) -> None:
        self.proyecto = proyecto

    @property
    def _conexion(self) -> sqlite3.Connection:
        return self.proyecto.conexion

    def _una_revision(self, revision_id: str) -> tuple[object, ...]:
        try:
            fila = self._conexion.execute(
                "SELECT id, draft_id, document_json, created_at FROM EditRevision WHERE id=?",
                (revision_id,),
            ).fetchone()
        except sqlite3.Error as error:
            raise ErrorHistorialEdicion("No se pudo consultar la revision.") from error
        if fila is None:
            raise ErrorHistorialEdicion(f"No existe la revision {revision_id!r}.")
        return fila

    def obtener_revision(self, revision_id: str) -> EditRevision:
        return _decodificar(self._una_revision(revision_id))

    def obtener_draft(self, draft_id: str) -> Draft:
        try:
            fila = self._conexion.execute(
                "SELECT id, moment_id, current_revision_id FROM Draft WHERE id=?", (draft_id,),
            ).fetchone()
        except sqlite3.Error as error:
            raise ErrorHistorialEdicion("No se pudo consultar el borrador.") from error
        if fila is None:
            raise ErrorHistorialEdicion(f"No existe el borrador {draft_id!r}.")
        if fila[1] is None or fila[2] is None:
            raise ErrorHistorialEdicion(f"El borrador {draft_id!r} esta incompleto.")
        return Draft(fila[0], fila[1], fila[2])

    def obtener_variant(self, variant_id: str) -> Variant:
        try:
            fila = self._conexion.execute(
                "SELECT id, draft_id, name, current_revision_id FROM Variant WHERE id=?",
                (variant_id,),
            ).fetchone()
        except sqlite3.Error as error:
            raise ErrorHistorialEdicion("No se pudo consultar la variante.") from error
        if fila is None:
            raise ErrorHistorialEdicion(f"No existe la variante {variant_id!r}.")
        if fila[3] is None:
            raise ErrorHistorialEdicion(f"La variante {variant_id!r} no apunta a una revision.")
        return Variant(fila[0], fila[1], fila[2], fila[3])

    def crear_borrador(self, draft: Draft, variant: Variant, revision: EditRevision) -> None:
        if variant.draft_id != draft.id or revision.draft_id != draft.id:
            raise ErrorHistorialEdicion("El borrador, la variante y la revision no pertenecen juntos.")
        if draft.current_revision_id != revision.id or variant.current_revision_id != revision.id:
            raise ErrorHistorialEdicion("El borrador nuevo tiene punteros iniciales contradictorios.")
        if revision.parent_revision_id is not None or revision.variant_id != variant.id:
            raise ErrorHistorialEdicion("La primera revision tiene que ser la raiz de su variante.")
        try:
            with self.proyecto.transaccion() as conexion:
                # El NULL temporal evita depender de claves foraneas diferidas:
                # primero existe el Draft, luego su revision, al final el puntero.
                conexion.execute(
                    "INSERT INTO Draft (id, moment_id, current_revision_id) VALUES (?, ?, NULL)",
                    (draft.id, draft.moment_id),
                )
                conexion.execute(
                    "INSERT INTO EditRevision (id, draft_id, document_json, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (revision.id, draft.id, _codificar(revision), revision.created_at),
                )
                conexion.execute(
                    "UPDATE Draft SET current_revision_id=? WHERE id=?",
                    (revision.id, draft.id),
                )
                conexion.execute(
                    "INSERT INTO Variant (id, draft_id, name, current_revision_id)"
                    " VALUES (?, ?, ?, ?)",
                    (variant.id, draft.id, variant.name, revision.id),
                )
        except ErrorHistorialEdicion:
            raise
        except Exception as error:
            raise ErrorHistorialEdicion("No se pudo crear el borrador atomico.") from error

    def confirmar_revision(self, variant_id: str, expected_revision_id: str,
                            revision: EditRevision) -> Variant:
        variant = self.obtener_variant(variant_id)
        if variant.current_revision_id != expected_revision_id:
            raise ErrorHistorialEdicion("La variante cambio mientras se preparaba la revision.")
        if (revision.draft_id != variant.draft_id or revision.variant_id != variant.id
                or revision.parent_revision_id != expected_revision_id):
            raise ErrorHistorialEdicion("La revision nueva no prolonga la variante indicada.")
        parent = self.obtener_revision(expected_revision_id)
        if parent.draft_id != variant.draft_id:
            raise ErrorHistorialEdicion("La revision padre pertenece a otro borrador.")
        try:
            with self.proyecto.transaccion() as conexion:
                conexion.execute(
                    "INSERT INTO EditRevision (id, draft_id, document_json, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (revision.id, revision.draft_id, _codificar(revision), revision.created_at),
                )
                cursor = conexion.execute(
                    "UPDATE Variant SET current_revision_id=?"
                    " WHERE id=? AND current_revision_id=?",
                    (revision.id, variant.id, expected_revision_id),
                )
                if cursor.rowcount != 1:
                    raise ErrorHistorialEdicion(
                        "La variante cambio mientras se confirmaba la revision.")
                conexion.execute(
                    "UPDATE Draft SET current_revision_id=? WHERE id=?",
                    (revision.id, variant.draft_id),
                )
        except ErrorHistorialEdicion:
            raise
        except Exception as error:
            raise ErrorHistorialEdicion("No se pudo confirmar la revision atomica.") from error
        return Variant(variant.id, variant.draft_id, variant.name, revision.id)

    def mover_puntero(self, variant_id: str, expected_revision_id: str,
                       target_revision_id: str) -> Variant:
        variant = self.obtener_variant(variant_id)
        target = self.obtener_revision(target_revision_id)
        if target.draft_id != variant.draft_id:
            raise ErrorHistorialEdicion("La revision destino pertenece a otro borrador.")
        try:
            with self.proyecto.transaccion() as conexion:
                cursor = conexion.execute(
                    "UPDATE Variant SET current_revision_id=?"
                    " WHERE id=? AND current_revision_id=?",
                    (target.id, variant.id, expected_revision_id),
                )
                if cursor.rowcount != 1:
                    raise ErrorHistorialEdicion(
                        "La variante cambio mientras se movia su puntero.")
                conexion.execute(
                    "UPDATE Draft SET current_revision_id=? WHERE id=?",
                    (target.id, variant.draft_id),
                )
        except ErrorHistorialEdicion:
            raise
        except Exception as error:
            raise ErrorHistorialEdicion("No se pudo mover el puntero de revision.") from error
        return Variant(variant.id, variant.draft_id, variant.name, target.id)

    def duplicar_variant(self, variant: Variant,
                         fork_revision: EditRevision | None = None) -> None:
        if fork_revision is None:
            revision = self.obtener_revision(variant.current_revision_id)
            if revision.draft_id != variant.draft_id:
                raise ErrorHistorialEdicion("La variante duplicada apunta a otro borrador.")
        else:
            if (fork_revision.id != variant.current_revision_id
                    or fork_revision.draft_id != variant.draft_id
                    or fork_revision.variant_id != variant.id
                    or fork_revision.parent_revision_id is None):
                raise ErrorHistorialEdicion("La revision de ramificacion contradice la variante.")
            parent = self.obtener_revision(fork_revision.parent_revision_id)
            if parent.draft_id != variant.draft_id:
                raise ErrorHistorialEdicion("La ramificacion parte de otro borrador.")
        try:
            with self.proyecto.transaccion() as conexion:
                if fork_revision is not None:
                    conexion.execute(
                        "INSERT INTO EditRevision (id, draft_id, document_json, created_at)"
                        " VALUES (?, ?, ?, ?)",
                        (fork_revision.id, fork_revision.draft_id,
                         _codificar(fork_revision), fork_revision.created_at),
                    )
                conexion.execute(
                    "INSERT INTO Variant (id, draft_id, name, current_revision_id)"
                    " VALUES (?, ?, ?, ?)",
                    (variant.id, variant.draft_id, variant.name, variant.current_revision_id),
                )
                conexion.execute(
                    "UPDATE Draft SET current_revision_id=? WHERE id=?",
                    (variant.current_revision_id, variant.draft_id),
                )
        except Exception as error:
            raise ErrorHistorialEdicion("No se pudo duplicar la variante.") from error

    def duplicar_variante(self, variant: Variant,
                          fork_revision: EditRevision | None = None) -> None:
        self.duplicar_variant(variant, fork_revision)

    def _revisiones_del_draft(self, draft_id: str) -> tuple[EditRevision, ...]:
        try:
            filas = self._conexion.execute(
                "SELECT id, draft_id, document_json, created_at FROM EditRevision"
                " WHERE draft_id=? ORDER BY created_at, rowid",
                (draft_id,),
            ).fetchall()
        except sqlite3.Error as error:
            raise ErrorHistorialEdicion("No se pudo consultar el historial.") from error
        return tuple(_decodificar(fila) for fila in filas)

    def hijos(self, revision_id: str, variant_id: str) -> tuple[EditRevision, ...]:
        variant = self.obtener_variant(variant_id)
        children = (
            revision for revision in self._revisiones_del_draft(variant.draft_id)
            if revision.parent_revision_id == revision_id and revision.variant_id == variant_id
        )
        return tuple(children)

    def historial(self, variant_id: str) -> tuple[EditRevision, ...]:
        variant = self.obtener_variant(variant_id)
        by_id = {revision.id: revision
                 for revision in self._revisiones_del_draft(variant.draft_id)}
        chain: list[EditRevision] = []
        seen: set[str] = set()
        current_id: str | None = variant.current_revision_id
        while current_id is not None:
            if current_id in seen:
                raise ErrorHistorialEdicion("El historial contiene un ciclo.")
            seen.add(current_id)
            revision = by_id.get(current_id)
            if revision is None:
                raise ErrorHistorialEdicion("El historial apunta a una revision inexistente.")
            chain.append(revision)
            current_id = revision.parent_revision_id
        chain.reverse()
        return tuple(chain)

    def revision_inicial(self, draft_id: str) -> EditRevision:
        roots = tuple(revision for revision in self._revisiones_del_draft(draft_id)
                      if revision.parent_revision_id is None)
        if len(roots) != 1:
            raise ErrorHistorialEdicion(
                f"El borrador {draft_id!r} no tiene una unica revision inicial.")
        return roots[0]


__all__ = ["FORMATO_REVISION", "RepositorioSqliteEdicion"]
