"""Hechos estructurados del analisis. Misma regla que la ingesta: codigo y datos.

Un diagnostico no es un log. Es lo que la UI convierte en progreso y en acciones
—reintentar, instalar capacidad, aceptar menos clips— y por eso su contrato es
la clave, nunca el texto que la acompana.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from .errors import ErrorAnalisis


CODIGO_AUDIO_PREPARADO = "analisis.audio_preparado"
CODIGO_AUDIO_REUTILIZADO = "analisis.audio_reutilizado"
CODIGO_RASGOS_MEDIDOS = "analisis.rasgos_medidos"
CODIGO_RASGOS_REUTILIZADOS = "analisis.rasgos_reutilizados"
CODIGO_RANKING_COMPLETO = "analisis.ranking_completo"
CODIGO_RANKING_REUTILIZADO = "analisis.ranking_reutilizado"
CODIGO_MOMENTOS_MATERIALIZADOS = "analisis.momentos_materializados"
#: La fuente no tiene pista de audio: no es un fallo del proyecto, es la razon
#: por la que este modo no puede proponer candidatos.
CODIGO_SIN_AUDIO = "analisis.sin_audio"
#: Se midio, pero nada supero el umbral. Es el estado recuperable del ticket.
CODIGO_SIN_CANDIDATOS = "analisis.sin_candidatos_suficientes"
#: Se pidieron mas candidatos de los que la serie admite sin solaparlos.
CODIGO_CANDIDATOS_PARCIALES = "analisis.candidatos_parciales"
#: Una senal opcional no estaba disponible o fallo. El analisis continua.
CODIGO_ENRIQUECIMIENTO_OMITIDO = "analisis.enriquecimiento_omitido"


@dataclass(frozen=True)
class DiagnosticoAnalisis:
    """Hecho estructurado de un analisis. Nunca texto libre como contrato."""

    codigo: str
    datos: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.codigo, str) or not self.codigo:
            raise ErrorAnalisis("Un diagnostico de analisis necesita codigo.")
        object.__setattr__(self, "datos", dict(self.datos))
