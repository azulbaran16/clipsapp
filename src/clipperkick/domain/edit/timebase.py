"""Tiempo de la edicion: entero, exacto y con una sola unidad.

El documento no guarda segundos en coma flotante en ningun sitio. Todo instante
es un **tick** entero contado desde el inicio del clip, y la `Timebase` dice
cuantos ticks hay en un segundo. La razon no es de estilo:

- un `float` no sobrevive a un round-trip JSON sin que su ultimo bit dependa del
  intercalador que lo escribio, y el ticket exige round-trip canonico *sin
  perder precision temporal*;
- dos cues contiguos escritos en segundos pueden solaparse o dejar un hueco
  segun como se redondeen al compilar, y el solape es justo la invariante que el
  documento tiene que hacer irrepresentable;
- una comparacion de igualdad entre revisiones —la que decide si un patch
  cambio algo y si una cache sigue siendo valida— tiene que ser exacta.

La cuantizacion desde segundos existe **solo en la frontera**: al migrar un
documento antiguo o al recibir un analisis que hablaba en segundos. Una vez
dentro, nadie vuelve a coma flotante.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import gcd, isfinite

from .errors import ErrorDocumentoEdicion


#: 48000 ticks por segundo. Es divisible por 24, 25, 30, 48, 50, 60, 100 y 1000,
#: de modo que un fotograma de cualquier fps habitual y un milisegundo caen
#: exactamente sobre un tick y no arrastran error al compilar.
TIMEBASE_PREDETERMINADO = 48000

#: Cota superior de la timebase. No es una limitacion tecnica sino una defensa:
#: una timebase absurda convierte cualquier duracion razonable en un entero que
#: nadie puede leer y hace que un documento migrado desborde al cuantizar.
TIMEBASE_MAXIMO = 1_000_000_000

#: Cota superior de la duracion de un clip, en segundos. Un borrador es un clip
#: corto; un documento que declara mas horas que un dia esta describiendo otra
#: cosa y su aritmetica de ticks deja de ser barata.
DURACION_MAXIMA_SEGUNDOS = 86_400


def exigir_entero(valor: object, campo: str) -> int:
    """Entero de verdad. `bool` es `int` en Python y aqui nunca es un tiempo."""
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un entero.")
    return valor


def exigir_tick(valor: object, campo: str) -> int:
    tick = exigir_entero(valor, campo)
    if tick < 0:
        raise ErrorDocumentoEdicion(f"El campo '{campo}' no puede ser un tick negativo.")
    return tick


def exigir_identificador(valor: object, campo: str) -> str:
    """Identidad no vacia y sin espacios de borde.

    Se exige aqui y no al serializar porque un id con espacios invisibles
    produce dos elementos que la UI muestra iguales y el documento trata como
    distintos: el solape se detecta, la duplicidad no.
    """
    if not isinstance(valor, str):
        raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser texto.")
    if not valor or valor != valor.strip():
        raise ErrorDocumentoEdicion(
            f"El campo '{campo}' tiene que ser un identificador no vacio y sin espacios de borde.")
    return valor


@dataclass(frozen=True)
class Fraccion:
    """Racional exacto y ya reducido. La igualdad estructural es la matematica.

    Se reduce en el constructor a proposito: sin eso `30/1` y `60/2` serian dos
    documentos distintos que describen el mismo fps, y la comparacion de
    revisiones —de la que cuelga la invalidacion de cache— empezaria a mentir.
    """

    numerador: int
    denominador: int = 1

    def __post_init__(self) -> None:
        numerador = exigir_entero(self.numerador, "numerador")
        denominador = exigir_entero(self.denominador, "denominador")
        if numerador <= 0 or denominador <= 0:
            raise ErrorDocumentoEdicion("Una fraccion del documento tiene que ser positiva.")
        divisor = gcd(numerador, denominador)
        object.__setattr__(self, "numerador", numerador // divisor)
        object.__setattr__(self, "denominador", denominador // divisor)

    def como_documento(self) -> dict[str, object]:
        return {"numerador": self.numerador, "denominador": self.denominador}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "Fraccion":
        if not isinstance(datos, dict):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        return cls(numerador=exigir_entero(datos.get("numerador"), f"{campo}.numerador"),
                   denominador=exigir_entero(datos.get("denominador"), f"{campo}.denominador"))

    def __str__(self) -> str:
        return f"{self.numerador}/{self.denominador}"


@dataclass(frozen=True)
class Timebase:
    """Ticks por segundo. Es la unica unidad de tiempo del documento."""

    ticks_por_segundo: int = TIMEBASE_PREDETERMINADO

    def __post_init__(self) -> None:
        ticks = exigir_entero(self.ticks_por_segundo, "timebase")
        if ticks <= 0 or ticks > TIMEBASE_MAXIMO:
            raise ErrorDocumentoEdicion(
                f"La timebase tiene que estar entre 1 y {TIMEBASE_MAXIMO} ticks por segundo.")

    def cuantizar(self, segundos: object, campo: str) -> int:
        """Segundos -> ticks. Solo se usa en la frontera (migracion, analisis).

        Redondea al tick mas cercano con desempate al par, que es la regla de
        `round` de Python: es la unica que no sesga sistematicamente hacia
        adelante una lista de cues consecutivos.
        """
        if isinstance(segundos, bool) or not isinstance(segundos, (int, float)):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un numero de segundos.")
        if not isfinite(float(segundos)):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' no es un numero de segundos finito.")
        if segundos < 0:
            raise ErrorDocumentoEdicion(f"El campo '{campo}' no puede ser negativo.")
        if segundos > DURACION_MAXIMA_SEGUNDOS:
            raise ErrorDocumentoEdicion(
                f"El campo '{campo}' declara mas de {DURACION_MAXIMA_SEGUNDOS} segundos.")
        return int(round(segundos * self.ticks_por_segundo))

    def limite(self) -> int:
        """Mayor duracion representable, en ticks."""
        return DURACION_MAXIMA_SEGUNDOS * self.ticks_por_segundo

    def como_documento(self) -> int:
        return self.ticks_por_segundo

    @classmethod
    def desde_documento(cls, datos: object) -> "Timebase":
        return cls(ticks_por_segundo=exigir_entero(datos, "timebase"))


@dataclass(frozen=True)
class Rango:
    """Intervalo semiabierto `[inicio, fin)` en ticks.

    Semiabierto porque es lo que hace que "contiguo" y "solapado" sean
    preguntas distintas: dos cues que se tocan en el mismo tick son contiguos y
    validos; si el intervalo fuese cerrado compartirian un tick y el documento
    tendria que elegir cual gana en cada compilacion.
    """

    inicio: int
    fin: int

    def __post_init__(self) -> None:
        inicio = exigir_tick(self.inicio, "inicio")
        fin = exigir_tick(self.fin, "fin")
        if fin <= inicio:
            raise ErrorDocumentoEdicion(
                f"Un rango del documento termina antes de empezar ({inicio} -> {fin}).")

    @property
    def duracion(self) -> int:
        return self.fin - self.inicio

    def contiene(self, tick: int) -> bool:
        return self.inicio <= tick < self.fin

    def dentro_de(self, limite: int) -> bool:
        return self.fin <= limite

    def solapa(self, otro: "Rango") -> bool:
        return self.inicio < otro.fin and otro.inicio < self.fin

    def unir(self, otro: "Rango") -> "Rango":
        return Rango(min(self.inicio, otro.inicio), max(self.fin, otro.fin))

    def expandido(self, margen: int, limite: int) -> "Rango":
        """Rango ampliado y recortado al documento; nunca se sale de `[0, limite)`."""
        return Rango(max(0, self.inicio - margen), min(limite, max(self.fin + margen, self.inicio + 1)))

    def como_documento(self) -> dict[str, object]:
        return {"inicio": self.inicio, "fin": self.fin}

    @classmethod
    def desde_documento(cls, datos: object, campo: str) -> "Rango":
        if not isinstance(datos, dict):
            raise ErrorDocumentoEdicion(f"El campo '{campo}' tiene que ser un objeto.")
        return cls(inicio=exigir_tick(datos.get("inicio"), f"{campo}.inicio"),
                   fin=exigir_tick(datos.get("fin"), f"{campo}.fin"))


def exigir_dentro(rango: Rango, duracion: int, contexto: str) -> None:
    if not rango.dentro_de(duracion):
        raise ErrorDocumentoEdicion(
            f"{contexto} ocupa {rango.inicio}-{rango.fin} y el documento dura {duracion} ticks.")


def exigir_tick_dentro(tick: int, duracion: int, contexto: str) -> None:
    """Un instante puntual puede caer justo en el final: `[0, duracion]`.

    Es distinto de un rango, que es semiabierto: un keyframe de encuadre en el
    ultimo tick describe el fotograma final, mientras que un cue que terminase
    en `duracion + 1` describiria material que no existe.
    """
    if not 0 <= tick <= duracion:
        raise ErrorDocumentoEdicion(
            f"{contexto} cae en el tick {tick} y el documento dura {duracion} ticks.")


def exigir_identidades_unicas(identidades: tuple[str, ...], contexto: str) -> None:
    vistas: set[str] = set()
    for identidad in identidades:
        if identidad in vistas:
            raise ErrorDocumentoEdicion(f"{contexto} repite el identificador {identidad!r}.")
        vistas.add(identidad)


def exigir_sin_solapes(rangos: tuple[Rango, ...], contexto: str) -> None:
    """Los rangos llegan ya ordenados, de modo que basta comparar con el previo."""
    for previo, siguiente in zip(rangos, rangos[1:]):
        if previo.solapa(siguiente):
            raise ErrorDocumentoEdicion(
                f"{contexto} solapa {previo.inicio}-{previo.fin} con"
                f" {siguiente.inicio}-{siguiente.fin}.")


__all__ = [
    "DURACION_MAXIMA_SEGUNDOS", "Fraccion", "Rango", "TIMEBASE_MAXIMO",
    "TIMEBASE_PREDETERMINADO", "Timebase", "exigir_dentro", "exigir_entero",
    "exigir_identidades_unicas", "exigir_identificador", "exigir_sin_solapes", "exigir_tick",
    "exigir_tick_dentro",
]
