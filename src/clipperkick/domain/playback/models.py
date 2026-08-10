"""Vocabulario de reproduccion, independiente del reproductor que lo cumpla.

Estos valores existen para que la decision del spike —QtMultimedia, libmpv o
VLC— no se filtre a la UI. Un view model habla de rangos, elementos y progreso;
nunca de `QMediaPlayer`, de `mpv_handle` ni de milisegundos de un backend
concreto. El tiempo se expresa siempre en segundos de punto flotante porque es
la unidad del `EditDocument` y de FFmpeg; cada adaptador convierte a la suya.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .errors import ErrorReproduccion


class EstadoReproduccion(str, Enum):
    """Estados observables. `FALLIDO` no es terminal para la sesion completa:
    solo para el elemento que fallo, de modo que una playlist puede continuar."""

    INACTIVO = "inactivo"
    CARGANDO = "cargando"
    REPRODUCIENDO = "reproduciendo"
    PAUSADO = "pausado"
    FINALIZADO = "finalizado"
    FALLIDO = "fallido"


@dataclass(frozen=True)
class RangoReproduccion:
    """Sub-rango reproducible de un medio, en segundos absolutos de la fuente.

    Se valida en la construccion y no en el adaptador: un rango invertido o
    negativo es un error del llamador, y descubrirlo dentro del reproductor lo
    convertiria en un fallo de backend distinto por cada backend.
    """

    inicio: float
    fin: float

    def __post_init__(self) -> None:
        if self.inicio < 0:
            raise ErrorReproduccion("Un rango de reproduccion no puede empezar antes del origen.")
        if self.fin <= self.inicio:
            raise ErrorReproduccion("Un rango de reproduccion debe terminar despues de su inicio.")

    @property
    def duracion(self) -> float:
        return self.fin - self.inicio

    def contiene(self, segundos: float) -> bool:
        return self.inicio <= segundos < self.fin

    def acotar(self, segundos: float) -> float:
        return min(max(segundos, self.inicio), self.fin)


@dataclass(frozen=True)
class ElementoPlaylist:
    """Un medio y, opcionalmente, el tramo que interesa de el.

    Un candidato es un rango de la fuente proxy y un Draft es una secuencia de
    segmentos: ambos son la misma estructura, y por eso la playlist es parte del
    contrato y no una funcion que la UI tenga que reimplementar.
    """

    elemento_id: str
    ruta: str
    rango: RangoReproduccion | None = None
    etiqueta: str = ""

    def __post_init__(self) -> None:
        if not self.elemento_id:
            raise ErrorReproduccion("Un elemento de la playlist necesita identidad propia.")
        if not self.ruta:
            raise ErrorReproduccion("Un elemento de la playlist necesita una ruta de medio.")

    @property
    def inicio(self) -> float:
        return self.rango.inicio if self.rango else 0.0


@dataclass(frozen=True)
class ProgresoReproduccion:
    """Instantanea completa del reproductor: la UI no acumula estado propio.

    Cada evento describe la situacion entera —que elemento, en que estado, en
    que posicion— para que un mensaje perdido o entregado fuera de orden no deje
    a la interfaz inventando una transicion que nunca ocurrio.
    """

    indice: int
    elemento_id: str
    estado: EstadoReproduccion
    posicion: float = 0.0
    duracion: float = 0.0
    rango: RangoReproduccion | None = None

    @property
    def posicion_relativa(self) -> float:
        """Posicion dentro del rango: lo que una barra de progreso debe pintar."""
        return max(0.0, self.posicion - (self.rango.inicio if self.rango else 0.0))

    @property
    def duracion_efectiva(self) -> float:
        """Lo que dura *este* tramo, no el archivo que lo contiene."""
        return self.rango.duracion if self.rango is not None else self.duracion

    @property
    def fraccion(self) -> float:
        efectiva = self.duracion_efectiva
        return min(1.0, self.posicion_relativa / efectiva) if efectiva > 0 else 0.0


@dataclass(frozen=True)
class FalloReproduccion:
    """Un fallo es un evento, no una excepcion.

    Un medio ilegible en mitad de una playlist no puede propagarse como
    excepcion: ocurre dentro del backend, de forma asincrona, y la respuesta
    correcta es informar y seguir con el resto, no romper la pila del que pulso
    `reproducir` hace treinta segundos.
    """

    elemento_id: str
    codigo: str
    mensaje: str
