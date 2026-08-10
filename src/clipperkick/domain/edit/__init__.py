"""Documento declarativo, patches e historial de edicion."""

from .analysis import fusionar_analisis, restaurar_automatico
from .document import EditDocument, validar_assets
from .errors import (
    ErrorAssetEdicion, ErrorCatalogoEdicion, ErrorDocumentoEdicion, ErrorEdicion,
    ErrorEsquemaEdicion, ErrorHistorialEdicion, ErrorPatchEdicion,
)
from .history import Draft, EditRevision, Variant
from .patches import Patch, aplicar_patches, impacto_de
from .schema import deserializar, migrar_documento, serializar

__all__ = [
    "Draft", "EditDocument", "EditRevision", "ErrorAssetEdicion", "ErrorCatalogoEdicion",
    "ErrorDocumentoEdicion", "ErrorEdicion", "ErrorEsquemaEdicion", "ErrorHistorialEdicion",
    "ErrorPatchEdicion", "Patch", "Variant", "aplicar_patches", "deserializar",
    "fusionar_analisis", "impacto_de", "migrar_documento", "restaurar_automatico",
    "serializar", "validar_assets",
]
