"""Estado reproducible de la carcasa, encima del `PlaybackPort`.

Este view model es la prueba de que el puerto sirve: se escribe una vez y no
cambia cuando el spike elija otro backend. Habla de fracciones —lo que una barra
sabe pintar y arrastrar— y traduce a segundos absolutos de la fuente, que es lo
unico que un reproductor entiende.

Un fallo de un elemento no vacia el panel: conserva la playlist y su seleccion,
porque la accion util despues de un medio ilegible es pasar al siguiente o
reintentar, y ambas necesitan que el contexto siga ahi.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from clipperkick.application.playback import PlaybackPort
from clipperkick.domain.playback import (ElementoPlaylist, EstadoReproduccion,
                                         FalloReproduccion, ProgresoReproduccion,
                                         RangoReproduccion)

from .observable import Emisor


@dataclass(frozen=True)
class EstadoReproductor:
    estado: EstadoReproduccion = EstadoReproduccion.INACTIVO
    indice: int = -1
    total: int = 0
    etiqueta: str = ""
    posicion: float = 0.0
    duracion: float = 0.0
    fraccion: float = 0.0
    mensaje: str = ""

    @property
    def reproduciendo(self) -> bool:
        return self.estado is EstadoReproduccion.REPRODUCIENDO

    @property
    def hay_medio(self) -> bool:
        return self.total > 0

    @property
    def puede_avanzar(self) -> bool:
        return 0 <= self.indice < self.total - 1

    @property
    def puede_retroceder(self) -> bool:
        return self.indice > 0


class ViewModelReproduccion:
    def __init__(self, reproductor: PlaybackPort) -> None:
        self._reproductor = reproductor
        self._elementos: tuple[ElementoPlaylist, ...] = ()
        self._cerrado = False
        self.estado: Emisor[EstadoReproductor] = Emisor(EstadoReproductor())
        reproductor.suscribir(self)

    # -- ordenes ------------------------------------------------------------ #

    def cargar(self, elementos: Sequence[ElementoPlaylist], indice: int = 0) -> None:
        self._elementos = tuple(elementos)
        self.estado.emitir(EstadoReproductor(total=len(self._elementos),
                                             indice=indice if self._elementos else -1,
                                             etiqueta=self._etiqueta(indice)))
        self._reproductor.cargar(self._elementos, indice)

    def alternar(self) -> None:
        """Un solo boton: es lo que la persona espera de la barra espaciadora."""
        if not self.estado.valor.hay_medio:
            return
        if self.estado.valor.reproduciendo:
            self._reproductor.pausar()
        else:
            self._reproductor.reproducir()

    def reproducir(self) -> None:
        if self.estado.valor.hay_medio:
            self._reproductor.reproducir()

    def pausar(self) -> None:
        self._reproductor.pausar()

    def buscar_fraccion(self, fraccion: float) -> None:
        """La barra manda 0..1; el reproductor recibe segundos de la fuente."""
        actual = self._actual()
        if actual is None:
            return
        fraccion = min(1.0, max(0.0, fraccion))
        rango = actual.rango
        if rango is not None:
            self._reproductor.buscar(rango.inicio + rango.duracion * fraccion)
        elif self.estado.valor.duracion > 0:
            self._reproductor.buscar(self.estado.valor.duracion * fraccion)

    def seleccionar(self, indice: int) -> None:
        if 0 <= indice < len(self._elementos):
            self._reproductor.seleccionar(indice)

    def siguiente(self) -> None:
        if self.estado.valor.puede_avanzar:
            self._reproductor.seleccionar(self.estado.valor.indice + 1)

    def anterior(self) -> None:
        if self.estado.valor.puede_retroceder:
            self._reproductor.seleccionar(self.estado.valor.indice - 1)

    def fijar_rango(self, rango: RangoReproduccion | None) -> None:
        """Ajustar inicio/fin de un candidato sin recargar el medio."""
        seleccionado = self.estado.valor.indice
        self._elementos = tuple(
            replace(elemento, rango=rango) if indice == seleccionado else elemento
            for indice, elemento in enumerate(self._elementos))
        self._reproductor.fijar_rango(rango)

    def vaciar(self) -> None:
        """Suelta el medio actual sin destruir el backend reutilizable.

        Cerrar un proyecto debe devolver sus handles, pero la siguiente carpeta
        abierta sigue necesitando el mismo reproductor y la misma superficie Qt.
        """
        if self._cerrado:
            return
        self._elementos = ()
        self._reproductor.cargar(())
        self.estado.emitir(EstadoReproductor())

    def cerrar(self) -> None:
        """Suelta el medio. En Windows es lo que permite archivar el proyecto."""
        if self._cerrado:
            return
        self._cerrado = True
        self._reproductor.liberar()
        self._elementos = ()
        self.estado.emitir(EstadoReproductor())

    # -- oyente del puerto -------------------------------------------------- #

    def progreso(self, progreso: ProgresoReproduccion) -> None:
        # Cualquier progreso que no sea un fallo borra el aviso anterior: si el
        # reproductor volvio a avanzar, el problema que se mostraba ya no describe
        # lo que esta pasando.
        vigente = self.estado.valor.mensaje if progreso.estado is EstadoReproduccion.FALLIDO else ""
        self.estado.emitir(replace(
            self.estado.valor, estado=progreso.estado, indice=progreso.indice,
            total=len(self._elementos), etiqueta=self._etiqueta(progreso.indice),
            posicion=progreso.posicion_relativa, duracion=progreso.duracion_efectiva,
            fraccion=progreso.fraccion, mensaje=vigente))

    def fallo(self, fallo: FalloReproduccion) -> None:
        self.estado.emitir(replace(self.estado.valor, estado=EstadoReproduccion.FALLIDO,
                                   mensaje=fallo.mensaje))

    # -- interno ------------------------------------------------------------ #

    def _actual(self) -> ElementoPlaylist | None:
        indice = self.estado.valor.indice
        return self._elementos[indice] if 0 <= indice < len(self._elementos) else None

    def _etiqueta(self, indice: int) -> str:
        if not 0 <= indice < len(self._elementos):
            return ""
        elemento = self._elementos[indice]
        return elemento.etiqueta or elemento.elemento_id
