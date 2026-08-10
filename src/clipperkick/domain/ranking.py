"""Seleccion pura de momentos destacados."""

from __future__ import annotations

from collections.abc import Sequence


def elegir_picos(
    niveles: Sequence[float], numero_clips: int, duracion: int
) -> list[tuple[int, float]]:
    """Elige picos separados y devuelve los mejores primero."""
    if not niveles or numero_clips <= 0:
        return []

    cantidad = len(niveles)
    base = sorted(niveles)[cantidad // 2]
    puntajes = [nivel - base for nivel in niveles]
    margen = min(30, cantidad // 10)
    for indice in range(margen):
        puntajes[indice] = -999
        puntajes[cantidad - 1 - indice] = -999

    separacion = duracion + 30
    picos: list[tuple[int, float]] = []
    for _ in range(numero_clips):
        mejor = max(range(cantidad), key=puntajes.__getitem__)
        if puntajes[mejor] < 1.5:
            break
        picos.append((mejor, puntajes[mejor]))
        for indice in range(
            max(0, mejor - separacion), min(cantidad, mejor + separacion)
        ):
            puntajes[indice] = -999

    picos.sort(key=lambda pico: -pico[1])
    return picos
