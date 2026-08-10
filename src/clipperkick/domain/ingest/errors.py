"""Errores tipados de la ingesta.

Misma regla que T01-T03: ninguna capa superior distingue un fallo leyendo
texto. Aqui la regla tiene una consecuencia concreta que el ticket exige por
escrito —*las fuentes con VFR, sin audio o con multiples flujos producen estados
explicitos, no excepciones genericas*—, de modo que la frontera entre error y
estado es deliberada:

- **Error** es lo que impide construir el modelo: no hay archivo, no hay flujo
  de video, ffprobe no contesta o contesta algo que no se puede leer.
- **Estado** es lo que el modelo *si* puede describir: VFR, rotacion, ausencia
  de audio, varios flujos. Eso viaja como `AvisoMedia` dentro de la metadata y
  no interrumpe nada.

Convertir un aviso en excepcion obligaria a cada llamador a atrapar para saber
algo que la metadata ya dice, y a inventar un valor para seguir.
"""


class ErrorIngesta(Exception):
    """Una fuente no pudo convertirse en un `SourceAsset` utilizable."""


class ErrorFuenteIngesta(ErrorIngesta):
    """La entrada no designa una fuente que se pueda incorporar."""


class ErrorDescargaIngesta(ErrorIngesta):
    """La obtencion remota no dejo un archivo final utilizable."""


class ErrorHuellaIngesta(ErrorIngesta):
    """La huella de la fuente no pudo calcularse o no es representable."""


class ErrorHuellaInestable(ErrorHuellaIngesta):
    """El archivo cambio de identidad o de contenido mientras se leia.

    Es su propia clase porque decide algo distinto que el resto: una huella
    inestable no significa que la fuente sea invalida, sino que *todavia no se
    sabe*. Quien la recibe puede reintentar; quien recibe `ErrorHuellaIngesta`
    a secas no tiene nada que reintentar.
    """


class ErrorEvidenciaHuella(ErrorHuellaInestable):
    """No hay forma de demostrar que la huella describa un solo contenido.

    Se distingue de `ErrorHuellaInestable` en lo unico que importa aqui: **no
    se reintenta**. Una huella inestable puede estabilizarse en la siguiente
    pasada porque la mutacion era puntual; una evidencia insuficiente lo seguira
    siendo por muchas veces que se lea, porque lo que falta es la senal con la
    que se decidiria, no la ocasion de mirarla.
    """


class ErrorCancelacionIngesta(ErrorIngesta):
    """La persona detuvo la ingesta antes de que produjese nada publicable."""


class ErrorMediaDanada(ErrorIngesta):
    """El archivo existe pero no describe un medio que se pueda analizar."""


class ErrorMetadataMedia(ErrorIngesta):
    """El sondeo contesto, pero su documento no es normalizable."""


class ErrorClaveMaterializacion(ErrorIngesta):
    """La configuracion o las entradas no admiten una clave determinista."""


class ErrorArtefactoIngesta(ErrorIngesta):
    """Un artefacto no supero checksum, validacion o linaje."""
