"""Ranking basico: de una serie de sonoridad a candidatos explicables.

Es una funcion pura y esa es su propiedad importante, no un detalle de estilo:
rerankear es exactamente volver a llamarla con otra configuracion sobre la misma
serie ya publicada. Si necesitase el archivo, el proyecto o un reloj, la promesa
del ticket —*cambiar cantidad, duracion o pesos rerunea ranking, no extraccion*—
seria imposible de cumplir por construccion.

## Como se decide

1. **Se fija la referencia una vez.** La base es la mediana de la serie —el
   nivel "normal" de este VOD— y la escala es la distancia de la base al
   percentil alto. Ambas viajan al resultado: sin ellas, "+8 dB" no significa
   nada y dos VODs no se pueden comparar.
2. **Se puntua cada posicion, no solo los picos.** Se calcula el score completo
   —con sus tres senales— en todos los indices elegibles, y la seleccion
   codiciosa opera sobre *ese* numero. Es lo que hace que lo que se selecciona y
   lo que se explica sean la misma cosa; puntuar despues de elegir permitiria
   que el mejor pico tuviese el peor score publicado.
3. **Se descartan los bordes antes de elegir.** Intro y despedida suelen ser lo
   mas sonoro de un directo y casi nunca son el momento que alguien recortaria.
4. **Se separa por la duracion del clip.** Con esa distancia minima entre picos,
   dos rangos construidos con el mismo adelanto no pueden solaparse: el "sin
   solapamientos" es una consecuencia aritmetica, no una limpieza posterior.

## Las tres senales

| Senal | Que mide | Por que no basta sola |
| --- | --- | --- |
| `pico` | cuanto sobresale el instante sobre la base | un golpe de un segundo ganaria a media hora de partida |
| `sostenido` | la media del clip completo sobre la base | un tramo permanentemente ruidoso ganaria siempre |
| `contraste` | cuanto sube el clip respecto a lo anterior | por si sola premiaria cualquier salida de un silencio |

Las tres son baratas, funcionan en CPU, no necesitan red ni modelo y se derivan
del mismo artefacto. Una senal opcional futura —transcripcion, sujeto— entra por
`senales_extra` sin tocar nada de esto.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .candidates import (
    EstadoAnalisis, Momento, ReferenciaScore, ResultadoRanking, identidad_momento,
)
from .config import SENAL_CONTRASTE, SENAL_PICO, SENAL_SOSTENIDO, ConfiguracionRanking
from .errors import ErrorSerieAnalisis
from .features import SerieSonoridad, mediana, percentil
from .peaks import elegir_indices, silenciar_bordes
from .scoring import ComponenteScore, razones_de, score_total


@dataclass(frozen=True)
class SenalExterna:
    """Aporte de una capacidad opcional, ya normalizado a [0, 1] por muestra.

    Se exige normalizada —y no en su unidad nativa— porque el nucleo no puede
    saber que escala tiene la confianza de un transcriptor o la cobertura de un
    tracker. Quien produce la senal es quien sabe convertirla; aqui solo se
    promedia sobre la ventana del clip y se pondera.
    """

    nombre: str
    version: str
    valores: tuple[float, ...]
    paso_segundos: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.nombre, str) or not self.nombre:
            raise ErrorSerieAnalisis("Una senal externa necesita nombre.")
        if not isinstance(self.version, str) or not self.version:
            raise ErrorSerieAnalisis(f"La senal {self.nombre!r} necesita declarar su version.")
        valores = tuple(float(valor) for valor in self.valores)
        if any(not 0.0 <= valor <= 1.0 for valor in valores):
            raise ErrorSerieAnalisis(
                f"La senal {self.nombre!r} entrega valores fuera de [0, 1]; normalizala en origen.")
        object.__setattr__(self, "valores", valores)
        if self.paso_segundos <= 0:
            raise ErrorSerieAnalisis(f"La senal {self.nombre!r} declara un paso que no avanza.")

    def media(self, desde_segundos: float, hasta_segundos: float) -> float | None:
        if not self.valores:
            return None
        inicio = max(0, int(desde_segundos / self.paso_segundos))
        fin = min(len(self.valores), max(inicio + 1, int(hasta_segundos / self.paso_segundos)))
        if fin <= inicio:
            return None
        return sum(self.valores[inicio:fin]) / (fin - inicio)


def _normalizar(bruto: float, escala: float) -> float:
    """Lleva una diferencia en dB a [0, 1] contra la escala del VOD."""
    if escala <= 0:
        return 0.0
    return max(0.0, min(1.0, bruto / escala))


def _rango(indice: int, serie: SerieSonoridad, config: ConfiguracionRanking,
           duracion_total: float) -> tuple[float, float]:
    """Rango del clip alrededor de una muestra, **recortado** contra el VOD.

    El rango natural de un pico es `[t - adelanto*d, t + (1-adelanto)*d]`, y dos
    picos separados por al menos `d` producen rangos naturales que como mucho se
    tocan. Contra los bordes del VOD ese rango se **recorta**, nunca se desplaza:
    desplazarlo conservaria la duracion pedida, pero empujaria el final de un
    clip de la cabecera hacia dentro del siguiente y el "sin solapamientos"
    dejaria de ser cierto justo donde nadie lo mira. Recortar solo puede
    encoger, de modo que la invariante se hereda del rango natural.

    El precio es un clip mas corto pegado al principio o al final del VOD. Con
    el margen de bordes por defecto no llega a ocurrir; con el margen a cero, un
    clip corto y honesto es mejor que dos que se pisan.
    """
    natural = serie.segundo_de(indice) - config.duracion_segundos * config.adelanto
    inicio = max(0.0, natural)
    fin = min(duracion_total, natural + config.duracion_segundos)
    return inicio, fin


def _componentes(indice: int, serie: SerieSonoridad, config: ConfiguracionRanking,
                 base: float, escala: float, duracion_total: float,
                 senales: Mapping[str, SenalExterna]) -> tuple[ComponenteScore, ...]:
    inicio, fin = _rango(indice, serie, config, duracion_total)
    desde, hasta = serie.indice_de(inicio), serie.indice_de(fin - 1e-9) + 1
    media_clip = serie.media(desde, hasta)
    componentes: list[ComponenteScore] = []

    peso = config.peso_de(SENAL_PICO)
    if peso > 0:
        bruto = serie.valores[indice] - base
        componentes.append(ComponenteScore(
            senal=SENAL_PICO, valor=bruto, referencia=base, escala=escala,
            normalizado=_normalizar(bruto, escala), peso=peso))

    peso = config.peso_de(SENAL_SOSTENIDO)
    if peso > 0 and media_clip is not None:
        bruto = media_clip - base
        componentes.append(ComponenteScore(
            senal=SENAL_SOSTENIDO, valor=bruto, referencia=base, escala=escala,
            normalizado=_normalizar(bruto, escala), peso=peso))

    peso = config.peso_de(SENAL_CONTRASTE)
    if peso > 0 and media_clip is not None:
        anterior_desde = desde - serie.muestras_de(config.ventana_contraste_segundos)
        media_previa = serie.media(anterior_desde, desde)
        if media_previa is not None:
            bruto = media_clip - media_previa
            componentes.append(ComponenteScore(
                senal=SENAL_CONTRASTE, valor=bruto, referencia=media_previa, escala=escala,
                normalizado=_normalizar(bruto, escala), peso=peso))

    for nombre in sorted(senales):
        peso = config.peso_de(nombre)
        if peso <= 0:
            continue
        aporte = senales[nombre].media(inicio, fin)
        if aporte is None:
            continue
        componentes.append(ComponenteScore(
            senal=nombre, valor=aporte, referencia=0.0, escala=1.0,
            normalizado=max(0.0, min(1.0, aporte)), peso=peso, unidad="fraccion"))
    return tuple(componentes)


def rankear(serie: SerieSonoridad, config: ConfiguracionRanking, fuente_id: str,
            senales_extra: Sequence[SenalExterna] = (),
            duracion_total: float | None = None) -> ResultadoRanking:
    """Candidatos separados, ordenados y explicados. Determinista y puro."""
    if not isinstance(fuente_id, str) or not fuente_id:
        raise ErrorSerieAnalisis("El ranking necesita saber de que fuente sale.")
    senales = {senal.nombre: senal for senal in senales_extra}
    # Una senal pesada que no llego se cuenta como ausente. Es la diferencia
    # entre "este clip no tenia voz" y "no supimos si la tenia", y el Flujo 3
    # exige que la degradacion se comunique en vez de aplicarse en silencio.
    ausentes = tuple(sorted(nombre for nombre, peso in config.pesos.items()
                            if peso > 0 and nombre not in senales
                            and nombre not in (SENAL_PICO, SENAL_SOSTENIDO, SENAL_CONTRASTE)))
    if serie.vacia:
        return ResultadoRanking(estado=EstadoAnalisis.SIN_CANDIDATOS,
                                senales_presentes=tuple(sorted(senales)),
                                senales_ausentes=ausentes)

    total = duracion_total if duracion_total is not None else serie.duracion_segundos
    total = max(total, serie.paso_segundos)
    base = mediana(serie.valores)
    escala = max(percentil(serie.valores, config.percentil_escala) - base,
                 config.escala_minima_db)
    referencia = ReferenciaScore(base_db=base, escala_db=escala, muestras=len(serie),
                                 duracion_segundos=total)

    puntajes = [score_total(_componentes(indice, serie, config, base, escala, total, senales))
                for indice in range(len(serie))]
    silenciar_bordes(puntajes, serie.muestras_de(config.margen_efectivo(total)))
    # El umbral se aplica dentro de la seleccion: un indice descartado por borde
    # o por cercania vale `MARCA_DESCARTADO`, que nunca alcanza un umbral de
    # [0, 1]. Asi "no quedan candidatos" y "no quedan candidatos buenos" salen
    # por el mismo camino y no hay dos definiciones de suficiente.
    elegidos = elegir_indices(puntajes, config.cantidad,
                              serie.muestras_cubriendo(config.separacion_efectiva()),
                              config.umbral_score)

    momentos: list[Momento] = []
    for posicion, (indice, score) in enumerate(elegidos, 1):
        inicio, fin = _rango(indice, serie, config, total)
        if fin <= inicio:
            continue
        componentes = _componentes(indice, serie, config, base, escala, total, senales)
        momentos.append(Momento(
            id=identidad_momento(fuente_id, inicio, fin), fuente_id=fuente_id,
            inicio_segundos=inicio, fin_segundos=fin, score=score, posicion=posicion,
            componentes=componentes,
            razones=razones_de(componentes, score, ausentes)))

    if not momentos:
        estado = EstadoAnalisis.SIN_CANDIDATOS
    elif len(momentos) < config.cantidad:
        estado = EstadoAnalisis.PARCIAL
    else:
        estado = EstadoAnalisis.COMPLETO
    return ResultadoRanking(momentos=tuple(momentos), estado=estado, referencia=referencia,
                            senales_presentes=tuple(sorted(senales)), senales_ausentes=ausentes)
