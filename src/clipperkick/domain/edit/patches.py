"""Patches tipados: la unica forma de cambiar un documento.

Un patch no es un diff generico sobre JSON. Es una operacion con nombre, con
argumentos validados y con tres responsabilidades:

1. **Producir el documento siguiente.** Nunca muta el anterior: devuelve otro,
   revalidado entero. Una revision es inmutable, y la unica manera de garantizar
   eso es que ni siquiera exista la operacion de mutar.
2. **Marcar lo que la persona fijo.** Aplicar un patch marca como `manual`
   exactamente los campos que toco. Es lo que hace que un rerun de analisis
   pueda proponer de nuevo sin pisar correcciones, y por eso vive aqui y no en
   la UI: si marcar fuese responsabilidad de quien llama, bastaria un camino
   olvidado para que una correccion quedase desprotegida.
3. **Declarar su rango de impacto.** El preview reconstruye solo lo afectado.
   Un patch que no sabe acotar su impacto devuelve el documento entero, que es
   la respuesta cara pero nunca incorrecta.

Cambiar los segmentos de la fuente es el unico patch que reescribe el mapa del
tiempo. Lo hace **remapeando** todo lo demas por su posicion en la fuente, no
por su posicion en el clip: recortar el inicio de un momento tiene que mover los
subtitulos con el audio del que salieron, no dejarlos donde estaban.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import ClassVar

from .catalog import CAPAS_AUDIO, ValorParametro
from .document import EditDocument
from .errors import ErrorPatchEdicion
from .timebase import Rango, exigir_entero, exigir_identificador
from .tracks import (
    CapaAudio, Cue, EventoEfecto, KeyframeEncuadre, Palabra, ReferenciaAsset, ReferenciaPreset,
    SourceSegment, Superposicion, VentanaEncuadre, con_marcas,
)


#: Margen que un cambio de texto o estilo anade a su cue: las animaciones de
#: entrada y salida empiezan antes y acaban despues del propio cue, y un preview
#: que solo invalidase el cue mostraria media animacion vieja.
DIVISOR_MARGEN_ANIMACION = 4


def _margen(documento: EditDocument) -> int:
    return max(1, documento.timebase.ticks_por_segundo // DIVISOR_MARGEN_ANIMACION)


# --------------------------------------------------------------------------- #
# Remapeo del tiempo cuando cambian los segmentos
# --------------------------------------------------------------------------- #

def _tramos(segmentos: Sequence[SourceSegment]) -> tuple[tuple[int, SourceSegment], ...]:
    """(inicio en el documento, segmento) para cada tramo, en orden."""
    tramos: list[tuple[int, SourceSegment]] = []
    acumulado = 0
    for segmento in segmentos:
        tramos.append((acumulado, segmento))
        acumulado += segmento.duracion
    return tuple(tramos)


def _a_fuente(tick: int, tramos: Sequence[tuple[int, SourceSegment]]) -> int | None:
    for inicio, segmento in tramos:
        if inicio <= tick < inicio + segmento.duracion:
            return segmento.rango.inicio + (tick - inicio)
    return None


def _a_documento(tick_fuente: int, tramos: Sequence[tuple[int, SourceSegment]]) -> int | None:
    for inicio, segmento in tramos:
        if segmento.rango.contiene(tick_fuente):
            return inicio + (tick_fuente - segmento.rango.inicio)
    return None


class _Mapa:
    """Traduce posiciones del documento entre dos disposiciones de segmentos."""

    def __init__(self, antes: Sequence[SourceSegment], despues: Sequence[SourceSegment]) -> None:
        self._antes, self._despues = _tramos(antes), _tramos(despues)

    def tick(self, valor: int) -> int | None:
        fuente = _a_fuente(valor, self._antes)
        return None if fuente is None else _a_documento(fuente, self._despues)

    def rango(self, valor: Rango) -> Rango | None:
        """Extremos por separado. El interior puede haber perdido material.

        Mapear los dos extremos y no el intervalo completo es lo correcto
        cuando un corte elimina algo de en medio: el elemento sigue cubriendo el
        mismo contenido y simplemente dura menos. Si alguno de los extremos ya no
        existe en la fuente, el elemento se retira: recolocarlo seria inventar
        una decision editorial que nadie tomo.
        """
        inicio = self.tick(valor.inicio)
        fin = self.tick(valor.fin - 1)
        if inicio is None or fin is None or fin + 1 <= inicio:
            return None
        return Rango(inicio, fin + 1)


def remapear(documento: EditDocument, segmentos: Sequence[SourceSegment]) -> EditDocument:
    """Devuelve el documento con otros segmentos y todo lo demas recolocado."""
    nuevos = tuple(sorted(segmentos, key=lambda s: (s.rango.inicio, s.id)))
    if not nuevos:
        raise ErrorPatchEdicion("Un documento no puede quedarse sin segmentos de la fuente.")
    mapa = _Mapa(documento.source_segments, nuevos)
    duracion = sum(segmento.duracion for segmento in nuevos)

    cues = []
    for cue in documento.caption_track.cues:
        rango = mapa.rango(cue.rango)
        if rango is None:
            continue
        palabras = []
        for palabra in cue.palabras:
            recolocada = mapa.rango(palabra.rango)
            if recolocada is None or recolocada.inicio < rango.inicio or recolocada.fin > rango.fin:
                continue
            palabras.append(replace(palabra, rango=recolocada))
        cues.append(replace(cue, rango=rango, palabras=tuple(palabras)))

    keyframes = []
    for keyframe in documento.framing_track.keyframes:
        tick = mapa.tick(keyframe.tick)
        if tick is not None:
            keyframes.append(replace(keyframe, tick=tick))
    if not keyframes:
        keyframes = [replace(documento.framing_track.keyframes[0], tick=0)]
    if all(keyframe.tick != 0 for keyframe in keyframes):
        # El encuadre necesita ventana en el primer fotograma. Se clona el
        # keyframe superviviente mas temprano en vez de inventar una ventana:
        # es la unica decision que no cambia lo que se ve al empezar.
        primero = min(keyframes, key=lambda k: k.tick)
        keyframes.append(replace(primero, id=f"{primero.id}.inicio", tick=0))

    eventos = []
    for evento in documento.effect_track.eventos:
        rango = mapa.rango(evento.rango)
        if rango is not None:
            eventos.append(replace(evento, rango=rango))

    elementos = []
    for elemento in documento.overlay_track.elementos:
        rango = mapa.rango(elemento.rango)
        if rango is not None:
            elementos.append(replace(elemento, rango=rango))

    capas = []
    for capa in documento.audio_track.capas:
        puntos = []
        for punto in capa.envolvente:
            tick = mapa.tick(punto.tick)
            if tick is not None:
                puntos.append(replace(punto, tick=tick))
        capas.append(replace(capa, envolvente=tuple(puntos)))

    desfase = documento.caption_track.desfase
    return documento.con(
        source_segments=nuevos, duracion=duracion,
        caption_track=replace(documento.caption_track, cues=tuple(cues),
                              desfase=max(-duracion, min(duracion, desfase))),
        framing_track=replace(documento.framing_track, keyframes=tuple(keyframes)),
        audio_track=replace(documento.audio_track, capas=tuple(capas)),
        effect_track=replace(documento.effect_track, eventos=tuple(eventos)),
        overlay_track=replace(documento.overlay_track, elementos=tuple(elementos)),
    )


# --------------------------------------------------------------------------- #
# Patch base
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Patch:
    """Operacion tipada sobre un documento. Las subclases son datos, no logica suelta."""

    CODIGO: ClassVar[str] = "edicion.desconocido"

    def aplicar(self, documento: EditDocument) -> EditDocument:
        raise NotImplementedError

    def impacto(self, documento: EditDocument) -> Rango:
        """Por defecto, todo. Cada patch que sepa acotar lo hace explicitamente."""
        return documento.rango_completo

    @property
    def codigo(self) -> str:
        return self.CODIGO

    @property
    def grupo(self) -> str:
        """Clave de agrupacion del autoguardado.

        Dos patches del mismo grupo consecutivos son la misma correccion en
        curso —escribir en un cue, arrastrar un control— y se confirman como una
        sola revision. Cambiar de grupo cierra la anterior.
        """
        return self.CODIGO


def aplicar_patches(documento: EditDocument, patches: Sequence[Patch]) -> EditDocument:
    for patch in patches:
        if not isinstance(patch, Patch):
            raise ErrorPatchEdicion("Solo se aplican patches tipados sobre un documento.")
        documento = patch.aplicar(documento)
    return documento


def impacto_de(documento: EditDocument, patches: Sequence[Patch]) -> Rango:
    """Union de impactos sobre los estados sucesivos del mismo lote.

    Calcular cada impacto contra el documento inicial falla para lotes como
    ``dividir cue -> corregir el cue nuevo``: el segundo objetivo todavia no
    existe en el snapshot inicial. Recorrer la misma secuencia que se confirma
    mantiene el calculo total y determinista.
    """
    if not patches:
        return Rango(0, 1)
    acumulado: Rango | None = None
    actual = documento
    for patch in patches:
        if not isinstance(patch, Patch):
            raise ErrorPatchEdicion("Solo se calcula impacto de patches tipados.")
        impacto = patch.impacto(actual)
        acumulado = impacto if acumulado is None else acumulado.unir(impacto)
        actual = patch.aplicar(actual)
    assert acumulado is not None
    return acumulado


# --------------------------------------------------------------------------- #
# Segmentos
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class AjustarSegmento(Patch):
    """Mueve los limites de un tramo de la fuente y recoloca todo lo demas."""

    CODIGO: ClassVar[str] = "segmentos.rango"

    segmento_id: str
    rango: Rango

    def aplicar(self, documento: EditDocument) -> EditDocument:
        segmentos = list(documento.source_segments)
        for indice, segmento in enumerate(segmentos):
            if segmento.id == self.segmento_id:
                segmentos[indice] = con_marcas(replace(segmento, rango=self.rango), ("rango",))
                break
        else:
            raise ErrorPatchEdicion(f"El documento no tiene el segmento {self.segmento_id!r}.")
        return remapear(documento, segmentos)

    @property
    def grupo(self) -> str:
        return f"{self.CODIGO}:{self.segmento_id}"


@dataclass(frozen=True)
class EstablecerTransicion(Patch):
    CODIGO: ClassVar[str] = "segmentos.transicion"

    segmento_id: str
    transicion: str

    def aplicar(self, documento: EditDocument) -> EditDocument:
        segmentos = list(documento.source_segments)
        for indice, segmento in enumerate(segmentos):
            if segmento.id == self.segmento_id:
                segmentos[indice] = con_marcas(
                    replace(segmento, transicion_entrada=self.transicion), ("transicion_entrada",))
                return documento.con(source_segments=tuple(segmentos))
        raise ErrorPatchEdicion(f"El documento no tiene el segmento {self.segmento_id!r}.")

    def impacto(self, documento: EditDocument) -> Rango:
        inicio = documento.inicio_en_documento(self.segmento_id)
        return Rango(inicio, inicio + 1).expandido(_margen(documento), documento.duracion)

    @property
    def grupo(self) -> str:
        return f"{self.CODIGO}:{self.segmento_id}"


# --------------------------------------------------------------------------- #
# Subtitulos
# --------------------------------------------------------------------------- #

def _cue(documento: EditDocument, cue_id: str) -> Cue:
    cue = documento.caption_track.cue(cue_id)
    if cue is None:
        raise ErrorPatchEdicion(f"El documento no tiene el cue {cue_id!r}.")
    return cue


def _con_cues(documento: EditDocument, cues: Sequence[Cue],
              *, estructura_manual: bool = False) -> EditDocument:
    track = replace(documento.caption_track, cues=tuple(cues))
    if estructura_manual:
        track = con_marcas(track, ("cues",))
    return documento.con(caption_track=track)


def _reemplazar_cue(documento: EditDocument, cue: Cue) -> EditDocument:
    return _con_cues(documento, [cue if otro.id == cue.id else otro
                                 for otro in documento.caption_track.cues])


@dataclass(frozen=True)
class CorregirTextoCue(Patch):
    """Corrige el texto **sin** tocar los tiempos. Realinear es otra accion."""

    CODIGO: ClassVar[str] = "captions.texto"

    cue_id: str
    texto: str

    def aplicar(self, documento: EditDocument) -> EditDocument:
        cue = _cue(documento, self.cue_id)
        return _reemplazar_cue(documento, con_marcas(replace(cue, texto=self.texto), ("texto",)))

    def impacto(self, documento: EditDocument) -> Rango:
        return _cue(documento, self.cue_id).rango.expandido(_margen(documento), documento.duracion)

    @property
    def grupo(self) -> str:
        return f"{self.CODIGO}:{self.cue_id}"


@dataclass(frozen=True)
class CorregirPalabra(Patch):
    """Corrige un token estable sin mover sus timestamps."""

    CODIGO: ClassVar[str] = "captions.palabra.texto"

    cue_id: str
    palabra_id: str
    texto: str

    def aplicar(self, documento: EditDocument) -> EditDocument:
        cue = _cue(documento, self.cue_id)
        words = list(cue.palabras)
        for index, word in enumerate(words):
            if word.id == self.palabra_id:
                words[index] = con_marcas(replace(word, texto=self.texto), ("texto",))
                break
        else:
            raise ErrorPatchEdicion(
                f"El cue {self.cue_id!r} no tiene la palabra {self.palabra_id!r}.")
        updated = con_marcas(
            replace(cue, palabras=tuple(words), texto=_texto_de(words)), ("texto",))
        return _reemplazar_cue(documento, updated)

    def impacto(self, documento: EditDocument) -> Rango:
        return _cue(documento, self.cue_id).rango.expandido(
            _margen(documento), documento.duracion)

    @property
    def grupo(self) -> str:
        return f"{self.CODIGO}:{self.cue_id}:{self.palabra_id}"


@dataclass(frozen=True)
class AjustarRangoCue(Patch):
    CODIGO: ClassVar[str] = "captions.rango"

    cue_id: str
    rango: Rango

    def aplicar(self, documento: EditDocument) -> EditDocument:
        cue = _cue(documento, self.cue_id)
        palabras = tuple(palabra for palabra in cue.palabras
                         if palabra.rango.inicio >= self.rango.inicio
                         and palabra.rango.fin <= self.rango.fin)
        return _reemplazar_cue(
            documento, con_marcas(replace(cue, rango=self.rango, palabras=palabras), ("rango",)))

    def impacto(self, documento: EditDocument) -> Rango:
        margen = _margen(documento)
        antes = _cue(documento, self.cue_id).rango.expandido(margen, documento.duracion)
        return antes.unir(self.rango.expandido(margen, documento.duracion))

    @property
    def grupo(self) -> str:
        return f"{self.CODIGO}:{self.cue_id}"


@dataclass(frozen=True)
class OcultarCue(Patch):
    CODIGO: ClassVar[str] = "captions.oculto"

    cue_id: str
    oculto: bool = True

    def aplicar(self, documento: EditDocument) -> EditDocument:
        cue = _cue(documento, self.cue_id)
        return _reemplazar_cue(documento,
                               con_marcas(replace(cue, oculto=self.oculto), ("oculto",)))

    def impacto(self, documento: EditDocument) -> Rango:
        return _cue(documento, self.cue_id).rango.expandido(_margen(documento), documento.duracion)

    @property
    def grupo(self) -> str:
        return f"{self.CODIGO}:{self.cue_id}"


@dataclass(frozen=True)
class DividirCue(Patch):
    """Parte un cue en dos por un tick interior."""

    CODIGO: ClassVar[str] = "captions.dividir"

    cue_id: str
    tick: int
    nuevo_id: str
    texto_izquierda: str | None = None
    texto_derecha: str | None = None

    def aplicar(self, documento: EditDocument) -> EditDocument:
        cue = _cue(documento, self.cue_id)
        tick = exigir_entero(self.tick, "division.tick")
        if not cue.rango.inicio < tick < cue.rango.fin:
            raise ErrorPatchEdicion(
                f"El cue {self.cue_id!r} no se puede dividir en el tick {tick}: cae fuera de el.")
        exigir_identificador(self.nuevo_id, "division.nuevo_id")
        if documento.caption_track.cue(self.nuevo_id) is not None:
            raise ErrorPatchEdicion(f"Ya existe un cue {self.nuevo_id!r}.")
        izquierda = tuple(p for p in cue.palabras if p.rango.fin <= tick)
        derecha = tuple(p for p in cue.palabras if p.rango.inicio >= tick)
        if len(izquierda) + len(derecha) != len(cue.palabras):
            raise ErrorPatchEdicion(
                f"El tick {tick} parte una palabra del cue {self.cue_id!r}; divide en un silencio.")
        texto_izquierda = self.texto_izquierda or _texto_de(izquierda) or cue.texto
        texto_derecha = self.texto_derecha or _texto_de(derecha)
        if not texto_derecha:
            raise ErrorPatchEdicion(
                f"La division del cue {self.cue_id!r} deja la segunda mitad sin texto;"
                " indica cual va en cada parte.")
        primero = con_marcas(
            replace(cue, rango=Rango(cue.rango.inicio, tick), texto=texto_izquierda,
                    palabras=izquierda), ("rango", "texto", "palabras"))
        segundo = con_marcas(
            replace(cue, id=self.nuevo_id, rango=Rango(tick, cue.rango.fin), texto=texto_derecha,
                    palabras=derecha), ("rango", "texto", "palabras"))
        otros = [otro for otro in documento.caption_track.cues if otro.id != cue.id]
        return _con_cues(documento, [*otros, primero, segundo], estructura_manual=True)

    def impacto(self, documento: EditDocument) -> Rango:
        return _cue(documento, self.cue_id).rango.expandido(_margen(documento), documento.duracion)


@dataclass(frozen=True)
class UnirCues(Patch):
    """Une dos cues contiguos en el primero."""

    CODIGO: ClassVar[str] = "captions.unir"

    primero_id: str
    segundo_id: str

    def aplicar(self, documento: EditDocument) -> EditDocument:
        primero, segundo = _cue(documento, self.primero_id), _cue(documento, self.segundo_id)
        if primero.rango.inicio > segundo.rango.inicio:
            primero, segundo = segundo, primero
        ordenados = documento.caption_track.cues
        posicion = next(indice for indice, cue in enumerate(ordenados) if cue.id == primero.id)
        siguiente = ordenados[posicion + 1] if posicion + 1 < len(ordenados) else None
        if siguiente is None or siguiente.id != segundo.id:
            raise ErrorPatchEdicion(
                f"Los cues {self.primero_id!r} y {self.segundo_id!r} no son contiguos.")
        unido = con_marcas(
            replace(primero, rango=primero.rango.unir(segundo.rango),
                    texto=f"{primero.texto} {segundo.texto}".strip(),
                    palabras=primero.palabras + segundo.palabras),
            ("rango", "texto", "palabras"))
        return _con_cues(documento, [unido if otro.id == primero.id else otro
                                     for otro in documento.caption_track.cues
                                     if otro.id != segundo.id], estructura_manual=True)

    def impacto(self, documento: EditDocument) -> Rango:
        margen = _margen(documento)
        return (_cue(documento, self.primero_id).rango
                .unir(_cue(documento, self.segundo_id).rango)
                .expandido(margen, documento.duracion))


def _texto_de(palabras: Sequence[Palabra]) -> str:
    return " ".join(palabra.texto for palabra in palabras).strip()


@dataclass(frozen=True)
class DesplazarCaptions(Patch):
    """Desfase global. Impacta el documento entero por definicion."""

    CODIGO: ClassVar[str] = "captions.desfase"

    desfase: int

    def aplicar(self, documento: EditDocument) -> EditDocument:
        pista = con_marcas(replace(documento.caption_track, desfase=self.desfase), ("desfase",))
        return documento.con(caption_track=pista)


@dataclass(frozen=True)
class EstablecerEstiloCaptions(Patch):
    CODIGO: ClassVar[str] = "captions.estilo"

    estilo: str | None = None
    tamano_relativo: int | None = None

    def aplicar(self, documento: EditDocument) -> EditDocument:
        cambios: dict[str, object] = {}
        if self.estilo is not None:
            cambios["estilo"] = self.estilo
        if self.tamano_relativo is not None:
            cambios["tamano_relativo"] = self.tamano_relativo
        if not cambios:
            raise ErrorPatchEdicion("El patch de estilo de subtitulos no cambia nada.")
        return documento.con(
            caption_track=con_marcas(replace(documento.caption_track, **cambios), tuple(cambios)))


# --------------------------------------------------------------------------- #
# Encuadre
# --------------------------------------------------------------------------- #

def _keyframe(documento: EditDocument, keyframe_id: str) -> tuple[int, KeyframeEncuadre]:
    for indice, keyframe in enumerate(documento.framing_track.keyframes):
        if keyframe.id == keyframe_id:
            return indice, keyframe
    raise ErrorPatchEdicion(f"El documento no tiene el keyframe {keyframe_id!r}.")


def _impacto_keyframe(documento: EditDocument, keyframe_id: str) -> Rango:
    """Entre los keyframes vecinos: es lo unico que la interpolacion cambia."""
    indice, _ = _keyframe(documento, keyframe_id)
    keyframes = documento.framing_track.keyframes
    inicio = keyframes[indice - 1].tick if indice > 0 else 0
    fin = keyframes[indice + 1].tick if indice + 1 < len(keyframes) else documento.duracion
    return Rango(inicio, max(fin, inicio + 1))


@dataclass(frozen=True)
class MoverKeyframeEncuadre(Patch):
    CODIGO: ClassVar[str] = "encuadre.ventana"

    keyframe_id: str
    ventana: VentanaEncuadre

    def aplicar(self, documento: EditDocument) -> EditDocument:
        indice, keyframe = _keyframe(documento, self.keyframe_id)
        keyframes = list(documento.framing_track.keyframes)
        keyframes[indice] = con_marcas(replace(keyframe, ventana=self.ventana), ("ventana",))
        return documento.con(
            framing_track=replace(documento.framing_track, keyframes=tuple(keyframes)))

    def impacto(self, documento: EditDocument) -> Rango:
        return _impacto_keyframe(documento, self.keyframe_id)

    @property
    def grupo(self) -> str:
        return f"{self.CODIGO}:{self.keyframe_id}"


@dataclass(frozen=True)
class EstablecerModoEncuadre(Patch):
    CODIGO: ClassVar[str] = "encuadre.modo"

    keyframe_id: str
    modo: str

    def aplicar(self, documento: EditDocument) -> EditDocument:
        indice, keyframe = _keyframe(documento, self.keyframe_id)
        keyframes = list(documento.framing_track.keyframes)
        keyframes[indice] = con_marcas(replace(keyframe, modo=self.modo), ("modo",))
        return documento.con(
            framing_track=replace(documento.framing_track, keyframes=tuple(keyframes)))

    def impacto(self, documento: EditDocument) -> Rango:
        return _impacto_keyframe(documento, self.keyframe_id)

    @property
    def grupo(self) -> str:
        return f"{self.CODIGO}:{self.keyframe_id}"


@dataclass(frozen=True)
class EstablecerAspecto(Patch):
    CODIGO: ClassVar[str] = "encuadre.aspecto"

    aspecto: str

    def aplicar(self, documento: EditDocument) -> EditDocument:
        return documento.con(
            framing_track=con_marcas(replace(documento.framing_track, aspecto=self.aspecto),
                                     ("aspecto",)))


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #

def _capa(documento: EditDocument, nombre: str) -> tuple[int, CapaAudio]:
    if nombre not in CAPAS_AUDIO:
        raise ErrorPatchEdicion(f"El audio no tiene la capa {nombre!r}.")
    for indice, capa in enumerate(documento.audio_track.capas):
        if capa.capa == nombre:
            return indice, capa
    raise ErrorPatchEdicion(f"El documento no declara la capa {nombre!r}.")


def _con_capa(documento: EditDocument, indice: int, capa: CapaAudio) -> EditDocument:
    capas = list(documento.audio_track.capas)
    capas[indice] = capa
    return documento.con(audio_track=replace(documento.audio_track, capas=tuple(capas)))


@dataclass(frozen=True)
class EstablecerGananciaAudio(Patch):
    CODIGO: ClassVar[str] = "audio.ganancia"

    capa: str
    ganancia_ddb: int

    def aplicar(self, documento: EditDocument) -> EditDocument:
        indice, capa = _capa(documento, self.capa)
        return _con_capa(documento, indice,
                         con_marcas(replace(capa, ganancia_ddb=self.ganancia_ddb),
                                    ("ganancia_ddb",)))

    @property
    def grupo(self) -> str:
        return f"{self.CODIGO}:{self.capa}"


@dataclass(frozen=True)
class SilenciarCapa(Patch):
    CODIGO: ClassVar[str] = "audio.mute"

    capa: str
    silenciada: bool = True

    def aplicar(self, documento: EditDocument) -> EditDocument:
        indice, capa = _capa(documento, self.capa)
        return _con_capa(documento, indice,
                         con_marcas(replace(capa, silenciada=self.silenciada), ("silenciada",)))

    @property
    def grupo(self) -> str:
        return f"{self.CODIGO}:{self.capa}"


@dataclass(frozen=True)
class AsignarAssetAudio(Patch):
    CODIGO: ClassVar[str] = "audio.asset"

    capa: str
    asset: ReferenciaAsset | None

    def aplicar(self, documento: EditDocument) -> EditDocument:
        indice, capa = _capa(documento, self.capa)
        return _con_capa(documento, indice,
                         con_marcas(replace(capa, asset=self.asset), ("asset",)))

    @property
    def grupo(self) -> str:
        return f"{self.CODIGO}:{self.capa}"


@dataclass(frozen=True)
class EstablecerDucking(Patch):
    CODIGO: ClassVar[str] = "audio.ducking"

    ducking_ddb: int | None = None
    activo: bool | None = None

    def aplicar(self, documento: EditDocument) -> EditDocument:
        cambios: dict[str, object] = {}
        if self.ducking_ddb is not None:
            cambios["ducking_ddb"] = self.ducking_ddb
        if self.activo is not None:
            cambios["ducking_activo"] = self.activo
        if not cambios:
            raise ErrorPatchEdicion("El patch de ducking no cambia nada.")
        return documento.con(
            audio_track=con_marcas(replace(documento.audio_track, **cambios), tuple(cambios)))


# --------------------------------------------------------------------------- #
# Efectos
# --------------------------------------------------------------------------- #

def _evento(documento: EditDocument, evento_id: str) -> tuple[int, EventoEfecto]:
    for indice, evento in enumerate(documento.effect_track.eventos):
        if evento.id == evento_id:
            return indice, evento
    raise ErrorPatchEdicion(f"El documento no tiene el efecto {evento_id!r}.")


def _con_evento(documento: EditDocument, indice: int, evento: EventoEfecto) -> EditDocument:
    eventos = list(documento.effect_track.eventos)
    eventos[indice] = evento
    return documento.con(effect_track=replace(documento.effect_track, eventos=tuple(eventos)))


@dataclass(frozen=True)
class ActivarEfecto(Patch):
    CODIGO: ClassVar[str] = "efectos.activo"

    evento_id: str
    activo: bool = True

    def aplicar(self, documento: EditDocument) -> EditDocument:
        indice, evento = _evento(documento, self.evento_id)
        return _con_evento(documento, indice,
                           con_marcas(replace(evento, activo=self.activo), ("activo",)))

    def impacto(self, documento: EditDocument) -> Rango:
        _, evento = _evento(documento, self.evento_id)
        return evento.rango.expandido(_margen(documento), documento.duracion)

    @property
    def grupo(self) -> str:
        return f"{self.CODIGO}:{self.evento_id}"


@dataclass(frozen=True)
class AjustarParametrosEfecto(Patch):
    CODIGO: ClassVar[str] = "efectos.parametros"

    evento_id: str
    parametros: Mapping[str, ValorParametro]

    def aplicar(self, documento: EditDocument) -> EditDocument:
        indice, evento = _evento(documento, self.evento_id)
        combinados = {**dict(evento.parametros), **dict(self.parametros)}
        return _con_evento(documento, indice,
                           con_marcas(replace(evento, parametros=combinados), ("parametros",)))

    def impacto(self, documento: EditDocument) -> Rango:
        _, evento = _evento(documento, self.evento_id)
        return evento.rango.expandido(_margen(documento), documento.duracion)

    @property
    def grupo(self) -> str:
        return f"{self.CODIGO}:{self.evento_id}"


# --------------------------------------------------------------------------- #
# Superposiciones
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class AgregarSuperposicion(Patch):
    CODIGO: ClassVar[str] = "superposiciones.agregar"

    elemento: Superposicion

    def aplicar(self, documento: EditDocument) -> EditDocument:
        if any(otro.id == self.elemento.id for otro in documento.overlay_track.elementos):
            raise ErrorPatchEdicion(f"Ya existe la superposicion {self.elemento.id!r}.")
        elementos = documento.overlay_track.elementos + (
            con_marcas(self.elemento, ("rango", "anclaje", "escala_milesimas", "opacidad", "asset")),)
        return documento.con(
            overlay_track=con_marcas(replace(documento.overlay_track, elementos=elementos),
                                     ("elementos",)))

    def impacto(self, documento: EditDocument) -> Rango:
        return self.elemento.rango.expandido(_margen(documento), documento.duracion)


@dataclass(frozen=True)
class QuitarSuperposicion(Patch):
    CODIGO: ClassVar[str] = "superposiciones.quitar"

    elemento_id: str

    def aplicar(self, documento: EditDocument) -> EditDocument:
        restantes = tuple(elemento for elemento in documento.overlay_track.elementos
                          if elemento.id != self.elemento_id)
        if len(restantes) == len(documento.overlay_track.elementos):
            raise ErrorPatchEdicion(f"El documento no tiene la superposicion {self.elemento_id!r}.")
        return documento.con(
            overlay_track=con_marcas(replace(documento.overlay_track, elementos=restantes),
                                     ("elementos",)))


# --------------------------------------------------------------------------- #
# Preset, intensidad e intencion de render
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EstablecerPreset(Patch):
    """Cambiar de preset no borra la revision anterior: crea otra.

    Ni el preset ni la intensidad se marcan como override. No son correcciones
    puntuales sino la *entrada* del compilador: marcarlos protegeria justo lo
    que un rerun tiene que poder recalcular.
    """

    CODIGO: ClassVar[str] = "estilo.preset"

    preset: ReferenciaPreset

    def aplicar(self, documento: EditDocument) -> EditDocument:
        return documento.con(preset=self.preset)


@dataclass(frozen=True)
class EstablecerIntensidad(Patch):
    CODIGO: ClassVar[str] = "estilo.intensidad"

    intensidad: int

    def aplicar(self, documento: EditDocument) -> EditDocument:
        return documento.con(intensidad=self.intensidad)


@dataclass(frozen=True)
class EstablecerIntencionRender(Patch):
    CODIGO: ClassVar[str] = "render.intencion"

    campos: Mapping[str, object]

    def aplicar(self, documento: EditDocument) -> EditDocument:
        if not self.campos:
            raise ErrorPatchEdicion("El patch de intencion de render no cambia nada.")
        desconocidos = sorted(set(self.campos) - set(type(documento.render_intent).CAMPOS_MARCABLES))
        if desconocidos:
            raise ErrorPatchEdicion(
                f"La intencion de render no tiene los campos: {', '.join(desconocidos)}.")
        return documento.con(
            render_intent=con_marcas(replace(documento.render_intent, **dict(self.campos)),
                                     tuple(self.campos)))


__all__ = [
    "ActivarEfecto", "AgregarSuperposicion", "AjustarParametrosEfecto", "AjustarRangoCue",
    "AjustarSegmento", "AsignarAssetAudio", "CorregirPalabra", "CorregirTextoCue",
    "DesplazarCaptions", "DividirCue",
    "EstablecerAspecto", "EstablecerDucking", "EstablecerEstiloCaptions", "EstablecerGananciaAudio",
    "EstablecerIntencionRender", "EstablecerIntensidad", "EstablecerModoEncuadre",
    "EstablecerPreset", "EstablecerTransicion", "MoverKeyframeEncuadre", "OcultarCue", "Patch",
    "QuitarSuperposicion", "SilenciarCapa", "UnirCues", "aplicar_patches", "impacto_de", "remapear",
]
