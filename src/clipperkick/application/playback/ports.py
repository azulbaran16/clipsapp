"""El contrato que cualquier reproductor debe cumplir para servir a ClipsApp.

El spike de T08 compara backends; este puerto es lo que hace que esa comparacion
sea reversible. Todo lo que la UI necesita —cargar una playlist, reproducir,
pausar, buscar, acotar a un rango, seguir el progreso y enterarse de un fallo—
esta aqui, y nada de lo que un backend concreto necesita —handles, sinks,
formatos de tiempo, hilos propios— aparece.

Dos exigencias no son negociables y por eso estan en la firma:

- **`liberar()`.** En Windows un archivo abierto por el reproductor no se puede
  mover ni borrar. Un proyecto que se cierra mientras el reproductor conserva el
  handle deja una carpeta que no se puede archivar ni limpiar. Cerrar es parte
  del contrato, no una cortesia del adaptador.
- **El progreso se empuja, no se consulta.** Un `posicion()` invitaria a la UI a
  sondear desde el hilo de la interfaz; el puerto solo entrega instantaneas
  completas a un oyente, de modo que quien las recibe nunca puede quedarse a
  medio camino entre dos estados.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from clipperkick.domain.playback import (ElementoPlaylist, FalloReproduccion,
                                         ProgresoReproduccion, RangoReproduccion)


class OyenteReproduccion(Protocol):
    """Quien observa al reproductor. Ambos metodos deben ser baratos: el
    adaptador puede invocarlos desde su propio bucle de eventos."""

    def progreso(self, progreso: ProgresoReproduccion) -> None: ...
    def fallo(self, fallo: FalloReproduccion) -> None: ...


class PlaybackPort(Protocol):
    """Reproductor de una playlist de rangos.

    `cargar` reemplaza la playlist completa: no hay mutaciones parciales porque
    la unidad de trabajo de la UI —un candidato, un draft, una comparacion de
    variantes— siempre se describe entera.
    """

    def cargar(self, elementos: Sequence[ElementoPlaylist], indice: int = 0) -> None: ...
    def seleccionar(self, indice: int) -> None: ...
    def reproducir(self) -> None: ...
    def pausar(self) -> None: ...
    def buscar(self, segundos: float) -> None: ...
    def fijar_rango(self, rango: RangoReproduccion | None) -> None: ...
    def suscribir(self, oyente: OyenteReproduccion) -> None: ...
    def liberar(self) -> None: ...
