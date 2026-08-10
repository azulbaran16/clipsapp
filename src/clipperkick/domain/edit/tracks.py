"""Colecciones tipadas del documento de edicion.

Cada pista describe una decision editorial, no una operacion de render. No hay
pistas arbitrarias, ni multicamara, ni filtros de usuario: la lista de pistas es
cerrada y cada elemento se valida contra el catalogo integrado.

Dos reglas atraviesan el modulo entero:

1. **Toda posicion es un tick del documento.** Los `source_segments` son la
   unica excepcion parcial: su rango se mide en la *fuente*, y su posicion en el
   clip se deriva acumulando duraciones. Guardar ambas cosas permitiria que se
   contradijesen; derivar una hace que no exista el estado incoherente.
2. **Cada elemento sabe que se le corrigio a mano.** `manual` es el conjunto de
   campos que una persona fijo. No es metadata decorativa: es la unica razon por
   la que un analisis nuevo puede volver a correr sin borrar trabajo humano, y
   por eso viaja dentro del elemento y no en una tabla lateral que alguien
   pudiese olvidar de copiar al duplicar una variante.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import ClassVar

from .catalog import (
    ANCLAJES, ASPECTOS, CAPAS_AUDIO, ESPACIOS_COLOR, ESTILOS_CAPTION, MODOS_ENCUADRE,
    POLITICAS_FALLBACK, TIPOS_ASSET, TRANSICIONES, ValorParametro, exigir_opcion,
    normalizar_parametros,
)
from .errors import ErrorDocumentoEdicion
from .timebase import (
    Fraccion, Rango, exigir_dentro, exigir_entero, exigir_identidades_unicas,
    exigir_identificador, exigir_sin_solapes, exigir_tick, exigir_tick_dentro,
)


#: Escala de todas las magnitudes relativas del documento: milesimas de la
#: dimension correspondiente. Entera para que dos revisiones equivalentes
#: comparen iguales sin tolerancia.
ESCALA = 1000


def _texto(valor: object, campo: str) -> str:
    if not isinstance(valor, str):
        raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser texto.")
    return valor


def _texto_visible(valor: object, campo: str) -> str:
    texto = _texto(valor, campo)
    if not texto.strip():
        raise ErrorDocumentoEdicion(f"El campo '{campo}' no puede estar vacio.")
    return texto


def _booleano(valor: object, campo: str) -> bool:
    if not isinstance(valor, bool):
        raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un booleano.")
    return valor


def _entero_en(valor: object, campo: str, minimo: int, maximo: int) -> int:
    numero = exigir_entero(valor, campo)
    if not minimo <= numero <= maximo:
        raise ErrorDocumentoEdicion(
            f"El campo '{campo}' vale {numero} y tiene que estar entre {minimo} y {maximo}.")
    return numero


def _secuencia(datos: object, campo: str) -> list:
    if isinstance(datos, (str, bytes)) or not isinstance(datos, Sequence):
        raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser una lista.")
    return list(datos)


def _marcas(valor: object, permitidos: frozenset[str], contexto: str) -> frozenset[str]:
    """Conjunto de campos corregidos a mano, restringido a los que existen.

    Se valida contra los campos declarados porque una marca inventada
    —un `texto` en un keyframe, un campo mal escrito— protegeria un campo que no
    existe y dejaria sin proteger el que la persona creia haber fijado.
    """
    if isinstance(valor, (str, bytes)) or not isinstance(valor, (Sequence, frozenset, set)):
        raise ErrorDocumentoEdicion(f"El campo '{contexto}.manual' tiene que ser una lista.")
    marcas = frozenset(_texto(nombre, f"{contexto}.manual") for nombre in valor)
    desconocidas = sorted(marcas - permitidos)
    if desconocidas:
        raise ErrorDocumentoEdicion(
            f"{contexto} marca como manuales campos que no tiene: {', '.join(desconocidas)}.")
    return marcas


def _marcas_documento(marcas: frozenset[str]) -> list[str]:
    return sorted(marcas)


def marcado(elemento: object, campo: str) -> bool:
    """`True` si ese campo del elemento esta fijado a mano."""
    return campo in getattr(elemento, "manual", frozenset())


def es_manual(elemento: object) -> bool:
    """`True` si la persona toco algo del elemento; sobrevive a un rerun."""
    return bool(getattr(elemento, "manual", frozenset()))


def con_marcas(elemento, campos: Sequence[str]):
    """Devuelve el elemento con esos campos anadidos a sus marcas manuales."""
    permitidos = getattr(type(elemento), "CAMPOS_MARCABLES", frozenset())
    nuevas = frozenset(campos)
    desconocidas = sorted(nuevas - permitidos)
    if desconocidas:
        raise ErrorDocumentoEdicion(
            f"{type(elemento).__name__} no puede marcar: {', '.join(desconocidas)}.")
    return replace(elemento, manual=frozenset(elemento.manual) | nuevas)


# --------------------------------------------------------------------------- #
# Assets
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ReferenciaAsset:
    """Asset externo por identidad *y version*.

    La version es obligatoria porque un asset versionado invalida solo los
    eventos que lo usan: sin ella, cambiar una musica obligaria a invalidar el
    draft entero o a no invalidar nada.
    """

    asset_id: str
    version: str
    tipo: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset_id", exigir_identificador(self.asset_id, "asset_id"))
        object.__setattr__(self, "version", exigir_identificador(self.version, "asset.version"))
        object.__setattr__(self, "tipo", exigir_opcion(self.tipo, TIPOS_ASSET, "asset.tipo"))

    @property
    def clave(self) -> str:
        return f"{self.asset_id}@{self.version}"

    def como_documento(self) -> dict[str, object]:
        return {"asset_id": self.asset_id, "version": self.version, "tipo": self.tipo}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "ReferenciaAsset":
        if not isinstance(datos, Mapping):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        return cls(asset_id=_texto(datos.get("asset_id"), f"{campo}.asset_id"),
                   version=_texto(datos.get("version"), f"{campo}.version"),
                   tipo=datos.get("tipo"))


@dataclass(frozen=True)
class ReferenciaPreset:
    """Preset con version. Cambiar cualquiera de las dos crea otra revision."""

    nombre: str
    version: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "nombre", exigir_identificador(self.nombre, "preset.nombre"))
        object.__setattr__(self, "version", exigir_identificador(self.version, "preset.version"))

    def como_documento(self) -> dict[str, object]:
        return {"nombre": self.nombre, "version": self.version}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "ReferenciaPreset":
        if not isinstance(datos, Mapping):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        return cls(nombre=_texto(datos.get("nombre"), f"{campo}.nombre"),
                   version=_texto(datos.get("version"), f"{campo}.version"))


# --------------------------------------------------------------------------- #
# Segmentos de la fuente
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class SourceSegment:
    """Tramo de la fuente que entra en el clip, con su transicion de entrada.

    El rango se mide **en la fuente**. Donde cae dentro del clip no se guarda:
    se deriva acumulando las duraciones de los segmentos anteriores. Guardar las
    dos cosas permitiria que se contradijesen, y no hay ninguna edicion de este
    producto que necesite reordenar tramos.
    """

    CAMPOS_MARCABLES: ClassVar[frozenset[str]] = frozenset({"rango", "transicion_entrada"})

    id: str
    rango: Rango
    transicion_entrada: str = "corte"
    manual: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", exigir_identificador(self.id, "segmento.id"))
        if not isinstance(self.rango, Rango):
            raise ErrorDocumentoEdicion("Un segmento necesita su rango en la fuente.")
        object.__setattr__(self, "transicion_entrada",
                           exigir_opcion(self.transicion_entrada, TRANSICIONES,
                                         f"segmento {self.id}.transicion_entrada"))
        object.__setattr__(self, "manual",
                           _marcas(self.manual, self.CAMPOS_MARCABLES, f"segmento {self.id}"))

    @property
    def duracion(self) -> int:
        return self.rango.duracion

    def como_documento(self) -> dict[str, object]:
        return {"id": self.id, "rango": self.rango.como_documento(),
                "transicion_entrada": self.transicion_entrada,
                "manual": _marcas_documento(self.manual)}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "SourceSegment":
        if not isinstance(datos, Mapping):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        return cls(id=_texto(datos.get("id"), f"{campo}.id"),
                   rango=Rango.desde_documento(datos.get("rango"), f"{campo}.rango"),
                   transicion_entrada=datos.get("transicion_entrada", "corte"),
                   manual=datos.get("manual", ()))


# --------------------------------------------------------------------------- #
# Encuadre
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class VentanaEncuadre:
    """Ventana sobre el fotograma de la fuente, en milesimas de sus dimensiones.

    Relativa y entera para que la misma revision valga para cualquier resolucion
    de proxy o de salida: el documento describe *que* se ve, no cuantos pixeles.
    """

    x: int
    y: int
    ancho: int
    alto: int

    def __post_init__(self) -> None:
        for campo in ("x", "y"):
            object.__setattr__(self, campo, _entero_en(getattr(self, campo), f"ventana.{campo}", 0, ESCALA))
        for campo in ("ancho", "alto"):
            object.__setattr__(self, campo, _entero_en(getattr(self, campo), f"ventana.{campo}", 1, ESCALA))
        if self.x + self.ancho > ESCALA or self.y + self.alto > ESCALA:
            raise ErrorDocumentoEdicion(
                "La ventana de encuadre se sale del fotograma de la fuente.")

    def como_documento(self) -> dict[str, object]:
        return {"x": self.x, "y": self.y, "ancho": self.ancho, "alto": self.alto}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "VentanaEncuadre":
        if not isinstance(datos, Mapping):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        return cls(x=exigir_entero(datos.get("x"), f"{campo}.x"),
                   y=exigir_entero(datos.get("y"), f"{campo}.y"),
                   ancho=exigir_entero(datos.get("ancho"), f"{campo}.ancho"),
                   alto=exigir_entero(datos.get("alto"), f"{campo}.alto"))


@dataclass(frozen=True)
class KeyframeEncuadre:
    CAMPOS_MARCABLES: ClassVar[frozenset[str]] = frozenset({"ventana", "modo", "tick"})

    id: str
    tick: int
    ventana: VentanaEncuadre
    modo: str = "gameplay"
    manual: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", exigir_identificador(self.id, "keyframe.id"))
        object.__setattr__(self, "tick", exigir_tick(self.tick, f"keyframe {self.id}.tick"))
        if not isinstance(self.ventana, VentanaEncuadre):
            raise ErrorDocumentoEdicion(f"El keyframe {self.id!r} necesita su ventana.")
        object.__setattr__(self, "modo",
                           exigir_opcion(self.modo, MODOS_ENCUADRE, f"keyframe {self.id}.modo"))
        object.__setattr__(self, "manual",
                           _marcas(self.manual, self.CAMPOS_MARCABLES, f"keyframe {self.id}"))

    def como_documento(self) -> dict[str, object]:
        return {"id": self.id, "tick": self.tick, "ventana": self.ventana.como_documento(),
                "modo": self.modo, "manual": _marcas_documento(self.manual)}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "KeyframeEncuadre":
        if not isinstance(datos, Mapping):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        return cls(id=_texto(datos.get("id"), f"{campo}.id"),
                   tick=exigir_tick(datos.get("tick"), f"{campo}.tick"),
                   ventana=VentanaEncuadre.desde_documento(datos.get("ventana"), f"{campo}.ventana"),
                   modo=datos.get("modo", "gameplay"),
                   manual=datos.get("manual", ()))


@dataclass(frozen=True)
class FramingTrack:
    """Aspecto de salida y keyframes interpolables de ventana/sujeto."""

    CAMPOS_MARCABLES: ClassVar[frozenset[str]] = frozenset({"aspecto", "keyframes"})

    aspecto: str = "9:16"
    keyframes: tuple[KeyframeEncuadre, ...] = ()
    manual: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "aspecto", exigir_opcion(self.aspecto, ASPECTOS, "encuadre.aspecto"))
        keyframes = tuple(self.keyframes)
        if not keyframes:
            raise ErrorDocumentoEdicion("El encuadre necesita al menos un keyframe.")
        keyframes = tuple(sorted(keyframes, key=lambda k: (k.tick, k.id)))
        exigir_identidades_unicas(tuple(k.id for k in keyframes), "El encuadre")
        for previo, siguiente in zip(keyframes, keyframes[1:]):
            if previo.tick == siguiente.tick:
                raise ErrorDocumentoEdicion(
                    f"El encuadre tiene dos keyframes en el tick {previo.tick}.")
        if keyframes[0].tick != 0:
            raise ErrorDocumentoEdicion(
                "El encuadre necesita un keyframe en el tick 0: sin el, el primer fotograma"
                " no tiene ventana definida.")
        object.__setattr__(self, "keyframes", keyframes)
        object.__setattr__(self, "manual", _marcas(self.manual, self.CAMPOS_MARCABLES, "encuadre"))

    def validar(self, duracion: int) -> None:
        for keyframe in self.keyframes:
            exigir_tick_dentro(keyframe.tick, duracion, f"El keyframe {keyframe.id!r}")

    def como_documento(self) -> dict[str, object]:
        return {"aspecto": self.aspecto,
                "keyframes": [k.como_documento() for k in self.keyframes],
                "manual": _marcas_documento(self.manual)}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "FramingTrack":
        if not isinstance(datos, Mapping):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        return cls(aspecto=datos.get("aspecto", "9:16"),
                   keyframes=tuple(
                       KeyframeEncuadre.desde_documento(entrada, f"{campo}.keyframes[{indice}]")
                       for indice, entrada in enumerate(_secuencia(datos.get("keyframes", ()),
                                                                   f"{campo}.keyframes"))),
                   manual=datos.get("manual", ()))


# --------------------------------------------------------------------------- #
# Subtitulos
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Palabra:
    """Palabra con su tiempo propio y su confianza, en milesimas."""

    CAMPOS_MARCABLES: ClassVar[frozenset[str]] = frozenset({"texto", "rango"})

    id: str
    texto: str
    rango: Rango
    confianza: int = ESCALA
    manual: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", exigir_identificador(self.id, "palabra.id"))
        object.__setattr__(self, "texto", _texto_visible(self.texto, "palabra.texto"))
        if not isinstance(self.rango, Rango):
            raise ErrorDocumentoEdicion("Una palabra necesita su rango.")
        object.__setattr__(self, "confianza",
                           _entero_en(self.confianza, "palabra.confianza", 0, ESCALA))
        object.__setattr__(self, "manual", _marcas(self.manual, self.CAMPOS_MARCABLES, "palabra"))

    def como_documento(self) -> dict[str, object]:
        return {"id": self.id, "texto": self.texto, "rango": self.rango.como_documento(),
                "confianza": self.confianza, "manual": _marcas_documento(self.manual)}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "Palabra":
        if not isinstance(datos, Mapping):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        return cls(id=_texto(datos.get("id"), f"{campo}.id"),
                   texto=_texto(datos.get("texto"), f"{campo}.texto"),
                   rango=Rango.desde_documento(datos.get("rango"), f"{campo}.rango"),
                   confianza=datos.get("confianza", ESCALA),
                   manual=datos.get("manual", ()))


@dataclass(frozen=True)
class Cue:
    """Grupo de subtitulo. Editar su texto **no** mueve sus tiempos.

    Es la regla que el recorrido del borrador exige por escrito: corregir una
    palabra mal transcrita no puede desplazar el cue, porque realinear es una
    accion explicita y distinta. Por eso `texto` y `rango` son campos marcables
    por separado y un rerun de analisis puede reponer uno sin tocar el otro.
    """

    CAMPOS_MARCABLES: ClassVar[frozenset[str]] = frozenset({"texto", "rango", "palabras", "oculto"})

    id: str
    rango: Rango
    texto: str
    palabras: tuple[Palabra, ...] = ()
    oculto: bool = False
    manual: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", exigir_identificador(self.id, "cue.id"))
        if not isinstance(self.rango, Rango):
            raise ErrorDocumentoEdicion(f"El cue {self.id!r} necesita su rango.")
        object.__setattr__(self, "texto", _texto_visible(self.texto, f"cue {self.id}.texto"))
        object.__setattr__(self, "oculto", _booleano(self.oculto, f"cue {self.id}.oculto"))
        palabras = tuple(sorted(self.palabras, key=lambda p: (p.rango.inicio, p.rango.fin)))
        exigir_identidades_unicas(tuple(p.id for p in palabras), f"El cue {self.id!r}")
        exigir_sin_solapes(tuple(p.rango for p in palabras), f"El cue {self.id!r}")
        for palabra in palabras:
            if palabra.rango.inicio < self.rango.inicio or palabra.rango.fin > self.rango.fin:
                raise ErrorDocumentoEdicion(
                    f"El cue {self.id!r} tiene la palabra {palabra.texto!r} fuera de su rango.")
        object.__setattr__(self, "palabras", palabras)
        object.__setattr__(self, "manual",
                           _marcas(self.manual, self.CAMPOS_MARCABLES, f"cue {self.id}"))

    def como_documento(self) -> dict[str, object]:
        return {"id": self.id, "rango": self.rango.como_documento(), "texto": self.texto,
                "palabras": [p.como_documento() for p in self.palabras],
                "oculto": self.oculto, "manual": _marcas_documento(self.manual)}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "Cue":
        if not isinstance(datos, Mapping):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        return cls(id=_texto(datos.get("id"), f"{campo}.id"),
                   rango=Rango.desde_documento(datos.get("rango"), f"{campo}.rango"),
                   texto=_texto(datos.get("texto"), f"{campo}.texto"),
                   palabras=tuple(
                       Palabra.desde_documento(entrada, f"{campo}.palabras[{indice}]")
                       for indice, entrada in enumerate(_secuencia(datos.get("palabras", ()),
                                                                   f"{campo}.palabras"))),
                   oculto=datos.get("oculto", False),
                   manual=datos.get("manual", ()))


@dataclass(frozen=True)
class CaptionTrack:
    CAMPOS_MARCABLES: ClassVar[frozenset[str]] = frozenset(
        {"cues", "estilo", "tamano_relativo", "desfase"})

    cues: tuple[Cue, ...] = ()
    estilo: str = "limpio"
    tamano_relativo: int = 100
    #: Desfase global en ticks. Puede ser negativo: adelantar los subtitulos es
    #: una correccion tan legitima como retrasarlos.
    desfase: int = 0
    manual: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        cues = tuple(sorted(self.cues, key=lambda c: (c.rango.inicio, c.id)))
        exigir_identidades_unicas(tuple(c.id for c in cues), "Los subtitulos")
        exigir_sin_solapes(tuple(c.rango for c in cues), "Los subtitulos")
        object.__setattr__(self, "cues", cues)
        object.__setattr__(self, "estilo",
                           exigir_opcion(self.estilo, ESTILOS_CAPTION, "subtitulos.estilo"))
        object.__setattr__(self, "tamano_relativo",
                           _entero_en(self.tamano_relativo, "subtitulos.tamano_relativo", 50, 200))
        object.__setattr__(self, "desfase", exigir_entero(self.desfase, "subtitulos.desfase"))
        object.__setattr__(self, "manual", _marcas(self.manual, self.CAMPOS_MARCABLES, "subtitulos"))

    def validar(self, duracion: int) -> None:
        for cue in self.cues:
            exigir_dentro(cue.rango, duracion, f"El cue {cue.id!r}")
        if abs(self.desfase) > duracion:
            raise ErrorDocumentoEdicion(
                f"El desfase de subtitulos ({self.desfase}) supera la duracion del documento.")

    def cue(self, cue_id: str) -> Cue | None:
        return next((cue for cue in self.cues if cue.id == cue_id), None)

    def como_documento(self) -> dict[str, object]:
        return {"cues": [c.como_documento() for c in self.cues], "estilo": self.estilo,
                "tamano_relativo": self.tamano_relativo, "desfase": self.desfase,
                "manual": _marcas_documento(self.manual)}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "CaptionTrack":
        if not isinstance(datos, Mapping):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        return cls(cues=tuple(Cue.desde_documento(entrada, f"{campo}.cues[{indice}]")
                              for indice, entrada in enumerate(
                                  _secuencia(datos.get("cues", ()), f"{campo}.cues"))),
                   estilo=datos.get("estilo", "limpio"),
                   tamano_relativo=datos.get("tamano_relativo", 100),
                   desfase=datos.get("desfase", 0),
                   manual=datos.get("manual", ()))


# --------------------------------------------------------------------------- #
# Audio
# --------------------------------------------------------------------------- #

#: Ganancias en decimas de decibelio. Entero por la misma razon que el tiempo:
#: `-6.0 dB` escrito por dos caminos distintos tiene que ser el mismo documento.
GANANCIA_MINIMA = -600
GANANCIA_MAXIMA = 120


@dataclass(frozen=True)
class PuntoEnvolvente:
    tick: int
    ganancia_ddb: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "tick", exigir_tick(self.tick, "envolvente.tick"))
        object.__setattr__(self, "ganancia_ddb",
                           _entero_en(self.ganancia_ddb, "envolvente.ganancia_ddb",
                                      GANANCIA_MINIMA, GANANCIA_MAXIMA))

    def como_documento(self) -> dict[str, object]:
        return {"tick": self.tick, "ganancia_ddb": self.ganancia_ddb}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "PuntoEnvolvente":
        if not isinstance(datos, Mapping):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        return cls(tick=exigir_tick(datos.get("tick"), f"{campo}.tick"),
                   ganancia_ddb=datos.get("ganancia_ddb", 0))


@dataclass(frozen=True)
class CapaAudio:
    CAMPOS_MARCABLES: ClassVar[frozenset[str]] = frozenset(
        {"ganancia_ddb", "silenciada", "envolvente", "asset"})

    capa: str
    ganancia_ddb: int = 0
    silenciada: bool = False
    envolvente: tuple[PuntoEnvolvente, ...] = ()
    asset: ReferenciaAsset | None = None
    manual: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "capa", exigir_opcion(self.capa, CAPAS_AUDIO, "audio.capa"))
        object.__setattr__(self, "ganancia_ddb",
                           _entero_en(self.ganancia_ddb, f"audio.{self.capa}.ganancia_ddb",
                                      GANANCIA_MINIMA, GANANCIA_MAXIMA))
        object.__setattr__(self, "silenciada",
                           _booleano(self.silenciada, f"audio.{self.capa}.silenciada"))
        envolvente = tuple(sorted(self.envolvente, key=lambda p: p.tick))
        for previo, siguiente in zip(envolvente, envolvente[1:]):
            if previo.tick == siguiente.tick:
                raise ErrorDocumentoEdicion(
                    f"La envolvente de {self.capa!r} tiene dos puntos en el tick {previo.tick}.")
        object.__setattr__(self, "envolvente", envolvente)
        if self.asset is not None:
            if not isinstance(self.asset, ReferenciaAsset):
                raise ErrorDocumentoEdicion(f"La capa {self.capa!r} declara un asset ilegible.")
            if self.capa == "voz":
                # La voz sale de la fuente. Un asset ahi describiria una locucion
                # sustituta, que este producto no edita y el compilador no sabria
                # mezclar sin inventarse una politica.
                raise ErrorDocumentoEdicion("La capa de voz no admite un asset externo.")
            esperado = "musica" if self.capa == "bgm" else "sfx"
            if self.asset.tipo != esperado:
                raise ErrorDocumentoEdicion(
                    f"La capa {self.capa!r} necesita un asset {esperado!r}, no"
                    f" {self.asset.tipo!r}.")
        object.__setattr__(self, "manual",
                           _marcas(self.manual, self.CAMPOS_MARCABLES, f"audio.{self.capa}"))

    def validar(self, duracion: int) -> None:
        for punto in self.envolvente:
            exigir_tick_dentro(punto.tick, duracion, f"La envolvente de {self.capa!r}")

    def como_documento(self) -> dict[str, object]:
        return {"capa": self.capa, "ganancia_ddb": self.ganancia_ddb,
                "silenciada": self.silenciada,
                "envolvente": [p.como_documento() for p in self.envolvente],
                "asset": self.asset.como_documento() if self.asset is not None else None,
                "manual": _marcas_documento(self.manual)}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "CapaAudio":
        if not isinstance(datos, Mapping):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        asset = datos.get("asset")
        return cls(capa=datos.get("capa"),
                   ganancia_ddb=datos.get("ganancia_ddb", 0),
                   silenciada=datos.get("silenciada", False),
                   envolvente=tuple(
                       PuntoEnvolvente.desde_documento(entrada, f"{campo}.envolvente[{indice}]")
                       for indice, entrada in enumerate(
                           _secuencia(datos.get("envolvente", ()), f"{campo}.envolvente"))),
                   asset=None if asset is None else ReferenciaAsset.desde_documento(
                       asset, f"{campo}.asset"),
                   manual=datos.get("manual", ()))


@dataclass(frozen=True)
class AudioTrack:
    """Las tres capas conceptuales, siempre las tres y siempre en el mismo orden.

    Que existan todas —aunque esten silenciadas— es lo que hace que el documento
    sea canonico: si una capa pudiese faltar, "sin bgm" y "bgm a -inf" serian dos
    documentos distintos para el mismo clip y la comparacion de revisiones
    dependeria de cual escribio la UI.
    """

    CAMPOS_MARCABLES: ClassVar[frozenset[str]] = frozenset({"ducking_ddb", "ducking_activo"})

    capas: tuple[CapaAudio, ...] = ()
    #: Cuanto baja el resto cuando hay voz. Negativo o cero: subir el fondo bajo
    #: la voz no es ducking, es otro efecto.
    ducking_ddb: int = -90
    ducking_activo: bool = True
    manual: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        capas = tuple(self.capas) or tuple(CapaAudio(capa=nombre) for nombre in CAPAS_AUDIO)
        presentes = tuple(capa.capa for capa in capas)
        if sorted(presentes) != sorted(CAPAS_AUDIO):
            raise ErrorDocumentoEdicion(
                f"El audio declara las capas {presentes} y tiene que declarar exactamente"
                f" {CAPAS_AUDIO}.")
        orden = {nombre: indice for indice, nombre in enumerate(CAPAS_AUDIO)}
        object.__setattr__(self, "capas", tuple(sorted(capas, key=lambda c: orden[c.capa])))
        object.__setattr__(self, "ducking_ddb",
                           _entero_en(self.ducking_ddb, "audio.ducking_ddb", GANANCIA_MINIMA, 0))
        object.__setattr__(self, "ducking_activo",
                           _booleano(self.ducking_activo, "audio.ducking_activo"))
        object.__setattr__(self, "manual", _marcas(self.manual, self.CAMPOS_MARCABLES, "audio"))

    def validar(self, duracion: int) -> None:
        for capa in self.capas:
            capa.validar(duracion)

    def capa(self, nombre: str) -> CapaAudio:
        for capa in self.capas:
            if capa.capa == nombre:
                return capa
        raise ErrorDocumentoEdicion(f"El audio no declara la capa {nombre!r}.")

    def como_documento(self) -> dict[str, object]:
        return {"capas": [c.como_documento() for c in self.capas],
                "ducking_ddb": self.ducking_ddb, "ducking_activo": self.ducking_activo,
                "manual": _marcas_documento(self.manual)}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "AudioTrack":
        if not isinstance(datos, Mapping):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        return cls(capas=tuple(CapaAudio.desde_documento(entrada, f"{campo}.capas[{indice}]")
                               for indice, entrada in enumerate(
                                   _secuencia(datos.get("capas", ()), f"{campo}.capas"))),
                   ducking_ddb=datos.get("ducking_ddb", -90),
                   ducking_activo=datos.get("ducking_activo", True),
                   manual=datos.get("manual", ()))


# --------------------------------------------------------------------------- #
# Efectos
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class EventoEfecto:
    """Evento del catalogo integrado, con sus parametros ya normalizados.

    `activo` existe para que desactivar un efecto sugerido sea una correccion
    reversible y no un borrado: la persona tiene que poder volver a encenderlo
    sin que el analisis lo reponga a sus espaldas.
    """

    CAMPOS_MARCABLES: ClassVar[frozenset[str]] = frozenset({"rango", "parametros", "activo"})

    id: str
    tipo: str
    rango: Rango
    parametros: Mapping[str, ValorParametro] = field(default_factory=dict)
    activo: bool = True
    manual: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", exigir_identificador(self.id, "efecto.id"))
        object.__setattr__(self, "tipo", _texto(self.tipo, "efecto.tipo"))
        if not isinstance(self.rango, Rango):
            raise ErrorDocumentoEdicion(f"El efecto {self.id!r} necesita su rango.")
        object.__setattr__(self, "activo", _booleano(self.activo, f"efecto {self.id}.activo"))
        object.__setattr__(self, "parametros",
                           normalizar_parametros(self.tipo, self.parametros, f"efecto {self.id}"))
        object.__setattr__(self, "manual",
                           _marcas(self.manual, self.CAMPOS_MARCABLES, f"efecto {self.id}"))

    def como_documento(self) -> dict[str, object]:
        return {"id": self.id, "tipo": self.tipo, "rango": self.rango.como_documento(),
                "parametros": dict(self.parametros), "activo": self.activo,
                "manual": _marcas_documento(self.manual)}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "EventoEfecto":
        if not isinstance(datos, Mapping):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        parametros = datos.get("parametros", {})
        if not isinstance(parametros, Mapping):
            raise ErrorDocumentoEdicion(f"El campo '{campo}.parametros' tiene que ser un objeto.")
        return cls(id=_texto(datos.get("id"), f"{campo}.id"),
                   tipo=_texto(datos.get("tipo"), f"{campo}.tipo"),
                   rango=Rango.desde_documento(datos.get("rango"), f"{campo}.rango"),
                   parametros=parametros, activo=datos.get("activo", True),
                   manual=datos.get("manual", ()))


@dataclass(frozen=True)
class EffectTrack:
    """Eventos de estilo. Se permiten solapes: un destello sobre un zoom es una
    combinacion legitima y el compilador sabe ordenarlos por tipo, no por
    posicion en la lista."""

    CAMPOS_MARCABLES: ClassVar[frozenset[str]] = frozenset({"eventos"})

    eventos: tuple[EventoEfecto, ...] = ()
    manual: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        eventos = tuple(sorted(self.eventos, key=lambda e: (e.rango.inicio, e.tipo, e.id)))
        exigir_identidades_unicas(tuple(e.id for e in eventos), "Los efectos")
        object.__setattr__(self, "eventos", eventos)
        object.__setattr__(self, "manual", _marcas(self.manual, self.CAMPOS_MARCABLES, "efectos"))

    def validar(self, duracion: int) -> None:
        for evento in self.eventos:
            exigir_dentro(evento.rango, duracion, f"El efecto {evento.id!r}")

    def evento(self, evento_id: str) -> EventoEfecto | None:
        return next((evento for evento in self.eventos if evento.id == evento_id), None)

    def como_documento(self) -> dict[str, object]:
        return {"eventos": [e.como_documento() for e in self.eventos],
                "manual": _marcas_documento(self.manual)}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "EffectTrack":
        if not isinstance(datos, Mapping):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        return cls(eventos=tuple(
            EventoEfecto.desde_documento(entrada, f"{campo}.eventos[{indice}]")
            for indice, entrada in enumerate(_secuencia(datos.get("eventos", ()),
                                                        f"{campo}.eventos"))),
            manual=datos.get("manual", ()))


# --------------------------------------------------------------------------- #
# Superposiciones (branding y graficos)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Superposicion:
    CAMPOS_MARCABLES: ClassVar[frozenset[str]] = frozenset(
        {"rango", "anclaje", "escala_milesimas", "opacidad", "asset"})

    id: str
    asset: ReferenciaAsset
    rango: Rango
    anclaje: str = "inferior_derecha"
    escala_milesimas: int = 150
    opacidad: int = 100
    manual: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", exigir_identificador(self.id, "superposicion.id"))
        if not isinstance(self.asset, ReferenciaAsset):
            raise ErrorDocumentoEdicion(f"La superposicion {self.id!r} necesita su asset.")
        if self.asset.tipo != "grafico":
            raise ErrorDocumentoEdicion(
                f"La superposicion {self.id!r} necesita un asset grafico, no"
                f" {self.asset.tipo!r}.")
        if not isinstance(self.rango, Rango):
            raise ErrorDocumentoEdicion(f"La superposicion {self.id!r} necesita su rango.")
        object.__setattr__(self, "anclaje",
                           exigir_opcion(self.anclaje, ANCLAJES, f"superposicion {self.id}.anclaje"))
        object.__setattr__(self, "escala_milesimas",
                           _entero_en(self.escala_milesimas,
                                      f"superposicion {self.id}.escala_milesimas", 10, 2000))
        object.__setattr__(self, "opacidad",
                           _entero_en(self.opacidad, f"superposicion {self.id}.opacidad", 0, 100))
        object.__setattr__(self, "manual",
                           _marcas(self.manual, self.CAMPOS_MARCABLES, f"superposicion {self.id}"))

    def como_documento(self) -> dict[str, object]:
        return {"id": self.id, "asset": self.asset.como_documento(),
                "rango": self.rango.como_documento(), "anclaje": self.anclaje,
                "escala_milesimas": self.escala_milesimas, "opacidad": self.opacidad,
                "manual": _marcas_documento(self.manual)}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "Superposicion":
        if not isinstance(datos, Mapping):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        return cls(id=_texto(datos.get("id"), f"{campo}.id"),
                   asset=ReferenciaAsset.desde_documento(datos.get("asset"), f"{campo}.asset"),
                   rango=Rango.desde_documento(datos.get("rango"), f"{campo}.rango"),
                   anclaje=datos.get("anclaje", "inferior_derecha"),
                   escala_milesimas=datos.get("escala_milesimas", 150),
                   opacidad=datos.get("opacidad", 100),
                   manual=datos.get("manual", ()))


@dataclass(frozen=True)
class OverlayTrack:
    CAMPOS_MARCABLES: ClassVar[frozenset[str]] = frozenset({"elementos"})

    elementos: tuple[Superposicion, ...] = ()
    manual: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        elementos = tuple(sorted(self.elementos, key=lambda s: (s.rango.inicio, s.id)))
        exigir_identidades_unicas(tuple(s.id for s in elementos), "Las superposiciones")
        object.__setattr__(self, "elementos", elementos)
        object.__setattr__(self, "manual",
                           _marcas(self.manual, self.CAMPOS_MARCABLES, "superposiciones"))

    def validar(self, duracion: int) -> None:
        for elemento in self.elementos:
            exigir_dentro(elemento.rango, duracion, f"La superposicion {elemento.id!r}")

    def como_documento(self) -> dict[str, object]:
        return {"elementos": [s.como_documento() for s in self.elementos],
                "manual": _marcas_documento(self.manual)}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "OverlayTrack":
        if not isinstance(datos, Mapping):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        return cls(elementos=tuple(
            Superposicion.desde_documento(entrada, f"{campo}.elementos[{indice}]")
            for indice, entrada in enumerate(_secuencia(datos.get("elementos", ()),
                                                        f"{campo}.elementos"))),
            manual=datos.get("manual", ()))


# --------------------------------------------------------------------------- #
# Intencion de render
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class ZonaSegura:
    """Margenes en milesimas que ninguna caption ni grafico deben invadir."""

    superior: int = 80
    inferior: int = 120
    izquierda: int = 50
    derecha: int = 50

    def __post_init__(self) -> None:
        for campo in ("superior", "inferior", "izquierda", "derecha"):
            object.__setattr__(self, campo,
                               _entero_en(getattr(self, campo), f"zona_segura.{campo}", 0, 400))
        if self.superior + self.inferior >= ESCALA or self.izquierda + self.derecha >= ESCALA:
            raise ErrorDocumentoEdicion("La zona segura no deja area util.")

    def como_documento(self) -> dict[str, object]:
        return {"superior": self.superior, "inferior": self.inferior,
                "izquierda": self.izquierda, "derecha": self.derecha}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "ZonaSegura":
        if not isinstance(datos, Mapping):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        return cls(superior=datos.get("superior", 80), inferior=datos.get("inferior", 120),
                   izquierda=datos.get("izquierda", 50), derecha=datos.get("derecha", 50))


@dataclass(frozen=True)
class RenderIntent:
    """Fps logico, color, zona segura y politica de fallback.

    Es *intencion*, no configuracion de codec: la revision no dice con que
    encoder se materializa, porque eso cambia con la maquina y no puede
    convertir la misma revision en dos documentos distintos.
    """

    CAMPOS_MARCABLES: ClassVar[frozenset[str]] = frozenset(
        {"fps_logico", "espacio_color", "zona_segura", "politica_fallback", "captions_quemados"})

    fps_logico: Fraccion = Fraccion(30, 1)
    espacio_color: str = "bt709"
    zona_segura: ZonaSegura = ZonaSegura()
    politica_fallback: str = "degradar"
    captions_quemados: bool = True
    manual: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.fps_logico, Fraccion):
            raise ErrorDocumentoEdicion("La intencion de render necesita su fps logico.")
        if self.fps_logico.numerador > 240 * self.fps_logico.denominador:
            raise ErrorDocumentoEdicion(
                f"El fps logico {self.fps_logico} supera los 240 fotogramas por segundo.")
        object.__setattr__(self, "espacio_color",
                           exigir_opcion(self.espacio_color, ESPACIOS_COLOR, "render.espacio_color"))
        if not isinstance(self.zona_segura, ZonaSegura):
            raise ErrorDocumentoEdicion("La intencion de render necesita su zona segura.")
        object.__setattr__(self, "politica_fallback",
                           exigir_opcion(self.politica_fallback, POLITICAS_FALLBACK,
                                         "render.politica_fallback"))
        object.__setattr__(self, "captions_quemados",
                           _booleano(self.captions_quemados, "render.captions_quemados"))
        object.__setattr__(self, "manual", _marcas(self.manual, self.CAMPOS_MARCABLES, "render"))

    def como_documento(self) -> dict[str, object]:
        return {"fps_logico": self.fps_logico.como_documento(), "espacio_color": self.espacio_color,
                "zona_segura": self.zona_segura.como_documento(),
                "politica_fallback": self.politica_fallback,
                "captions_quemados": self.captions_quemados,
                "manual": _marcas_documento(self.manual)}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "RenderIntent":
        if not isinstance(datos, Mapping):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        return cls(fps_logico=Fraccion.desde_documento(datos.get("fps_logico"),
                                                       f"{campo}.fps_logico"),
                   espacio_color=datos.get("espacio_color", "bt709"),
                   zona_segura=ZonaSegura.desde_documento(datos.get("zona_segura", {}),
                                                          f"{campo}.zona_segura"),
                   politica_fallback=datos.get("politica_fallback", "degradar"),
                   captions_quemados=datos.get("captions_quemados", True),
                   manual=datos.get("manual", ()))


__all__ = [
    "AudioTrack", "CapaAudio", "CaptionTrack", "Cue", "ESCALA", "EffectTrack", "EventoEfecto",
    "FramingTrack", "GANANCIA_MAXIMA", "GANANCIA_MINIMA", "KeyframeEncuadre", "OverlayTrack",
    "Palabra", "PuntoEnvolvente", "ReferenciaAsset", "ReferenciaPreset", "RenderIntent",
    "SourceSegment", "Superposicion", "VentanaEncuadre", "ZonaSegura", "con_marcas", "es_manual",
    "marcado",
]
