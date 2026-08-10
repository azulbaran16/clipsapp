"""Modelo puro de ingesta: fuentes, huellas, metadata, claves y artefactos."""

from .artifacts import (
    CLAVE_CHECKSUMS, ESTADO_DISPONIBLE, ESTADO_RECONSTRUIBLE, PREFIJO_CHECKSUM, ArchivoArtefacto,
    LinajeArtefacto, ManifiestoArtefacto, RegistroArtefacto, etiquetar_checksum, reutilizable,
    rutas_declaradas,
)
from .cache import (
    PREFIJO_CLAVE, VERSION_CLAVE, canonicalizar, clave_materializacion, es_clave_materializacion,
)
from .errors import (
    ErrorArtefactoIngesta, ErrorCancelacionIngesta, ErrorClaveMaterializacion,
    ErrorDescargaIngesta, ErrorEvidenciaHuella, ErrorFuenteIngesta, ErrorHuellaIngesta,
    ErrorHuellaInestable, ErrorIngesta, ErrorMediaDanada, ErrorMetadataMedia,
)
from .fingerprint import (
    ALGORITMO, BLOQUE_LECTURA, TAMANO_VENTANA, VERSION_HUELLA, HuellaFuente, Ventana,
    componer_completa, componer_parcial, detecta_reemplazo, digest_bloque,
    nuevo_acumulador, plan_parcial,
)
from .media import (
    CAMPOS_VOLATILES_FORMATO, ROTACIONES_RECTAS, TOLERANCIA_FPS, VERSION_METADATA, AvisoMedia,
    FlujoAudio, FlujoVideo, MetadataMedia, Racional, leer_racional, normalizar_sondeo,
    sondeo_canonico,
)
from .models import (
    CODIGO_ARTEFACTO_PUBLICADO, CODIGO_ARTEFACTO_REUTILIZADO, CODIGO_AVISO_MEDIA,
    CODIGO_COPIA_COMPLETA, CODIGO_DESCARGA_COMPLETA, CODIGO_DESCENDIENTES_INVALIDADOS,
    CODIGO_DESCARGA_CANCELADA, CODIGO_FUENTE_REEMPLAZADA, CODIGO_FUENTE_REGISTRADA,
    CODIGO_ORIGEN_NO_RETIRADO,
    CODIGO_FUENTE_RELOCALIZADA, CODIGO_FUENTE_REPARADA, CODIGO_FUENTE_REUTILIZADA,
    CODIGO_SONDEO_COMPLETO, CODIGO_TEMPORALES_RETIRADOS,
    DiagnosticoIngesta, EstadoFuente, ModoFuente, OrigenFuente, SourceAsset,
    referencia_de_apertura,
)

__all__ = [
    "ALGORITMO", "AvisoMedia", "ArchivoArtefacto", "BLOQUE_LECTURA", "CAMPOS_VOLATILES_FORMATO",
    "CLAVE_CHECKSUMS", "CODIGO_ARTEFACTO_PUBLICADO", "CODIGO_ARTEFACTO_REUTILIZADO",
    "CODIGO_AVISO_MEDIA", "CODIGO_COPIA_COMPLETA", "CODIGO_DESCARGA_COMPLETA",
    "CODIGO_DESCENDIENTES_INVALIDADOS", "CODIGO_FUENTE_REEMPLAZADA", "CODIGO_FUENTE_REGISTRADA",
    "CODIGO_DESCARGA_CANCELADA", "CODIGO_FUENTE_RELOCALIZADA", "CODIGO_FUENTE_REPARADA",
    "CODIGO_FUENTE_REUTILIZADA", "CODIGO_ORIGEN_NO_RETIRADO", "CODIGO_SONDEO_COMPLETO",
    "CODIGO_TEMPORALES_RETIRADOS",
    "DiagnosticoIngesta", "ESTADO_DISPONIBLE", "ESTADO_RECONSTRUIBLE", "ErrorArtefactoIngesta",
    "ErrorCancelacionIngesta",
    "ErrorClaveMaterializacion", "ErrorDescargaIngesta", "ErrorEvidenciaHuella",
    "ErrorFuenteIngesta", "ErrorHuellaIngesta",
    "ErrorHuellaInestable",
    "ErrorIngesta", "ErrorMediaDanada", "ErrorMetadataMedia", "EstadoFuente", "FlujoAudio",
    "FlujoVideo", "HuellaFuente", "LinajeArtefacto", "ManifiestoArtefacto", "MetadataMedia",
    "ModoFuente", "OrigenFuente", "PREFIJO_CHECKSUM", "PREFIJO_CLAVE", "ROTACIONES_RECTAS",
    "Racional", "RegistroArtefacto", "SourceAsset", "TAMANO_VENTANA", "TOLERANCIA_FPS",
    "VERSION_CLAVE", "VERSION_HUELLA", "VERSION_METADATA", "Ventana", "canonicalizar",
    "clave_materializacion", "componer_completa", "componer_parcial", "detecta_reemplazo",
    "digest_bloque", "es_clave_materializacion", "etiquetar_checksum", "leer_racional",
    "nuevo_acumulador",
    "normalizar_sondeo", "plan_parcial", "referencia_de_apertura", "reutilizable",
    "rutas_declaradas", "sondeo_canonico",
]
