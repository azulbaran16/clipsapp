"""Errores tipados del analisis basico.

Se repite aqui la frontera que T04 fijo para la ingesta, porque el ticket la
vuelve a exigir con otras palabras: *un audio plano produce un estado "sin
candidatos suficientes" recuperable, no un fallo del proyecto*.

- **Error** es lo que impide construir el modelo: una configuracion que no
  admite un resultado determinista, una serie de sonoridad que no se puede
  leer, un artefacto que no supera su checksum.
- **Estado** es lo que el modelo *si* puede describir: que no haya bastantes
  candidatos, que una senal opcional no estuviese disponible, que la confianza
  sea baja. Eso viaja como `EstadoAnalisis`, `Razon` y `DiagnosticoAnalisis`.

La diferencia importa porque decide quien tiene que actuar. Un error interrumpe
el analisis y pide intervencion; un estado se muestra, se explica y permite
seguir —volver a analizar con otra duracion, aceptar menos clips, instalar una
capacidad—.
"""


class ErrorAnalisis(Exception):
    """El analisis basico no pudo producir un resultado utilizable."""


class ErrorConfiguracionAnalisis(ErrorAnalisis):
    """La configuracion no admite un resultado determinista y explicable."""


class ErrorSerieAnalisis(ErrorAnalisis):
    """La serie de sonoridad no se puede representar o no se puede leer."""


class ErrorArtefactoAnalisis(ErrorAnalisis):
    """Un artefacto de analisis no supero checksum, linaje o formato."""


class ErrorMomentoAnalisis(ErrorAnalisis):
    """Un candidato persistido no describe un rango legible."""


class ErrorAudioAusente(ErrorAnalisis):
    """La fuente no contiene una pista de audio utilizable."""


class ErrorCancelacionAnalisis(ErrorAnalisis):
    """La persona cancelo antes de publicar la etapa en curso."""
