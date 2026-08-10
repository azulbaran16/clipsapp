"""Identidad de jobs e intentos: la unica frontera que valida un componente.

Un `job_id` no es solo una clave: se convierte en un tramo de ruta dentro del
proyecto (`cache/jobs/<job>/<intento>/`, `artifacts/<job>/`). Por eso su forma
es una invariante de dominio y no un detalle del almacen: si un identificador
puede contener `..`, un separador o una unidad, entonces *cualquier* adaptador
que lo concatene escribe —o borra— fuera del proyecto, y habria que acordarse de
validarlo en cada punto de uso.

Aqui se valida una vez, y tanto `Job` como el almacen de artefactos llaman a la
misma funcion. La regla es deliberadamente estrecha: un componente valido es un
nombre, no una ruta. Todo lo que un sistema de archivos pueda reinterpretar
—separadores, `.`/`..`, unidades, flujos alternos, nombres de dispositivo,
espacios y puntos finales que Windows recorta— se rechaza antes de tocar disco.
"""

from __future__ import annotations

import unicodedata

from .errors import ErrorIdentificadorJob


LONGITUD_MAXIMA = 128

#: Separadores de *ambos* sistemas: un proyecto viaja entre Windows y POSIX, y
#: un nombre que aqui es inofensivo puede ser una ruta alla.
SEPARADORES = ("/", "\\")

#: `:` cubre unidades (`C:`) y flujos alternos NTFS (`archivo:oculto`); el resto
#: son caracteres que Windows no admite en un nombre y que en POSIX solo sirven
#: para confundir a quien lea un diagnostico.
CARACTERES_PROHIBIDOS = frozenset('<>:"|?*') | frozenset(chr(codigo) for codigo in range(32)) | {"\x7f"}

#: Nombres de dispositivo de Windows. `Path("CON")` no es un archivo: abrirlo
#: habla con la consola, y crearlo falla de formas dificiles de diagnosticar.
RESERVADOS_WINDOWS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{indice}" for indice in "123456789"}
    | {f"LPT{indice}" for indice in "123456789"}
)

#: Windows acepta los superindices como el digito correspondiente al resolver un
#: nombre de dispositivo: `COM¹` designa el mismo puerto que `COM1`. La tabla se
#: aplica siempre, corra donde corra, para que la frontera rechace lo mismo en
#: los dos sistemas y no dependa de la API del anfitrion.
SUPERINDICES = {"\u00b9": "1", "\u00b2": "2", "\u00b3": "3"}


def _nombre_de_dispositivo(valor: str) -> str:
    """Reduce un componente a la forma con la que Windows buscaria un device.

    Windows ignora la extension, recorta los espacios que preceden al punto y
    equipara los superindices a sus digitos. `CON .txt` y `COM².log` acaban
    siendo `CON` y `COM2`.
    """
    tronco = valor.split(".", 1)[0].rstrip(" ")
    return "".join(SUPERINDICES.get(caracter, caracter) for caracter in tronco).upper()


def es_componente_interno(valor: object) -> bool:
    try:
        exigir_componente_interno(valor)
    except ErrorIdentificadorJob:
        return False
    return True


def exigir_componente_interno(valor: object, contexto: str = "identificador") -> str:
    """Devuelve el componente si puede usarse como *un* tramo de ruta interna.

    No hace E/S: es una decision sobre la forma del texto, tomada antes de que
    exista cualquier efecto que deshacer.
    """
    if not isinstance(valor, str) or not valor:
        raise ErrorIdentificadorJob(f"El {contexto} debe ser un texto no vacio.")
    if len(valor) > LONGITUD_MAXIMA:
        raise ErrorIdentificadorJob(f"El {contexto} excede {LONGITUD_MAXIMA} caracteres.")
    if any(separador in valor for separador in SEPARADORES):
        raise ErrorIdentificadorJob(f"El {contexto} no puede contener separadores de ruta: {valor!r}")
    if valor in (".", ".."):
        raise ErrorIdentificadorJob(f"El {contexto} no puede ser una referencia de directorio: {valor!r}")
    prohibido = CARACTERES_PROHIBIDOS & set(valor)
    if prohibido:
        raise ErrorIdentificadorJob(
            f"El {contexto} contiene caracteres no permitidos: {sorted(prohibido)!r}")
    # Windows recorta espacios y puntos finales al abrir: `nombre.` y `nombre`
    # designarian la misma carpeta, de modo que dos ids distintos podrian
    # colisionar en disco.
    if valor != valor.rstrip(" ."):
        raise ErrorIdentificadorJob(f"El {contexto} no puede terminar en espacio o punto: {valor!r}")
    if _nombre_de_dispositivo(valor) in RESERVADOS_WINDOWS:
        raise ErrorIdentificadorJob(f"El {contexto} usa un nombre reservado del sistema: {valor!r}")
    # Dos secuencias Unicode distintas pueden nombrar el mismo archivo. Se exige
    # forma NFC para que el texto que se persiste sea ya el que el sistema de
    # archivos vera, y no una variante que colisione al escribirse.
    if valor != unicodedata.normalize("NFC", valor):
        raise ErrorIdentificadorJob(
            f"El {contexto} debe estar en forma normal NFC: {valor!r}")
    return valor


def clave_canonica(valor: str) -> str:
    """Clave con la que dos identificadores colisionan en disco.

    Windows y macOS no distinguen mayusculas en nombres de archivo: `A` y `a`
    designan la misma carpeta. Comparar por esta clave hace que la colision se
    detecte en cualquier sistema, y no solo alli donde el sistema de archivos la
    provocaria.
    """
    return unicodedata.normalize("NFC", valor).casefold()


def exigir_ruta_interna(valor: object, contexto: str = "ruta") -> tuple[str, ...]:
    """Descompone una ruta relativa en componentes ya validados.

    Se parte por *ambos* separadores antes de juzgar nada, de modo que una ruta
    absoluta (`/x`, `C:/x`) o una UNC (`\\\\servidor\\recurso`) produce un
    componente vacio o con `:` y muere en la misma comprobacion que el resto. El
    llamador recibe componentes, no una ruta: no puede reintroducir por
    concatenacion lo que aqui se rechazo.
    """
    if not isinstance(valor, str) or not valor:
        raise ErrorIdentificadorJob(f"La {contexto} debe ser un texto no vacio.")
    bruto = valor.replace("\\", "/").split("/")
    if any(not parte for parte in bruto):
        raise ErrorIdentificadorJob(f"La {contexto} no es relativa al proyecto: {valor!r}")
    return tuple(exigir_componente_interno(parte, f"componente de {contexto}") for parte in bruto)
