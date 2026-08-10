"""`EditDocument`: la fuente de verdad declarativa de una edicion.

Un documento describe *que* quiere la persona, nunca como se materializa. No
contiene comandos, filtros ni rutas de salida; el compilador de T07 traduce esta
intencion a un plan y los backends la materializan. Esa separacion es lo que
permite que preview y salida final no creen dos verdades sobre tiempos, textos o
encuadre.

Las invariantes que el constructor hace irrepresentables:

- **La duracion es la suma de los segmentos.** No es un campo independiente que
  alguien pueda dejar desincronizado: se declara y se comprueba, de modo que un
  documento no puede afirmar que dura mas de lo que trae de la fuente.
- **Nada ocurre fuera del clip.** Cues, efectos, keyframes y superposiciones
  viven en `[0, duracion]`; un rango que se sale es un error, no un valor que el
  compilador recorte en silencio.
- **Los segmentos no se solapan en la fuente y van en orden.** El producto edita
  un momento, no un timeline libre: reordenar tramos no es una edicion que este
  documento pueda expresar, y por eso no puede representarse.
- **Todo parametro esta en el catalogo.** Un efecto desconocido o un parametro
  inventado no llega nunca a existir.

Las referencias de asset se validan aparte (`validar_assets`) porque su
existencia depende del registro del proyecto y no del documento: un documento
correcto puede quedarse temporalmente sin un asset, y eso es un estado que la UI
muestra, no un documento que deje de ser legible.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace

from .errors import ErrorAssetEdicion, ErrorDocumentoEdicion
from .timebase import (
    Rango, Timebase, exigir_entero, exigir_identidades_unicas, exigir_identificador,
    exigir_sin_solapes,
)
from .tracks import (
    AudioTrack, CaptionTrack, EffectTrack, FramingTrack, OverlayTrack, ReferenciaAsset,
    ReferenciaPreset, RenderIntent, SourceSegment,
)


#: Version del esquema del documento. Se sube cuando cambia la *forma*; un valor
#: nuevo dentro de un catalogo no la mueve porque no rompe la lectura.
VERSION_DOCUMENTO = 3

INTENSIDAD_MINIMA = 0
INTENSIDAD_MAXIMA = 100


@dataclass(frozen=True)
class EditDocument:
    """Documento versionado e inmutable de una edicion."""

    document_id: str
    source_id: str
    timebase: Timebase
    duracion: int
    preset: ReferenciaPreset
    source_segments: tuple[SourceSegment, ...]
    framing_track: FramingTrack
    revision_id: str | None = None
    caption_track: CaptionTrack = CaptionTrack()
    audio_track: AudioTrack = AudioTrack()
    effect_track: EffectTrack = EffectTrack()
    overlay_track: OverlayTrack = OverlayTrack()
    render_intent: RenderIntent = RenderIntent()
    intensidad: int = 50

    def __post_init__(self) -> None:
        object.__setattr__(self, "document_id",
                           exigir_identificador(self.document_id, "document_id"))
        object.__setattr__(self, "source_id", exigir_identificador(self.source_id, "source_id"))
        if self.revision_id is not None:
            object.__setattr__(
                self, "revision_id", exigir_identificador(self.revision_id, "revision_id"))
        if not isinstance(self.timebase, Timebase):
            raise ErrorDocumentoEdicion("El documento necesita su timebase entera.")
        for nombre, tipo in (("preset", ReferenciaPreset), ("framing_track", FramingTrack),
                             ("caption_track", CaptionTrack), ("audio_track", AudioTrack),
                             ("effect_track", EffectTrack), ("overlay_track", OverlayTrack),
                             ("render_intent", RenderIntent)):
            if not isinstance(getattr(self, nombre), tipo):
                raise ErrorDocumentoEdicion(f"El documento necesita su '{nombre}'.")

        segmentos = tuple(sorted(self.source_segments, key=lambda s: (s.rango.inicio, s.id)))
        if not segmentos:
            raise ErrorDocumentoEdicion("El documento necesita al menos un segmento de la fuente.")
        exigir_identidades_unicas(tuple(s.id for s in segmentos), "Los segmentos")
        exigir_sin_solapes(tuple(s.rango for s in segmentos), "Los segmentos")
        object.__setattr__(self, "source_segments", segmentos)

        duracion = exigir_entero(self.duracion, "duracion")
        total = sum(segmento.duracion for segmento in segmentos)
        if duracion != total:
            raise ErrorDocumentoEdicion(
                f"El documento declara {duracion} ticks y sus segmentos suman {total}.")
        if duracion > self.timebase.limite():
            raise ErrorDocumentoEdicion(
                f"El documento declara {duracion} ticks, mas de lo representable con su timebase.")

        intensidad = exigir_entero(self.intensidad, "intensidad")
        if not INTENSIDAD_MINIMA <= intensidad <= INTENSIDAD_MAXIMA:
            raise ErrorDocumentoEdicion(
                f"La intensidad vale {intensidad} y tiene que estar entre"
                f" {INTENSIDAD_MINIMA} y {INTENSIDAD_MAXIMA}.")

        self.framing_track.validar(duracion)
        self.caption_track.validar(duracion)
        self.audio_track.validar(duracion)
        self.effect_track.validar(duracion)
        self.overlay_track.validar(duracion)

    # ------------------------------------------------------------------ #
    # Consultas
    # ------------------------------------------------------------------ #

    @property
    def rango_completo(self) -> Rango:
        return Rango(0, self.duracion)

    def inicio_en_documento(self, segmento_id: str) -> int:
        """Donde cae un segmento dentro del clip. Derivado, nunca almacenado."""
        acumulado = 0
        for segmento in self.source_segments:
            if segmento.id == segmento_id:
                return acumulado
            acumulado += segmento.duracion
        raise ErrorDocumentoEdicion(f"El documento no tiene el segmento {segmento_id!r}.")

    def assets(self) -> tuple[ReferenciaAsset, ...]:
        """Todos los assets que el documento referencia, deduplicados y en orden."""
        referencias: list[ReferenciaAsset] = []
        for capa in self.audio_track.capas:
            if capa.asset is not None:
                referencias.append(capa.asset)
        referencias.extend(elemento.asset for elemento in self.overlay_track.elementos)
        vistas: dict[str, ReferenciaAsset] = {}
        for referencia in referencias:
            vistas.setdefault(referencia.clave, referencia)
        return tuple(vistas[clave] for clave in sorted(vistas))

    # ------------------------------------------------------------------ #
    # Serializacion canonica
    # ------------------------------------------------------------------ #

    def como_documento(self) -> dict[str, object]:
        """Vista canonica. Es exactamente lo que se persiste y lo que se compara."""
        return {
            "schema_version": VERSION_DOCUMENTO,
            "document_id": self.document_id,
            "revision_id": self.revision_id,
            "source_id": self.source_id,
            "timebase": self.timebase.como_documento(),
            "duracion": self.duracion,
            "intensidad": self.intensidad,
            "preset": self.preset.como_documento(),
            "source_segments": [s.como_documento() for s in self.source_segments],
            "framing_track": self.framing_track.como_documento(),
            "caption_track": self.caption_track.como_documento(),
            "audio_track": self.audio_track.como_documento(),
            "effect_track": self.effect_track.como_documento(),
            "overlay_track": self.overlay_track.como_documento(),
            "render_intent": self.render_intent.como_documento(),
        }

    @classmethod
    def desde_documento(cls, datos: object) -> "EditDocument":
        """Lee un documento **ya migrado** a `VERSION_DOCUMENTO`.

        No acepta versiones antiguas a proposito: quien las tenga pasa por
        `schema.migrar_documento`, que es el unico sitio donde vive el
        conocimiento de como era el formato anterior.
        """
        if not isinstance(datos, Mapping):
            raise ErrorDocumentoEdicion("El documento serializado no es un objeto.")
        version = datos.get("schema_version")
        if version != VERSION_DOCUMENTO:
            raise ErrorDocumentoEdicion(
                f"El documento declara la version {version!r} y esta lectura espera"
                f" {VERSION_DOCUMENTO}; migralo antes.")
        return cls(
            document_id=datos.get("document_id"),
            revision_id=datos.get("revision_id"),
            source_id=datos.get("source_id"),
            timebase=Timebase.desde_documento(datos.get("timebase")),
            duracion=exigir_entero(datos.get("duracion"), "duracion"),
            intensidad=datos.get("intensidad", 50),
            preset=ReferenciaPreset.desde_documento(datos.get("preset"), "preset"),
            source_segments=tuple(
                SourceSegment.desde_documento(entrada, f"source_segments[{indice}]")
                for indice, entrada in enumerate(_lista(datos.get("source_segments"),
                                                        "source_segments"))),
            framing_track=FramingTrack.desde_documento(datos.get("framing_track"), "framing_track"),
            caption_track=CaptionTrack.desde_documento(datos.get("caption_track", {}),
                                                       "caption_track"),
            audio_track=AudioTrack.desde_documento(datos.get("audio_track", {}), "audio_track"),
            effect_track=EffectTrack.desde_documento(datos.get("effect_track", {}), "effect_track"),
            overlay_track=OverlayTrack.desde_documento(datos.get("overlay_track", {}),
                                                       "overlay_track"),
            render_intent=RenderIntent.desde_documento(datos.get("render_intent", {}),
                                                       "render_intent"),
        )

    def con(self, **cambios: object) -> "EditDocument":
        """Copia con cambios, revalidada entera. No hay mutacion en ningun punto."""
        return replace(self, **cambios)


def _lista(datos: object, campo: str) -> list:
    if not isinstance(datos, (list, tuple)):
        raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser una lista.")
    return list(datos)


def validar_assets(documento: EditDocument,
                   disponibles: Mapping[str, str | tuple[str, str] | ReferenciaAsset]) -> None:
    """Toda referencia del documento tiene que resolver en el registro dado.

    `disponibles` acepta la forma heredada id -> version y la forma tipada id ->
    (version, tipo) / ReferenciaAsset. La segunda comprueba tambien el tipo; la
    compatibilidad minima pista-tipo ya es una invariante del documento.
    """
    for referencia in documento.assets():
        disponible = disponibles.get(referencia.asset_id)
        if disponible is None:
            raise ErrorAssetEdicion(
                f"El documento referencia el asset {referencia.asset_id!r}, que el proyecto no tiene.")
        tipo: str | None = None
        if isinstance(disponible, ReferenciaAsset):
            if disponible.asset_id != referencia.asset_id:
                raise ErrorAssetEdicion(
                    f"El registro resolvio {referencia.asset_id!r} como"
                    f" {disponible.asset_id!r}.")
            version, tipo = disponible.version, disponible.tipo
        elif isinstance(disponible, tuple) and len(disponible) == 2:
            version, tipo = disponible
        else:
            version = disponible
        if not isinstance(version, str):
            raise ErrorAssetEdicion(
                f"El registro del asset {referencia.asset_id!r} no declara una version legible.")
        if version != referencia.version:
            raise ErrorAssetEdicion(
                f"El documento referencia {referencia.clave!r} y el proyecto publica la version"
                f" {version!r}.")
        if tipo is not None and tipo != referencia.tipo:
            raise ErrorAssetEdicion(
                f"El documento usa {referencia.asset_id!r} como {referencia.tipo!r}, pero el"
                f" proyecto lo registra como {tipo!r}.")


__all__ = [
    "EditDocument", "INTENSIDAD_MAXIMA", "INTENSIDAD_MINIMA", "VERSION_DOCUMENTO", "validar_assets",
]
