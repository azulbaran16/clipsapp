"""Seleccion pura de momentos destacados (interfaz de compatibilidad).

Sigue siendo la firma que el flujo legado usa y que `engine.py` reexporta, pero
ya no implementa la seleccion: la delega en `domain.analysis.peaks`, que es la
misma regla que consume el ranking sobre artefactos versionados.

La unificacion es el punto. Mientras cada camino tenia su bucle, el flujo
directo y el analisis por etapas podian separar candidatos de forma distinta
sobre el mismo VOD y ninguna prueba lo habria notado, porque cada una probaba su
propia copia. Ahora una diferencia de comportamiento entre ambos es imposible de
escribir sin cambiar el archivo que los dos comparten.

Lo que aqui se conserva —y por eso vive aqui y no en el nucleo compartido— son
las convenciones concretas del flujo legado: la mediana como linea base, el
margen de bordes en muestras, la separacion de `duracion + 30` y el umbral de
1.5 dB. Son las que producen los momentos que la version actual ya entrega, y
moverlas cambiaria el resultado de todos los proyectos existentes.
"""

from __future__ import annotations

from collections.abc import Sequence

from .analysis.peaks import elegir_indices, silenciar_bordes


#: Convenciones del flujo legado. Explicitas y con nombre para que se vea que
#: son decisiones de *este* camino y no constantes del algoritmo.
UMBRAL_LEGADO_DB = 1.5
MARGEN_LEGADO_MAXIMO = 30
FRACCION_LEGADA_BORDES = 10
SEPARACION_LEGADA_EXTRA = 30


def elegir_picos(
    niveles: Sequence[float], numero_clips: int, duracion: int
) -> list[tuple[int, float]]:
    """Elige picos separados y devuelve los mejores primero."""
    if not niveles or numero_clips <= 0:
        return []

    cantidad = len(niveles)
    base = sorted(niveles)[cantidad // 2]
    puntajes = silenciar_bordes([nivel - base for nivel in niveles],
                                min(MARGEN_LEGADO_MAXIMO, cantidad // FRACCION_LEGADA_BORDES))
    return elegir_indices(puntajes, numero_clips, duracion + SEPARACION_LEGADA_EXTRA,
                          UMBRAL_LEGADO_DB)
