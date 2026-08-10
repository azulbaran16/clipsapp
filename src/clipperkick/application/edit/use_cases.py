"""Casos de uso de revisiones, branching y autoguardado agrupado."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import uuid

from clipperkick.domain.edit.analysis import fusionar_analisis, restaurar_automatico
from clipperkick.domain.edit.document import EditDocument
from clipperkick.domain.edit.errors import ErrorHistorialEdicion
from clipperkick.domain.edit.history import Draft, EditRevision, Variant
from clipperkick.domain.edit.patches import Patch, aplicar_patches
from clipperkick.domain.edit.schema import serializar

from .ports import RepositorioEdicion


GenerarId = Callable[[], str]
Ahora = Callable[[], str]


def _uuid() -> str:
    return str(uuid.uuid4())


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class BorradorCreado:
    draft: Draft
    variant: Variant
    revision: EditRevision


class ServicioEdicion:
    """Coordina snapshots inmutables; el repositorio confirma cada cambio atomico."""

    def __init__(self, repositorio: RepositorioEdicion, generar_id: GenerarId = _uuid,
                 ahora: Ahora = _ahora) -> None:
        self.repositorio = repositorio
        self._generar_id = generar_id
        self._ahora = ahora

    def crear_borrador(self, moment_id: str, document: EditDocument,
                       name: str = "Principal") -> BorradorCreado:
        draft_id, variant_id, revision_id = (
            self._generar_id(), self._generar_id(), self._generar_id())
        revision = EditRevision(
            id=revision_id, draft_id=draft_id, variant_id=variant_id,
            parent_revision_id=None, document=document.con(revision_id=revision_id),
            created_at=self._ahora(),
            reason="automatico", group="creacion",
        )
        draft = Draft(draft_id, moment_id, revision_id)
        variant = Variant(variant_id, draft_id, name, revision_id)
        self.repositorio.crear_borrador(draft, variant, revision)
        return BorradorCreado(draft, variant, revision)

    def confirmar(self, variant_id: str, patches: Sequence[Patch],
                  *, group: str | None = None, reason: str = "edicion") -> EditRevision:
        if not patches:
            raise ErrorHistorialEdicion("No hay cambios que confirmar.")
        variant = self.repositorio.obtener_variant(variant_id)
        parent = self.repositorio.obtener_revision(variant.current_revision_id)
        document = aplicar_patches(parent.document, patches)
        return self._confirmar_documento(variant, parent, document, group=group, reason=reason)

    def fusionar_nuevo_analisis(self, variant_id: str,
                                propuesta: EditDocument) -> EditRevision:
        variant = self.repositorio.obtener_variant(variant_id)
        parent = self.repositorio.obtener_revision(variant.current_revision_id)
        document = fusionar_analisis(parent.document, propuesta)
        return self._confirmar_documento(
            variant, parent, document, group="analisis", reason="rerun_analisis")

    def restaurar_automatico(self, variant_id: str) -> EditRevision:
        variant = self.repositorio.obtener_variant(variant_id)
        parent = self.repositorio.obtener_revision(variant.current_revision_id)
        original = self.repositorio.revision_inicial(variant.draft_id)
        document = restaurar_automatico(parent.document, original.document)
        return self._confirmar_documento(
            variant, parent, document, group="restaurar", reason="restaurar_automatico")

    def _confirmar_documento(self, variant: Variant, parent: EditRevision,
                             document: EditDocument, *, group: str | None,
                             reason: str) -> EditRevision:
        if (serializar(document.con(revision_id=None))
                == serializar(parent.document.con(revision_id=None))):
            raise ErrorHistorialEdicion("El cambio no modifica el documento actual.")
        revision_id = self._generar_id()
        revision = EditRevision(
            id=revision_id, draft_id=variant.draft_id, variant_id=variant.id,
            parent_revision_id=parent.id, document=document.con(revision_id=revision_id),
            created_at=self._ahora(),
            reason=reason, group=group,
        )
        self.repositorio.confirmar_revision(variant.id, parent.id, revision)
        return revision

    def deshacer(self, variant_id: str) -> EditRevision:
        variant = self.repositorio.obtener_variant(variant_id)
        current = self.repositorio.obtener_revision(variant.current_revision_id)
        if current.parent_revision_id is None:
            raise ErrorHistorialEdicion("La variante ya esta en su revision inicial.")
        target = self.repositorio.obtener_revision(current.parent_revision_id)
        self.repositorio.mover_puntero(variant.id, current.id, target.id)
        return target

    def rehacer(self, variant_id: str) -> EditRevision:
        variant = self.repositorio.obtener_variant(variant_id)
        children = self.repositorio.hijos(variant.current_revision_id, variant.id)
        if not children:
            raise ErrorHistorialEdicion("La variante no tiene una revision que rehacer.")
        # El ultimo hijo es la rama activa mas reciente. Una edicion tras undo
        # conserva la rama anterior como historia, pero redo sigue la nueva.
        target = children[-1]
        self.repositorio.mover_puntero(variant.id, variant.current_revision_id, target.id)
        return target

    def duplicar_variant(self, variant_id: str, name: str) -> Variant:
        origin = self.repositorio.obtener_variant(variant_id)
        head = self.repositorio.obtener_revision(origin.current_revision_id)
        duplicate_id = self._generar_id()
        fork: EditRevision | None = None
        current_revision_id = origin.current_revision_id
        if head.parent_revision_id is not None:
            # Materializa el punto de ramificacion como revision equivalente,
            # hija del mismo padre que la cabeza original. Asi undo/redo de la
            # variante nueva tiene una ruta propia desde el primer instante.
            fork_id = self._generar_id()
            fork = EditRevision(
                id=fork_id, draft_id=origin.draft_id, variant_id=duplicate_id,
                parent_revision_id=head.parent_revision_id,
                document=head.document.con(revision_id=fork_id), created_at=self._ahora(),
                reason="duplicar_variante", group="branch",
            )
            current_revision_id = fork_id
        duplicate = Variant(
            id=duplicate_id, draft_id=origin.draft_id, name=name,
            current_revision_id=current_revision_id,
        )
        self.repositorio.duplicar_variant(duplicate, fork)
        return duplicate

    def duplicar_variante(self, variant_id: str, name: str) -> Variant:
        """Alias en castellano para consumidores de la capa de aplicacion."""
        return self.duplicar_variant(variant_id, name)

    def historial(self, variant_id: str) -> tuple[EditRevision, ...]:
        return self.repositorio.historial(variant_id)

    def autoguardado(self, variant_id: str) -> "AutoguardadoAgrupado":
        return AutoguardadoAgrupado(self, variant_id)


class AutoguardadoAgrupado:
    """Agrupa una rafaga del mismo control en una unica revision.

    Cambiar de grupo confirma el grupo anterior. ``flush`` cierra la rafaga
    pendiente (por timeout, blur, cierre o una accion explicita de la UI).
    """

    def __init__(self, servicio: ServicioEdicion, variant_id: str) -> None:
        self._servicio = servicio
        self._variant_id = variant_id
        self._group: str | None = None
        self._patches: list[Patch] = []

    @property
    def pending(self) -> bool:
        return bool(self._patches)

    def add(self, patch: Patch) -> EditRevision | None:
        if not isinstance(patch, Patch):
            raise ErrorHistorialEdicion("El autoguardado solo admite patches tipados.")
        saved = None
        if self._patches and patch.grupo != self._group:
            saved = self.flush()
        self._group = patch.grupo
        self._patches.append(patch)
        return saved

    def flush(self) -> EditRevision | None:
        if not self._patches:
            return None
        patches, group = tuple(self._patches), self._group
        revision = self._servicio.confirmar(
            self._variant_id, patches, group=group, reason="autoguardado")
        # Solo se descarta la rafaga despues del commit. Si el repositorio
        # falla, la UI puede reintentar sin reconstruir los patches perdidos.
        self._patches.clear()
        self._group = None
        return revision


__all__ = ["AutoguardadoAgrupado", "BorradorCreado", "ServicioEdicion"]
