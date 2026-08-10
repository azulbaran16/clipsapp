"""Catalogo integrado: lo unico que un documento puede pedir.

El documento no acepta filtros arbitrarios ni parametros libres. Cada efecto,
modo de encuadre, transicion o capa de audio existe aqui con su lista cerrada de
parametros, su tipo y su rango. Fuera de esta tabla no hay nada que un
`EditDocument` pueda nombrar.

Dos consecuencias deliberadas:

1. **Los parametros se normalizan, no se completan a mano.** Un evento que
   omite un parametro recibe su valor predeterminado *al construirse*, de modo
   que dos documentos que describen lo mismo se serializan igual. Sin eso, la
   igualdad entre revisiones dependeria de que la UI escribiese siempre el mismo
   subconjunto de claves.
2. **Todo valor numerico es entero.** Las magnitudes que conceptualmente son
   fraccionarias —una ganancia, un factor de velocidad— se expresan en milesimas
   o en decibelios por decima. Un `float` en el documento reintroduciria por la
   puerta de atras el problema que la timebase entera resuelve.

Anadir una operacion es anadir una entrada aqui y una prueba golden del plan
que la compila. Nunca se habilita en la UI antes de que exista esa equivalencia.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Union

from .errors import ErrorCatalogoEdicion, ErrorDocumentoEdicion
from .timebase import exigir_entero


ValorParametro = Union[int, bool, str]


@dataclass(frozen=True)
class ParametroEntero:
    predeterminado: int
    minimo: int
    maximo: int

    def validar(self, valor: object, contexto: str) -> int:
        numero = exigir_entero(valor, contexto)
        if not self.minimo <= numero <= self.maximo:
            raise ErrorCatalogoEdicion(
                f"{contexto} vale {numero} y el catalogo admite de {self.minimo} a {self.maximo}.")
        return numero


@dataclass(frozen=True)
class ParametroBooleano:
    predeterminado: bool

    def validar(self, valor: object, contexto: str) -> bool:
        if not isinstance(valor, bool):
            raise ErrorCatalogoEdicion(f"{contexto} tiene que ser un booleano.")
        return valor


@dataclass(frozen=True)
class ParametroOpcion:
    predeterminado: str
    opciones: tuple[str, ...]

    def validar(self, valor: object, contexto: str) -> str:
        if not isinstance(valor, str) or valor not in self.opciones:
            admitidas = ", ".join(sorted(self.opciones))
            raise ErrorCatalogoEdicion(f"{contexto} vale {valor!r} y el catalogo admite: {admitidas}.")
        return valor


Parametro = Union[ParametroEntero, ParametroBooleano, ParametroOpcion]


# --------------------------------------------------------------------------- #
# Vocabularios cerrados
# --------------------------------------------------------------------------- #

#: Aspecto del encuadre. El documento no describe resoluciones: eso es una
#: decision del plan de render, y fijarla aqui ataria la revision a un perfil.
ASPECTOS = ("9:16", "16:9", "1:1")

#: Que sigue el encuadre. `manual` es el unico que exige ventana explicita en
#: cada keyframe; los demas la proponen y la persona puede corregirla.
MODOS_ENCUADRE = ("gameplay", "webcam", "rostro", "manual")

#: Transicion permitida entre dos segmentos de la fuente. No hay catalogo libre:
#: son las tres que preview y salida final saben producir igual.
TRANSICIONES = ("corte", "fundido", "desde_negro")

#: Capas conceptuales de audio. La UI habla de estas cuatro; el mezclador real
#: es asunto del plan de render.
CAPAS_AUDIO = ("voz", "bgm", "sfx")

ESTILOS_CAPTION = ("limpio", "bloque", "karaoke")

ANCLAJES = ("superior_izquierda", "superior_centro", "superior_derecha",
            "centro", "inferior_izquierda", "inferior_centro", "inferior_derecha")

ESPACIOS_COLOR = ("bt709", "bt601")

#: Que hacer cuando una operacion no se puede materializar. Nunca es "callar":
#: la persona tiene que poder distinguir un clip degradado de uno completo.
POLITICAS_FALLBACK = ("degradar", "omitir", "detener")

#: Tipos de asset que el documento puede referenciar. El registro real —origen,
#: licencia, version— es de T10; aqui solo se declara que clase de cosa es.
TIPOS_ASSET = ("musica", "sfx", "grafico", "fuente")


# --------------------------------------------------------------------------- #
# Efectos
# --------------------------------------------------------------------------- #

#: Cada efecto declara sus parametros con tipo, rango y valor predeterminado.
#: La intensidad global del preset modula *frecuencia y magnitud* de estos
#: eventos; no habilita parametros nuevos ni valores fuera de rango.
CATALOGO_EFECTOS: Mapping[str, Mapping[str, Parametro]] = {
    "zoom_punch": {
        "magnitud": ParametroEntero(50, 0, 100),
    },
    "zoom_lento": {
        "magnitud": ParametroEntero(20, 0, 100),
        "hacia_dentro": ParametroBooleano(True),
    },
    "shake": {
        "magnitud": ParametroEntero(30, 0, 100),
        "frecuencia_dhz": ParametroEntero(80, 10, 300),
    },
    "destello": {
        "opacidad": ParametroEntero(60, 0, 100),
    },
    "camara_lenta": {
        # Factor de velocidad en milesimas: 500 es la mitad de velocidad. Entero
        # a proposito, para que dos revisiones con la misma rampa comparen igual.
        "factor_milesimas": ParametroEntero(500, 100, 1000),
    },
}


def normalizar_parametros(tipo: str, valores: Mapping[str, object],
                          contexto: str) -> Mapping[str, ValorParametro]:
    """Completa con predeterminados y rechaza todo lo que el catalogo no declara.

    Rechazar es el punto: un parametro desconocido no se ignora en silencio
    porque quien lo escribio cree que hizo algo, y el clip saldria sin ello sin
    que nada lo dijese.
    """
    declarados = CATALOGO_EFECTOS.get(tipo)
    if declarados is None:
        conocidos = ", ".join(sorted(CATALOGO_EFECTOS))
        raise ErrorCatalogoEdicion(
            f"{contexto} usa el efecto {tipo!r}, que esta version no conoce. Disponibles: {conocidos}.")
    if not isinstance(valores, Mapping):
        raise ErrorDocumentoEdicion(f"{contexto} tiene que declarar sus parametros como objeto.")
    desconocidos = sorted(set(valores) - set(declarados))
    if desconocidos:
        raise ErrorCatalogoEdicion(
            f"{contexto} declara parametros que el efecto {tipo!r} no tiene: {', '.join(desconocidos)}.")
    normalizados: dict[str, ValorParametro] = {}
    for nombre, parametro in declarados.items():
        if nombre in valores:
            normalizados[nombre] = parametro.validar(valores[nombre], f"{contexto}.{nombre}")
        else:
            normalizados[nombre] = parametro.predeterminado
    # Copia defensiva + vista inmutable: ``frozen=True`` no congela un dict
    # anidado y una revision podria cambiar despues de confirmada.
    return MappingProxyType(normalizados)


def exigir_opcion(valor: object, opciones: tuple[str, ...], contexto: str) -> str:
    if not isinstance(valor, str) or valor not in opciones:
        raise ErrorCatalogoEdicion(
            f"{contexto} vale {valor!r} y el catalogo admite: {', '.join(sorted(opciones))}.")
    return valor


__all__ = [
    "ANCLAJES", "ASPECTOS", "CAPAS_AUDIO", "CATALOGO_EFECTOS", "ESPACIOS_COLOR",
    "ESTILOS_CAPTION", "MODOS_ENCUADRE", "POLITICAS_FALLBACK", "Parametro", "ParametroBooleano",
    "ParametroEntero", "ParametroOpcion", "TIPOS_ASSET", "TRANSICIONES", "ValorParametro",
    "exigir_opcion", "normalizar_parametros",
]
