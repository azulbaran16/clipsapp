"""Seleccion codiciosa de indices separados sobre una serie de puntajes.

Es el nucleo que `elegir_picos` venia implementando en linea y que el ticket
pide *adaptar* para consumir artefactos versionados. Se extrae aqui por una
razon concreta y no por gusto de generalizar: el flujo legado y el ranking nuevo
tienen que separar candidatos **con la misma regla**. Si cada uno tuviera la
suya, dos versiones de ClipperKick darian dos conjuntos de momentos sobre el
mismo VOD y ninguna prueba lo notaria, porque cada una probaria su propia copia.

Tres detalles del algoritmo son contrato y no casualidad:

1. **El descarte es un valor centinela, no un borrado.** Marcar
   `MARCA_DESCARTADO` conserva los indices, y por tanto la aritmetica de
   separacion sigue midiendo distancias reales sobre la serie original.
2. **El empate lo gana el indice menor.** `max` sobre `range` devuelve la
   primera posicion maxima, de modo que dos picos identicos siempre se ordenan
   igual. Sin esa regla el resultado dependeria del orden de iteracion y el
   ticket exige un orden determinista.
3. **La ventana de separacion es asimetrica**: cubre `[mejor - separacion,
   mejor + separacion)`. Se conserva tal cual porque es la que produce los
   resultados que el flujo actual ya entrega; cambiarla moveria los momentos de
   todos los proyectos existentes sin que nadie lo hubiera pedido.
"""

from __future__ import annotations

from collections.abc import Sequence

from .errors import ErrorConfiguracionAnalisis


#: Valor con el que se marca un indice inelegible. Es deliberadamente muy bajo
#: —no `-inf`— para que la serie marcada siga siendo un `list[float]` normal y
#: se pueda inspeccionar en una prueba sin tratar casos especiales.
MARCA_DESCARTADO = -999.0


def silenciar_bordes(puntajes: list[float], margen: int,
                     marca: float = MARCA_DESCARTADO) -> list[float]:
    """Descarta los `margen` primeros y ultimos indices, en sitio.

    Es el sesgo de bordes del VOD: la intro y la despedida de un directo suelen
    ser lo mas sonoro del archivo y casi nunca son el momento que alguien
    querria recortar.
    """
    if margen <= 0:
        return puntajes
    cantidad = len(puntajes)
    for indice in range(min(margen, cantidad)):
        puntajes[indice] = marca
        puntajes[cantidad - 1 - indice] = marca
    return puntajes


def elegir_indices(puntajes: Sequence[float], cantidad: int, separacion: int,
                   umbral: float, marca: float = MARCA_DESCARTADO,
                   ) -> list[tuple[int, float]]:
    """Elige hasta `cantidad` indices separados, del mejor al peor.

    `separacion` se cuenta en indices de la serie: quien la reciba en segundos
    la convierte antes, porque solo el llamador sabe cuanto dura una muestra.
    """
    if separacion < 0:
        raise ErrorConfiguracionAnalisis("La separacion entre candidatos no puede ser negativa.")
    if cantidad <= 0 or not puntajes:
        return []
    restantes = list(puntajes)
    total = len(restantes)
    elegidos: list[tuple[int, float]] = []
    for _ in range(cantidad):
        mejor = max(range(total), key=restantes.__getitem__)
        if restantes[mejor] < umbral:
            break
        elegidos.append((mejor, restantes[mejor]))
        # El propio indice se marca aparte de su ventana. Con `separacion >= 1`
        # la ventana ya lo cubre y no cambia nada; con `separacion == 0` es lo
        # unico que impide que la misma posicion se elija `cantidad` veces.
        restantes[mejor] = marca
        for indice in range(max(0, mejor - separacion), min(total, mejor + separacion)):
            restantes[indice] = marca
    elegidos.sort(key=lambda elegido: -elegido[1])
    return elegidos
