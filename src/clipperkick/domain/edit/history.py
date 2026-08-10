"""Entidades inmutables del grafo de revisiones de una edicion."""

from __future__ import annotations

from dataclasses import dataclass

from .document import EditDocument
from .errors import ErrorHistorialEdicion
from .timebase import exigir_identificador


def _opcional(valor: object, campo: str) -> str | None:
    if valor is None:
        return None
    return exigir_identificador(valor, campo)


@dataclass(frozen=True)
class Draft:
    """Borrador persistente ligado a un momento y a una revision visible."""

    id: str
    moment_id: str
    current_revision_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", exigir_identificador(self.id, "draft.id"))
        object.__setattr__(self, "moment_id", exigir_identificador(self.moment_id, "draft.moment_id"))
        object.__setattr__(
            self, "current_revision_id",
            exigir_identificador(self.current_revision_id, "draft.current_revision_id"),
        )


@dataclass(frozen=True)
class Variant:
    """Rama nombrada cuyo puntero puede recorrer revisiones sin mutarlas."""

    id: str
    draft_id: str
    name: str
    current_revision_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", exigir_identificador(self.id, "variant.id"))
        object.__setattr__(self, "draft_id", exigir_identificador(self.draft_id, "variant.draft_id"))
        if not isinstance(self.name, str) or not self.name.strip():
            raise ErrorHistorialEdicion("La variante necesita un nombre visible.")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(
            self, "current_revision_id",
            exigir_identificador(self.current_revision_id, "variant.current_revision_id"),
        )


@dataclass(frozen=True)
class EditRevision:
    """Snapshot append-only y su arista hacia el padre.

    La revision inicial no tiene padre. Al duplicar desde una cabeza no raiz se
    crea un snapshot equivalente en la rama nueva, con el mismo padre: eso da a
    undo/redo un camino independiente sin copiar ningun archivo de media.
    """

    id: str
    draft_id: str
    variant_id: str | None
    parent_revision_id: str | None
    document: EditDocument
    created_at: str
    reason: str = "edicion"
    group: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", exigir_identificador(self.id, "revision.id"))
        object.__setattr__(self, "draft_id", exigir_identificador(self.draft_id, "revision.draft_id"))
        object.__setattr__(self, "variant_id", _opcional(self.variant_id, "revision.variant_id"))
        object.__setattr__(
            self, "parent_revision_id",
            _opcional(self.parent_revision_id, "revision.parent_revision_id"),
        )
        if self.parent_revision_id == self.id:
            raise ErrorHistorialEdicion("Una revision no puede ser su propio padre.")
        if not isinstance(self.document, EditDocument):
            raise ErrorHistorialEdicion("La revision necesita un EditDocument valido.")
        if self.document.revision_id != self.id:
            raise ErrorHistorialEdicion(
                "La identidad canonica del documento no coincide con su revision.")
        if not isinstance(self.created_at, str) or not self.created_at.strip():
            raise ErrorHistorialEdicion("La revision necesita una fecha estable.")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise ErrorHistorialEdicion("La revision necesita una razon.")
        object.__setattr__(self, "reason", self.reason.strip())
        object.__setattr__(self, "group", _opcional(self.group, "revision.group"))


__all__ = ["Draft", "EditRevision", "Variant"]
