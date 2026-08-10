"""Errores tipados de la edicion.

Misma regla que T01-T04: ninguna capa superior distingue un fallo leyendo
texto. Aqui la frontera entre *error* y *estado* es la que decide si el
documento existe:

- **Error** es lo que impide construir un `EditDocument`: un rango fuera de la
  duracion, un efecto que el catalogo no conoce, un asset que nadie registro,
  una revision que no pertenece a su draft.
- **Estado** es lo que el documento *si* puede describir: un cue oculto, una
  correccion manual, una variante sin rehacer disponible. Eso viaja como dato y
  no interrumpe nada.

Un documento invalido nunca se degrada a "documento parcial". Se rechaza,
porque una revision es inmutable y publicar una invalida la deja en el historial
para siempre.
"""


class ErrorEdicion(Exception):
    """La edicion no pudo representarse o confirmarse."""


class ErrorDocumentoEdicion(ErrorEdicion):
    """El documento no satisface las invariantes de su esquema."""


class ErrorCatalogoEdicion(ErrorDocumentoEdicion):
    """Se pidio un efecto, modo o parametro que el catalogo integrado no tiene.

    Es su propia clase porque decide algo distinto que el resto: no hay nada que
    corregir en los numeros, falta una capacidad. La UI la traduce a "esta
    version no conoce ese efecto" en vez de a "revisa el valor".
    """


class ErrorAssetEdicion(ErrorDocumentoEdicion):
    """El documento referencia un asset que el registro no puede resolver."""


class ErrorEsquemaEdicion(ErrorEdicion):
    """El documento serializado no es legible ni migrable a esta version."""


class ErrorPatchEdicion(ErrorEdicion):
    """El patch no aplica sobre el documento que recibio."""


class ErrorHistorialEdicion(ErrorEdicion):
    """La operacion de historial contradice la forma del grafo de revisiones."""
