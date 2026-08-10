"""Normalizacion pura de un sondeo de medios.

Un documento de ffprobe no es un modelo: es la respuesta de una herramienta
concreta, con campos opcionales, numeros escritos como texto, fracciones
degeneradas (`0/0`) y dos formas distintas de declarar lo mismo (la rotacion
vive en `side_data_list` o en `tags.rotate` segun el contenedor). Dejar ese
documento correr por la aplicacion convertiria cada consumidor en un parser, y
cada parser tendria su propia idea de que significa "sin audio".

Aqui se traduce **una vez** a valores tipados, y la traduccion es pura: recibe
un documento ya decodificado y no toca disco ni procesos. Eso la hace probable
con documentos grabados —CFR, VFR, rotacion, sin audio, multistream, danada— en
cualquier sistema, con o sin FFmpeg instalado.

Tres decisiones gobiernan el archivo:

1. **Las fracciones no se aplanan a `float`.** Un fps de 30000/1001 no es
   29.97: redondearlo aqui haria que dos sondeos identicos produjesen claves de
   materializacion distintas segun el error de coma flotante del interprete. Se
   conserva `Racional` y solo se convierte a `float` cuando alguien lo pide.
2. **Lo describible es estado, no excepcion.** VFR, rotacion, ausencia de audio
   y multiples flujos son `AvisoMedia`; lo que impide construir el modelo es un
   error tipado.
3. **Nada del documento es entrada confiable.** Un campo presente pero ilegible
   no se degrada a un valor por defecto: o se lee, o se declara ausente.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
import math

from .errors import ErrorMediaDanada, ErrorMetadataMedia


#: Version del contrato de metadata. Entra en la clave de materializacion: si
#: esta normalizacion cambia de forma, los artefactos derivados dejan de ser
#: reutilizables aunque el archivo sea byte a byte el mismo.
VERSION_METADATA = "clipsapp-metadata/1"

#: Dos fps se consideran el mismo cuando difieren menos que esto en relativo.
#: `r_frame_rate` y `avg_frame_rate` de un CFR coinciden exactamente; la
#: tolerancia existe para que un contenedor que redondea uno de los dos no
#: declare VFR donde no lo hay.
TOLERANCIA_FPS = 1e-6

ROTACIONES_RECTAS = (0, 90, 180, 270)


@dataclass(frozen=True, order=True)
class Racional:
    """Fraccion exacta y ya reducida, con denominador siempre positivo.

    Se normaliza en la construccion para que la igualdad y el texto sean
    canonicos: `60/2` y `30/1` son el mismo valor y producen la misma clave.
    """

    numerador: int
    denominador: int = 1

    def __post_init__(self) -> None:
        for campo in ("numerador", "denominador"):
            valor = getattr(self, campo)
            if isinstance(valor, bool) or not isinstance(valor, int):
                raise ErrorMetadataMedia(f"El {campo} de una fraccion debe ser un entero.")
        if self.denominador == 0:
            raise ErrorMetadataMedia("Una fraccion no puede tener denominador cero.")
        numerador, denominador = self.numerador, self.denominador
        if denominador < 0:
            numerador, denominador = -numerador, -denominador
        divisor = math.gcd(abs(numerador), denominador) or 1
        object.__setattr__(self, "numerador", numerador // divisor)
        object.__setattr__(self, "denominador", denominador // divisor)

    @property
    def valor(self) -> float:
        return self.numerador / self.denominador

    def como_texto(self) -> str:
        return f"{self.numerador}/{self.denominador}"

    def equivalente_a(self, otro: "Racional", tolerancia: float = TOLERANCIA_FPS) -> bool:
        """Igualdad con tolerancia relativa; exacta cuando alguno vale cero."""
        if self == otro:
            return True
        mayor = max(abs(self.valor), abs(otro.valor))
        if mayor == 0:
            return True
        return abs(self.valor - otro.valor) / mayor <= tolerancia


class AvisoMedia(str, Enum):
    """Estado explicito de una fuente. Ninguno impide construir la metadata."""

    SIN_AUDIO = "media.sin_audio"
    MULTIPLES_VIDEO = "media.multiples_video"
    MULTIPLES_AUDIO = "media.multiples_audio"
    TASA_VARIABLE = "media.vfr"
    ROTACION = "media.rotacion"
    ROTACION_OBLICUA = "media.rotacion_oblicua"
    SIN_DURACION = "media.sin_duracion"
    SIN_TASA_DECLARADA = "media.sin_fps"
    FLUJOS_IGNORADOS = "media.flujos_ignorados"


@dataclass(frozen=True)
class FlujoVideo:
    indice: int
    codec: str
    ancho: int
    alto: int
    formato_pixel: str = ""
    timebase: Racional | None = None
    tasa_declarada: Racional | None = None
    tasa_promedio: Racional | None = None
    rotacion: int = 0
    duracion_segundos: float | None = None
    fotogramas: int | None = None
    tasa_variable: bool = False

    @property
    def ancho_mostrado(self) -> int:
        return self.alto if self.rotacion in (90, 270) else self.ancho

    @property
    def alto_mostrado(self) -> int:
        return self.ancho if self.rotacion in (90, 270) else self.alto


@dataclass(frozen=True)
class FlujoAudio:
    indice: int
    codec: str
    canales: int = 0
    tasa_muestreo: int = 0
    timebase: Racional | None = None
    duracion_segundos: float | None = None


@dataclass(frozen=True)
class MetadataMedia:
    """Lo que ClipperKick sabe de un medio, sin rastro de quien lo sondeo."""

    version: str = VERSION_METADATA
    contenedor: str = ""
    duracion_segundos: float | None = None
    tamano_bytes: int | None = None
    video: tuple[FlujoVideo, ...] = ()
    audio: tuple[FlujoAudio, ...] = ()
    otros_flujos: int = 0
    avisos: frozenset[AvisoMedia] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "avisos", frozenset(self.avisos))

    @property
    def principal(self) -> FlujoVideo:
        """Flujo de video de referencia. Su existencia es una invariante."""
        if not self.video:
            raise ErrorMediaDanada("La metadata no describe ningun flujo de video.")
        return self.video[0]

    @property
    def tiene_audio(self) -> bool:
        return bool(self.audio)

    def tiene(self, aviso: AvisoMedia) -> bool:
        return aviso in self.avisos

    def como_documento(self) -> dict[str, object]:
        """Forma canonica y ordenada; es lo que se publica y lo que se hashea.

        Se construye a mano en vez de derivarse del dataclass para que anadir un
        campo interno no cambie en silencio la clave de materializacion de todos
        los proyectos existentes: el documento es el contrato, y cambiarlo obliga
        a mover `VERSION_METADATA`.
        """
        return {
            "version": self.version,
            "contenedor": self.contenedor,
            "duracion_segundos": self.duracion_segundos,
            "tamano_bytes": self.tamano_bytes,
            "otros_flujos": self.otros_flujos,
            "avisos": sorted(aviso.value for aviso in self.avisos),
            "video": [
                {
                    "indice": flujo.indice,
                    "codec": flujo.codec,
                    "ancho": flujo.ancho,
                    "alto": flujo.alto,
                    "formato_pixel": flujo.formato_pixel,
                    "timebase": None if flujo.timebase is None else flujo.timebase.como_texto(),
                    "tasa_declarada": (None if flujo.tasa_declarada is None
                                       else flujo.tasa_declarada.como_texto()),
                    "tasa_promedio": (None if flujo.tasa_promedio is None
                                      else flujo.tasa_promedio.como_texto()),
                    "rotacion": flujo.rotacion,
                    "duracion_segundos": flujo.duracion_segundos,
                    "fotogramas": flujo.fotogramas,
                    "tasa_variable": flujo.tasa_variable,
                }
                for flujo in self.video
            ],
            "audio": [
                {
                    "indice": flujo.indice,
                    "codec": flujo.codec,
                    "canales": flujo.canales,
                    "tasa_muestreo": flujo.tasa_muestreo,
                    "timebase": None if flujo.timebase is None else flujo.timebase.como_texto(),
                    "duracion_segundos": flujo.duracion_segundos,
                }
                for flujo in self.audio
            ],
        }


# --------------------------------------------------------------------------- #
# Lectura defensiva del documento
# --------------------------------------------------------------------------- #

def _texto(valor: object) -> str:
    return valor if isinstance(valor, str) else ""


def _entero(valor: object) -> int | None:
    """Entero o `None`. Nunca inventa un cero, que seria un dato falso."""
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return valor
    if isinstance(valor, float):
        return int(valor) if math.isfinite(valor) and float(valor).is_integer() else None
    if isinstance(valor, str):
        try:
            return int(valor.strip())
        except ValueError:
            return None
    return None


def _real(valor: object) -> float | None:
    """`float` finito, o `None`. `N/A`, `nan` e `inf` son ausencias, no valores."""
    if isinstance(valor, bool):
        return None
    if isinstance(valor, (int, float)):
        candidato = float(valor)
    elif isinstance(valor, str):
        try:
            candidato = float(valor.strip())
        except ValueError:
            return None
    else:
        return None
    return candidato if math.isfinite(candidato) else None


def leer_racional(valor: object) -> Racional | None:
    """Interpreta `"30000/1001"`, `"25"` o un numero. `0/0` es ausencia.

    ffprobe usa `0/0` para decir "no lo se" en `avg_frame_rate`. Traducirlo a
    cero convertiria un dato ausente en la afirmacion "cero fotogramas por
    segundo", que es exactamente el tipo de mentira que luego nadie sabe de
    donde salio.
    """
    if isinstance(valor, str):
        texto = valor.strip()
        if not texto or texto.upper() == "N/A":
            return None
        if "/" in texto:
            numerador, _, denominador = texto.partition("/")
            arriba, abajo = _entero(numerador), _entero(denominador)
            if arriba is None or abajo is None or abajo == 0:
                return None
            return Racional(arriba, abajo)
        entero = _entero(texto)
        if entero is not None:
            return Racional(entero, 1)
        aproximado = _real(texto)
        return None if aproximado is None else _desde_real(aproximado)
    if isinstance(valor, bool):
        return None
    if isinstance(valor, int):
        return Racional(valor, 1)
    if isinstance(valor, float):
        return _desde_real(valor)
    return None


def _desde_real(valor: float) -> Racional | None:
    """Convierte un decimal a fraccion exacta con denominador acotado.

    Se usa solo cuando el documento trae un numero donde el contrato de ffprobe
    promete una fraccion. El limite evita que un decimal periodico produzca un
    denominador absurdo que despues nadie pueda comparar.
    """
    if not math.isfinite(valor):
        return None
    numerador, denominador = valor.as_integer_ratio()
    if denominador > 1_000_000:
        numerador, denominador = round(valor * 1_000_000), 1_000_000
    return Racional(numerador, denominador)


def _rotacion_de(flujo: Mapping[str, object]) -> int | None:
    """Rotacion declarada, venga de la matriz de display o de las etiquetas.

    Se normaliza a `[0, 360)` para que `-90` y `270` —que designan lo mismo y
    aparecen segun el contenedor— no produzcan dos metadatas distintas para el
    mismo archivo.
    """
    laterales = flujo.get("side_data_list")
    if isinstance(laterales, Sequence) and not isinstance(laterales, (str, bytes)):
        for lateral in laterales:
            if not isinstance(lateral, Mapping):
                continue
            crudo = _real(lateral.get("rotation"))
            if crudo is not None:
                return int(round(crudo)) % 360
    etiquetas = flujo.get("tags")
    if isinstance(etiquetas, Mapping):
        crudo = _real(etiquetas.get("rotate"))
        if crudo is not None:
            return int(round(crudo)) % 360
    return None


def _duracion_de(flujo: Mapping[str, object], timebase: Racional | None) -> float | None:
    """Duracion del flujo: la declarada, o la derivada de su propia timebase."""
    declarada = _real(flujo.get("duration"))
    if declarada is not None and declarada >= 0:
        return declarada
    marcas = _entero(flujo.get("duration_ts"))
    if marcas is not None and marcas >= 0 and timebase is not None:
        return marcas * timebase.valor
    return None


def _normalizar_video(indice_declarado: object, flujo: Mapping[str, object],
                      posicion: int) -> tuple[FlujoVideo, set[AvisoMedia]]:
    ancho, alto = _entero(flujo.get("width")), _entero(flujo.get("height"))
    if ancho is None or alto is None or ancho <= 0 or alto <= 0:
        raise ErrorMediaDanada(
            f"El flujo de video {posicion} no declara dimensiones utilizables.")
    timebase = leer_racional(flujo.get("time_base"))
    declarada = leer_racional(flujo.get("r_frame_rate"))
    promedio = leer_racional(flujo.get("avg_frame_rate"))
    rotacion = _rotacion_de(flujo)
    avisos: set[AvisoMedia] = set()
    if rotacion:
        avisos.add(AvisoMedia.ROTACION)
        if rotacion not in ROTACIONES_RECTAS:
            # Una rotacion que no es multiplo de 90 no se puede aplicar sin
            # reencuadrar. Se conserva el dato y se avisa en vez de redondearlo:
            # redondear cambiaria el encuadre sin que nadie lo hubiera pedido.
            avisos.add(AvisoMedia.ROTACION_OBLICUA)
    variable = False
    if declarada is None and promedio is None:
        avisos.add(AvisoMedia.SIN_TASA_DECLARADA)
    elif declarada is not None and promedio is not None:
        variable = not declarada.equivalente_a(promedio)
        if variable:
            avisos.add(AvisoMedia.TASA_VARIABLE)
    return (
        FlujoVideo(
            indice=_entero(indice_declarado) if _entero(indice_declarado) is not None else posicion,
            codec=_texto(flujo.get("codec_name")),
            ancho=ancho, alto=alto,
            formato_pixel=_texto(flujo.get("pix_fmt")),
            timebase=timebase, tasa_declarada=declarada, tasa_promedio=promedio,
            rotacion=rotacion or 0,
            duracion_segundos=_duracion_de(flujo, timebase),
            fotogramas=_entero(flujo.get("nb_frames")),
            tasa_variable=variable),
        avisos)


def _normalizar_audio(indice_declarado: object, flujo: Mapping[str, object],
                      posicion: int) -> FlujoAudio:
    timebase = leer_racional(flujo.get("time_base"))
    return FlujoAudio(
        indice=_entero(indice_declarado) if _entero(indice_declarado) is not None else posicion,
        codec=_texto(flujo.get("codec_name")),
        canales=_entero(flujo.get("channels")) or 0,
        tasa_muestreo=_entero(flujo.get("sample_rate")) or 0,
        timebase=timebase,
        duracion_segundos=_duracion_de(flujo, timebase))


#: Campos del sondeo que dependen de *donde* estaba el archivo, no de que
#: contiene. Se retiran antes de publicar para que dos proyectos que ingieren
#: los mismos bytes —uno copiando un archivo local, otro terminando una
#: descarga— produzcan un artefacto identico byte a byte. Sin esto, la ruta se
#: colaria en el contenido publicado y la equivalencia solo seria observable
#: comparando la metadata normalizada, nunca el artefacto.
CAMPOS_VOLATILES_FORMATO = ("filename",)


def sondeo_canonico(documento: Mapping[str, object]) -> dict[str, object]:
    """Documento de sondeo sin los campos que dependen de la ubicacion."""
    if not isinstance(documento, Mapping):
        raise ErrorMetadataMedia("El sondeo no devolvio un documento.")
    canonico = dict(documento)
    formato = canonico.get("format")
    if isinstance(formato, Mapping):
        canonico["format"] = {clave: valor for clave, valor in formato.items()
                              if clave not in CAMPOS_VOLATILES_FORMATO}
    return canonico


def normalizar_sondeo(documento: Mapping[str, object]) -> MetadataMedia:
    """Traduce el documento de un sondeo a `MetadataMedia`.

    Lo que se rechaza es exactamente lo que impide seguir: un documento que no
    es un objeto, sin lista de flujos, o sin un flujo de video legible. Todo lo
    demas —incluida la ausencia total de duracion— se describe y se avisa.
    """
    if not isinstance(documento, Mapping):
        raise ErrorMetadataMedia("El sondeo no devolvio un documento.")
    flujos = documento.get("streams")
    if not isinstance(flujos, Sequence) or isinstance(flujos, (str, bytes)):
        raise ErrorMediaDanada("El sondeo no declara los flujos del archivo.")
    formato = documento.get("format")
    formato = formato if isinstance(formato, Mapping) else {}

    videos: list[FlujoVideo] = []
    audios: list[FlujoAudio] = []
    avisos: set[AvisoMedia] = set()
    otros = 0
    for posicion, flujo in enumerate(flujos):
        if not isinstance(flujo, Mapping):
            raise ErrorMediaDanada(f"El flujo {posicion} del sondeo no es un objeto.")
        tipo = _texto(flujo.get("codec_type"))
        if tipo == "video":
            normalizado, propios = _normalizar_video(flujo.get("index"), flujo, posicion)
            videos.append(normalizado)
            avisos |= propios
        elif tipo == "audio":
            audios.append(_normalizar_audio(flujo.get("index"), flujo, posicion))
        else:
            otros += 1
    if not videos:
        raise ErrorMediaDanada("El archivo no contiene ningun flujo de video.")
    if len(videos) > 1:
        avisos.add(AvisoMedia.MULTIPLES_VIDEO)
    if not audios:
        avisos.add(AvisoMedia.SIN_AUDIO)
    elif len(audios) > 1:
        avisos.add(AvisoMedia.MULTIPLES_AUDIO)
    if otros:
        avisos.add(AvisoMedia.FLUJOS_IGNORADOS)

    duracion = _real(formato.get("duration"))
    if duracion is None or duracion < 0:
        # El contenedor no la declara: se toma la mayor de las duraciones de
        # flujo, que es lo unico que se sabe con certeza sobre cuanto dura el
        # material. Si tampoco existe, se declara ausente y se avisa.
        candidatas = [flujo.duracion_segundos
                      for flujo in (*videos, *audios)
                      if flujo.duracion_segundos is not None]
        duracion = max(candidatas) if candidatas else None
    if duracion is None:
        avisos.add(AvisoMedia.SIN_DURACION)

    tamano = _entero(formato.get("size"))
    return MetadataMedia(
        version=VERSION_METADATA,
        contenedor=_texto(formato.get("format_name")),
        duracion_segundos=duracion,
        tamano_bytes=tamano if tamano is not None and tamano >= 0 else None,
        video=tuple(videos), audio=tuple(audios), otros_flujos=otros,
        avisos=frozenset(avisos))
