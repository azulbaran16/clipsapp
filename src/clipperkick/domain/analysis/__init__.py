"""Modelo puro del analisis basico: serie, score explicable y candidatos."""

from .candidates import (
    ESTADOS_MOMENTO, ESTADO_CANDIDATO, ESTADO_DESCARTADO, ESTADO_FAVORITO, PREFIJO_MOMENTO,
    VERSION_CANDIDATO, EstadoAnalisis, Momento, ReferenciaScore, ResultadoRanking,
    identidad_momento, milisegundos, resultado_desde_documento, sin_solapamientos,
)
from .config import (
    FRACCION_BORDES, MARGEN_BORDES_MAXIMO, PESOS_BASICOS, SENALES_BASICAS, SENAL_CONTRASTE,
    SENAL_PICO, SENAL_SOSTENIDO, VERSION_CONFIG, ConfiguracionExtraccion, ConfiguracionRanking,
    ConfiguracionRasgos,
)
from .diagnostics import (
    CODIGO_AUDIO_PREPARADO, CODIGO_AUDIO_REUTILIZADO, CODIGO_CANDIDATOS_PARCIALES,
    CODIGO_ENRIQUECIMIENTO_OMITIDO, CODIGO_MOMENTOS_MATERIALIZADOS, CODIGO_RANKING_COMPLETO,
    CODIGO_RANKING_REUTILIZADO, CODIGO_RASGOS_MEDIDOS, CODIGO_RASGOS_REUTILIZADOS,
    CODIGO_SIN_AUDIO, CODIGO_SIN_CANDIDATOS, DiagnosticoAnalisis,
)
from .errors import (
    ErrorAnalisis, ErrorArtefactoAnalisis, ErrorAudioAusente, ErrorCancelacionAnalisis,
    ErrorConfiguracionAnalisis, ErrorMomentoAnalisis, ErrorSerieAnalisis,
)
from .features import (
    VERSION_MEDICION, VERSION_SERIE, SerieSonoridad, mediana, medicion_canonica,
    normalizar_medicion, percentil, serie_desde_documento, suavizar,
)
from .peaks import MARCA_DESCARTADO, elegir_indices, silenciar_bordes
from .ranking import SenalExterna, rankear
from .scoring import (
    RAZONES_POR_SENAL, RAZON_BAJA_CONFIANZA, RAZON_CONTRASTE_PREVIO, RAZON_ENERGIA_SOSTENIDA,
    RAZON_PICO_SONORO, RAZON_SENAL_AUSENTE, RAZON_SENAL_OPCIONAL, UMBRAL_CONFIANZA, UMBRAL_RAZON,
    ComponenteScore, Razon, razones_de, score_total,
)

__all__ = [
    "CODIGO_AUDIO_PREPARADO", "CODIGO_AUDIO_REUTILIZADO", "CODIGO_CANDIDATOS_PARCIALES",
    "CODIGO_ENRIQUECIMIENTO_OMITIDO", "CODIGO_MOMENTOS_MATERIALIZADOS", "CODIGO_RANKING_COMPLETO",
    "CODIGO_RANKING_REUTILIZADO", "CODIGO_RASGOS_MEDIDOS", "CODIGO_RASGOS_REUTILIZADOS",
    "CODIGO_SIN_AUDIO", "CODIGO_SIN_CANDIDATOS", "ComponenteScore", "ConfiguracionExtraccion",
    "ConfiguracionRanking", "ConfiguracionRasgos", "DiagnosticoAnalisis", "ESTADOS_MOMENTO",
    "ESTADO_CANDIDATO", "ESTADO_DESCARTADO", "ESTADO_FAVORITO", "ErrorAnalisis",
    "ErrorArtefactoAnalisis", "ErrorAudioAusente", "ErrorCancelacionAnalisis",
    "ErrorConfiguracionAnalisis", "ErrorMomentoAnalisis",
    "ErrorSerieAnalisis", "EstadoAnalisis", "FRACCION_BORDES", "MARCA_DESCARTADO",
    "MARGEN_BORDES_MAXIMO", "Momento", "PESOS_BASICOS", "PREFIJO_MOMENTO",
    "RAZONES_POR_SENAL", "RAZON_BAJA_CONFIANZA", "RAZON_CONTRASTE_PREVIO",
    "RAZON_ENERGIA_SOSTENIDA", "RAZON_PICO_SONORO", "RAZON_SENAL_AUSENTE", "RAZON_SENAL_OPCIONAL",
    "Razon", "ReferenciaScore", "ResultadoRanking", "SENALES_BASICAS", "SENAL_CONTRASTE",
    "SENAL_PICO", "SENAL_SOSTENIDO", "SenalExterna", "SerieSonoridad", "UMBRAL_CONFIANZA",
    "UMBRAL_RAZON", "VERSION_CANDIDATO", "VERSION_CONFIG", "VERSION_MEDICION",
    "VERSION_SERIE", "elegir_indices", "identidad_momento", "mediana", "medicion_canonica",
    "milisegundos", "normalizar_medicion", "percentil", "rankear", "razones_de",
    "resultado_desde_documento", "score_total", "serie_desde_documento", "silenciar_bordes",
    "sin_solapamientos", "suavizar",
]
