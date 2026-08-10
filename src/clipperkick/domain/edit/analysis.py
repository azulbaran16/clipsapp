"""Precedencia de las correcciones manuales frente a un analisis nuevo.

Un rerun de analisis produce **otro documento completo**, no un parche: el
compilador de preset no sabe que corrigio la persona ni tiene por que saberlo.
La fusion es lo que decide, campo a campo, cual de los dos gana.

La regla es una sola y no admite matices: **lo marcado a mano gana siempre**. De
ahi salen tres consecuencias que el codigo hace explicitas:

- un campo marcado conserva su valor aunque el analisis proponga otro;
- un elemento con cualquier marca sobrevive aunque el analisis ya no lo proponga
  —borrar el cue que alguien escribio seria la peor forma de "actualizar"—;
- un elemento propuesto que **choca** con uno manual se descarta, no se
  reubica. Un solape de subtitulos o dos keyframes en el mismo tick no son
  estados que el documento admita, y resolverlos moviendo lo del analisis
  respeta la decision humana sin inventar ninguna.

La fusion exige que ambos documentos compartan mapa temporal (misma timebase,
mismos segmentos, misma duracion). No es una limitacion: un analisis se relanza
sobre el momento que hay, y si los limites cambiaron lo que corresponde es
recortar primero —que ya remapea todo— y analizar despues.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import TypeVar

from .document import EditDocument
from .errors import ErrorDocumentoEdicion
from .tracks import es_manual


Elemento = TypeVar("Elemento")


def _fusionar_campos(actual: Elemento, propuesto: Elemento) -> Elemento:
    """Toma el propuesto y le devuelve los campos que la persona fijo."""
    marcas = frozenset(getattr(actual, "manual", frozenset()))
    if not marcas:
        return propuesto
    conservados = {campo: getattr(actual, campo) for campo in marcas}
    return replace(propuesto, manual=marcas, **conservados)


def _fusionar_elementos(actuales: Sequence[Elemento], propuestos: Sequence[Elemento],
                        identidad: Callable[[Elemento], str],
                        compatibles: Callable[[Elemento, Elemento], bool] | None = None,
                        choca: Callable[[Elemento, Elemento], bool] | None = None,
                        ) -> tuple[Elemento, ...]:
    por_identidad = {identidad(elemento): elemento for elemento in actuales}
    identidades_propuestas = {identidad(elemento) for elemento in propuestos}
    #: Lo manual que el analisis ya no propone se conserva antes de nada: es lo
    #: que despues decide que propuestas chocan y hay que descartar.
    conservados = [elemento for elemento in actuales
                   if identidad(elemento) not in identidades_propuestas and es_manual(elemento)]

    resultado: list[Elemento] = []
    for propuesto in propuestos:
        actual = por_identidad.get(identidad(propuesto))
        if actual is None:
            if choca is not None and any(choca(propuesto, guardado) for guardado in conservados):
                continue
            resultado.append(propuesto)
        elif compatibles is not None and es_manual(actual) and not compatibles(actual, propuesto):
            # El analisis propone otra cosa con el mismo nombre. Fusionar campo a
            # campo mezclaria dos elementos distintos; se conserva el humano.
            resultado.append(actual)
        else:
            resultado.append(_fusionar_campos(actual, propuesto))
    return tuple([*resultado, *conservados])


def _exigir_mismo_mapa(actual: EditDocument, propuesta: EditDocument) -> None:
    if actual.document_id != propuesta.document_id:
        raise ErrorDocumentoEdicion(
            "El analisis propone un documento distinto del que se esta editando.")
    if actual.timebase != propuesta.timebase or actual.duracion != propuesta.duracion:
        raise ErrorDocumentoEdicion(
            "El analisis propone otra base temporal; recorta el momento antes de relanzarlo.")
    mapa_actual = tuple((segment.id, segment.rango) for segment in actual.source_segments)
    mapa_propuesto = tuple((segment.id, segment.rango) for segment in propuesta.source_segments)
    if mapa_actual != mapa_propuesto:
        raise ErrorDocumentoEdicion(
            "El analisis propone otros segmentos de la fuente; recorta el momento antes de"
            " relanzarlo.")


def fusionar_analisis(actual: EditDocument, propuesta: EditDocument) -> EditDocument:
    """Documento nuevo con lo propuesto donde no hay override y lo humano donde si."""
    _exigir_mismo_mapa(actual, propuesta)

    captions_actual, captions_propuesta = actual.caption_track, propuesta.caption_track
    if "cues" in captions_actual.manual:
        # La persona curo la lista entera —dividio, unio, oculto—. Reponer cues
        # automaticos sobre esa curacion la deshace sin que nada lo diga.
        cues = captions_actual.cues
    else:
        # Antes de fusionar el cue, fusiona sus tokens por identidad estable.
        # Asi un texto humano sobre una palabra sobrevive aunque el analisis
        # refresque confianza o timings de otros tokens.
        cues_propuestos = []
        cues_actuales = {cue.id: cue for cue in captions_actual.cues}
        for proposed_cue in captions_propuesta.cues:
            current_cue = cues_actuales.get(proposed_cue.id)
            if current_cue is not None:
                words = _fusionar_elementos(
                    current_cue.palabras, proposed_cue.palabras, lambda word: word.id)
                proposed_cue = replace(proposed_cue, palabras=words)
            cues_propuestos.append(proposed_cue)
        cues = _fusionar_elementos(
            captions_actual.cues, cues_propuestos, lambda cue: cue.id,
            choca=lambda propuesto, guardado: propuesto.rango.solapa(guardado.rango))
    caption_track = _fusionar_campos(captions_actual,
                                     replace(captions_propuesta, cues=cues, manual=frozenset()))

    encuadre_actual, encuadre_propuesta = actual.framing_track, propuesta.framing_track
    if "keyframes" in encuadre_actual.manual:
        keyframes = encuadre_actual.keyframes
    else:
        keyframes = _fusionar_elementos(
            encuadre_actual.keyframes, encuadre_propuesta.keyframes, lambda k: k.id,
            choca=lambda propuesto, guardado: propuesto.tick == guardado.tick)
    framing_track = _fusionar_campos(
        encuadre_actual, replace(encuadre_propuesta, keyframes=keyframes, manual=frozenset()))

    capas = _fusionar_elementos(actual.audio_track.capas, propuesta.audio_track.capas,
                                lambda capa: capa.capa)
    audio_track = _fusionar_campos(actual.audio_track,
                                   replace(propuesta.audio_track, capas=capas,
                                           manual=frozenset()))

    efectos_actual = actual.effect_track
    if "eventos" in efectos_actual.manual:
        eventos = efectos_actual.eventos
    else:
        eventos = _fusionar_elementos(
            efectos_actual.eventos, propuesta.effect_track.eventos, lambda evento: evento.id,
            compatibles=lambda actual_, propuesto: actual_.tipo == propuesto.tipo)
    effect_track = _fusionar_campos(
        efectos_actual, replace(propuesta.effect_track, eventos=eventos, manual=frozenset()))

    superposiciones_actual = actual.overlay_track
    if "elementos" in superposiciones_actual.manual:
        elementos = superposiciones_actual.elementos
    else:
        elementos = _fusionar_elementos(superposiciones_actual.elementos,
                                        propuesta.overlay_track.elementos,
                                        lambda elemento: elemento.id)
    overlay_track = _fusionar_campos(
        superposiciones_actual, replace(propuesta.overlay_track, elementos=elementos,
                                        manual=frozenset()))

    render_intent = _fusionar_campos(actual.render_intent,
                                     replace(propuesta.render_intent, manual=frozenset()))

    return actual.con(
        # Los segmentos no se tocan: la fusion exige que sean identicos, de modo
        # que el mapa temporal sobre el que se posiciona todo lo demas no cambia.
        # Preset e intensidad son la *entrada* del analisis, no su resultado, y
        # se conservan los que la persona eligio para relanzarlo.
        caption_track=caption_track, framing_track=framing_track, audio_track=audio_track,
        effect_track=effect_track, overlay_track=overlay_track, render_intent=render_intent)


def restaurar_automatico(actual: EditDocument, original: EditDocument) -> EditDocument:
    """Vuelve al documento generado, sin marcas manuales y sin tocar el historial.

    No borra nada: devuelve un documento nuevo que la capa de aplicacion
    confirmara como una revision mas. El estado corregido sigue siendo su padre y
    se alcanza deshaciendo, que es la "copia de seguridad" que el recorrido
    promete antes de restaurar.

    A diferencia de la fusion, aqui **si** puede cambiar el mapa temporal: si la
    persona recorto el momento, restaurar lo automatico devuelve tambien el
    recorte original. Eso es lo que "estado generado originalmente" significa.
    """
    if actual.document_id != original.document_id or actual.source_id != original.source_id:
        raise ErrorDocumentoEdicion("El documento original no corresponde al que se esta editando.")
    return original


__all__ = ["fusionar_analisis", "restaurar_automatico"]
