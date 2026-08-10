"""Emision de estado sin depender de Qt.

Un view model que emitiese `Signal` de Qt solo podria probarse con Qt instalado,
en un hilo con bucle de eventos y con una `QApplication` viva. La logica de la
carcasa —que pantalla toca, que error mostrar, si hay algo en curso— no necesita
nada de eso, y hacerla depender de ello convertiria cada prueba de una regla de
navegacion en una prueba de integracion grafica.

El emisor guarda el ultimo valor porque una vista siempre llega tarde: se
construye despues del view model y necesita pintarse antes del proximo cambio.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Generic, TypeVar


T = TypeVar("T")


class Emisor(Generic[T]):
    def __init__(self, inicial: T) -> None:
        self._valor = inicial
        self._oyentes: list[Callable[[T], None]] = []

    @property
    def valor(self) -> T:
        return self._valor

    def suscribir(self, oyente: Callable[[T], None]) -> Callable[[], None]:
        """Suscribe, entrega el estado actual y devuelve como darse de baja."""
        self._oyentes.append(oyente)
        oyente(self._valor)
        return lambda: self._oyentes.remove(oyente) if oyente in self._oyentes else None

    def emitir(self, valor: T) -> None:
        """Emite solo cambios reales: los estados son valores comparables y
        reenviar uno identico haria repintar sin motivo y dispararia bucles
        cuando una vista reacciona escribiendo de vuelta."""
        if valor == self._valor:
            return
        self._valor = valor
        for oyente in tuple(self._oyentes):
            oyente(valor)
