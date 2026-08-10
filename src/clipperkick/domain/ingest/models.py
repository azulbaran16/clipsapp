"""Modelo puro de una fuente incorporada al proyecto.

`SourceAsset` es el punto en el que una entrada del mundo —una ruta local o una
URL— deja de serlo y pasa a ser algo que el proyecto puede volver a encontrar,
verificar y reconstruir. Por eso su invariante no es "tiene los campos
rellenos" sino **es localizable y verificable**:

- una fuente copiada vive dentro del proyecto y se nombra con una ruta relativa
  confinada, nunca absoluta: el proyecto tiene que poder moverse de carpeta y de
  maquina sin reescribir nada;
- una fuente referenciada vive fuera y conserva su ruta ya normalizada, su
  tamano y su mtime, porque son las tres senales baratas con las que la
  siguiente apertura decide si sigue siendo la misma;
- en ambos casos la huella completa es obligatoria. Sin ella la fuente no tiene
  identidad y ningun artefacto derivado podria declararse reutilizable.

Los estados son cerrados a proposito. `missing` y `replaced` no son fallos: son
situaciones que la UI debe poder mostrar y de las que se sale relocalizando o
reingiriendo, sin perder analisis ni ediciones.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum

from clipperkick.domain.jobs.errors import ErrorIdentificadorJob
from clipperkick.domain.jobs.identity import exigir_ruta_interna

from .errors import ErrorFuenteIngesta
from .fingerprint import HuellaFuente


class ModoFuente(str, Enum):
    """Donde viven los bytes. Los valores son los que T02 ya persiste."""

    COPIADA = "copied"
    REFERENCIADA = "referenced"


class OrigenFuente(str, Enum):
    """De donde llegaron. No cambia el modelo, solo explica su procedencia."""

    LOCAL = "local"
    DESCARGA = "download"


class EstadoFuente(str, Enum):
    DISPONIBLE = "available"
    AUSENTE = "missing"
    REEMPLAZADA = "replaced"


# Codigos de diagnostico. Son parte del contrato observable: la UI traduce
# claves, nunca interpreta el texto libre que las acompana.
CODIGO_FUENTE_REGISTRADA = "ingesta.fuente_registrada"
CODIGO_FUENTE_REUTILIZADA = "ingesta.fuente_reutilizada"
CODIGO_FUENTE_RELOCALIZADA = "ingesta.fuente_relocalizada"
#: La fila conocida apuntaba a bytes que ya no son los suyos. No es una
#: reutilizacion —lo guardado estaba mal— y por eso tiene codigo propio: quien
#: lea el diagnostico tiene que poder distinguir "acerte en el cache" de
#: "encontre una copia corrupta y la sustitui por el material correcto".
CODIGO_FUENTE_REPARADA = "ingesta.fuente_reparada"
CODIGO_DESCARGA_CANCELADA = "ingesta.descarga_cancelada"
#: El traslado termino bien pero el original no se pudo retirar. No es un
#: fallo de la ingesta —el destino esta confirmado— y tampoco es silencio:
#: quien lo lea sabe que quedo una copia y donde.
CODIGO_ORIGEN_NO_RETIRADO = "ingesta.origen_no_retirado"
CODIGO_FUENTE_REEMPLAZADA = "ingesta.fuente_reemplazada"
CODIGO_DESCARGA_COMPLETA = "ingesta.descarga_completa"
CODIGO_COPIA_COMPLETA = "ingesta.copia_completa"
CODIGO_SONDEO_COMPLETO = "ingesta.sondeo_completo"
CODIGO_AVISO_MEDIA = "ingesta.aviso_media"
CODIGO_ARTEFACTO_PUBLICADO = "ingesta.artefacto_publicado"
CODIGO_ARTEFACTO_REUTILIZADO = "ingesta.artefacto_reutilizado"
CODIGO_DESCENDIENTES_INVALIDADOS = "ingesta.descendientes_invalidados"
CODIGO_TEMPORALES_RETIRADOS = "ingesta.temporales_retirados"


@dataclass(frozen=True)
class DiagnosticoIngesta:
    """Hecho estructurado de una ingesta. Nunca texto libre como contrato."""

    codigo: str
    datos: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.codigo, str) or not self.codigo:
            raise ErrorFuenteIngesta("Un diagnostico de ingesta necesita codigo.")
        object.__setattr__(self, "datos", dict(self.datos))


@dataclass(frozen=True)
class SourceAsset:
    """Fuente incorporada: localizable, verificable e invalidable."""

    id: str
    nombre: str
    modo: ModoFuente
    origen: OrigenFuente
    huella: HuellaFuente
    ruta_relativa: str | None = None
    ruta_externa: str | None = None
    entrada_original: str = ""
    estado: EstadoFuente = EstadoFuente.DISPONIBLE

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ErrorFuenteIngesta("Una fuente necesita identidad.")
        if not isinstance(self.nombre, str) or not self.nombre:
            raise ErrorFuenteIngesta("Una fuente necesita un nombre visible.")
        if not isinstance(self.huella, HuellaFuente):
            raise ErrorFuenteIngesta("Una fuente necesita su huella.")
        if self.huella.completa is None:
            raise ErrorFuenteIngesta(
                "Una fuente no se registra sin huella completa: sin ella no tiene identidad.")
        if self.modo is ModoFuente.COPIADA:
            if not self.ruta_relativa or self.ruta_externa:
                raise ErrorFuenteIngesta(
                    "Una fuente copiada se localiza solo por su ruta relativa al proyecto.")
            # La ruta interna se valida con la gramatica de los dos sistemas: un
            # proyecto viaja entre Windows y POSIX, y una ruta que aqui parece
            # relativa puede ser absoluta alla.
            try:
                exigir_ruta_interna(self.ruta_relativa, "ruta de la fuente")
            except ErrorIdentificadorJob as error:
                raise ErrorFuenteIngesta(
                    f"La fuente declara una ruta que no es interna: {self.ruta_relativa!r}"
                ) from error
        else:
            if not self.ruta_externa or self.ruta_relativa:
                raise ErrorFuenteIngesta(
                    "Una fuente referenciada se localiza solo por su ruta externa normalizada.")

    @property
    def es_interna(self) -> bool:
        return self.modo is ModoFuente.COPIADA

    @property
    def identidad_contenido(self) -> str:
        return self.huella.identidad

    def con_huella(self, huella: HuellaFuente) -> "SourceAsset":
        """Devuelve la fuente tras un reemplazo verificado de sus bytes."""
        from dataclasses import replace

        return replace(self, huella=huella, estado=EstadoFuente.DISPONIBLE)

    def como_documento(self) -> dict[str, object]:
        """Vista canonica. Es lo que viaja al manifiesto y a los diagnosticos."""
        return {"id": self.id, "nombre": self.nombre, "modo": self.modo.value,
                "origen": self.origen.value, "estado": self.estado.value,
                "ruta_relativa": self.ruta_relativa, "ruta_externa": self.ruta_externa,
                "huella": self.huella.como_documento()}


def referencia_de_apertura(fuente: SourceAsset) -> dict[str, object]:
    """Datos minimos que `project.json` necesita para abrir y diagnosticar.

    SQLite sigue siendo la autoridad del estado mutable: aqui solo viaja lo que
    permite *encontrar* la fuente y notar que ya no es la misma —modo, ruta,
    tamano, mtime y huella parcial—. La huella completa, los avisos y la
    metadata no entran: duplicarlos crearia dos verdades sobre lo mismo y la del
    manifiesto quedaria vieja al primer cambio.
    """
    return {"id": fuente.id, "modo": fuente.modo.value, "origen": fuente.origen.value,
            "ruta_relativa": fuente.ruta_relativa, "ruta_externa": fuente.ruta_externa,
            "tamano": fuente.huella.tamano, "mtime_ns": fuente.huella.mtime_ns,
            "huella_parcial": fuente.huella.parcial,
            "version_huella": fuente.huella.version}
