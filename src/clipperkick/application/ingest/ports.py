"""Puertos de la ingesta.

El caso de uso solo sabe pedir seis cosas: obtener bytes (`DescargadorFuente`),
colocarlos dentro del proyecto (`AlmacenFuentes`), identificarlos
(`ServicioHuellas`), describirlos (`SondaDetallada`), publicarlos como artefacto
(`AlmacenArtefactosIngesta`) y recordarlos (`RepositorioFuentes`). yt-dlp,
ffprobe, SQLite y el sistema de archivos viven detras de estas firmas, y por eso
la ingesta entera se puede ejercitar sin instalar nada.

`SondaDetallada` devuelve el **documento crudo** del proveedor y no metadata ya
normalizada. Es deliberado: la normalizacion es una regla de dominio versionada
(`VERSION_METADATA`) que entra en la clave de materializacion, y dejarla en el
adaptador permitiria que dos adaptadores normalizasen distinto bajo la misma
version de contrato.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import ContextManager, Protocol

from clipperkick.domain.ingest import (
    ArchivoArtefacto, DiagnosticoIngesta, HuellaFuente, ManifiestoArtefacto, MetadataMedia,
    RegistroArtefacto, SourceAsset,
)


#: Etapa logica que produce el sondeo normalizado de una fuente. El nombre entra
#: en la clave de materializacion y en el linaje del artefacto.
NOMBRE_STAGE_SONDEO = "ingesta.sondeo"
VERSION_CONTRATO_SONDEO = "1"
TIPO_ARTEFACTO_SONDEO = "probe"
NOMBRE_ARCHIVO_SONDEO = "probe.json"

#: Antiguedad a partir de la cual un temporal de ingesta se considera
#: abandonado. Un proyecto tiene un unico escritor, de modo que nada vivo puede
#: estar usando un staging de hace una hora.
ANTIGUEDAD_TEMPORALES = 3600.0

Notificador = Callable[[DiagnosticoIngesta], None]
Progreso = Callable[[float, float | None], None]
#: Token de cancelacion cooperativa. Se consulta, no se espera: quien lo
#: implementa decide como se enciende (un boton, un `threading.Event`, un
#: `Job` cancelado) y quien lo recibe solo tiene que preguntarle a menudo.
#: Es una funcion y no un objeto para que fingirlo en una prueba sea `lambda:
#: True` y no una clase entera.
Cancelacion = Callable[[], bool]


def nunca_cancelado() -> bool:
    """Token nulo. Existe para que nadie tenga que comprobar `None`."""
    return False


@dataclass(frozen=True)
class UbicacionFuente:
    """Donde quedaron los bytes tras incorporarlos al proyecto."""

    ruta_absoluta: str
    ruta_relativa: str | None = None
    nombre: str = ""
    #: Ruta del original que se pidio trasladar y no se pudo retirar. La
    #: incorporacion fue correcta —el destino esta confirmado— pero quedo una
    #: copia del origen, y eso se cuenta en vez de dejarlo como basura invisible.
    origen_pendiente: str | None = None


class DescargadorFuente(Protocol):
    """Obtiene una URL terminada en un archivo dentro del directorio dado.

    `cancelado` forma parte de la firma y no de una variante opcional del
    adaptador: una descarga es la unica operacion de la ingesta que puede durar
    horas, y sin un contrato de cancelacion en el puerto el caso de uso no tiene
    forma de pedir que pare. Quien lo implementa se compromete a **volver con el
    proceso hijo ya recolectado**: devolver el control con un `Popen` vivo deja
    el staging inborrable en Windows y un escritor suelto en cualquier sistema.
    """

    def descargar(self, url: str, directorio: str, hd: bool,
                  progreso: Progreso | None = None,
                  cancelado: Cancelacion | None = None) -> str: ...


class AlmacenFuentes(Protocol):
    """Coloca y localiza los bytes de una fuente dentro de la carpeta."""

    def staging(self) -> ContextManager[str]: ...
    def normalizar(self, entrada: str) -> str: ...
    def incorporar(self, origen: str, nombre: str = "", mover: bool = False) -> UbicacionFuente: ...
    def referenciar(self, origen: str) -> UbicacionFuente: ...
    def resolver(self, fuente: SourceAsset) -> str: ...
    def disponible(self, fuente: SourceAsset) -> bool: ...
    def retirar(self, ruta_relativa: str) -> None: ...
    def limpiar_abandonados(self, antiguedad: float, ahora: float | None = None) -> tuple[str, ...]: ...


class ServicioHuellas(Protocol):
    """`SourceFingerprintService`: identifica y reconcilia fuentes."""

    def parcial(self, ruta: str) -> HuellaFuente: ...
    def completa(self, ruta: str) -> HuellaFuente: ...


class SondaDetallada(Protocol):
    """Sondeo completo del proveedor de medios, sin normalizar."""

    def describir(self, ruta: str) -> Mapping[str, object]: ...
    def version(self) -> str: ...


class AlmacenArtefactosIngesta(Protocol):
    """Staging, verificacion de checksum y publicacion atomica por clave."""

    def preparar(self, clave: str) -> str: ...
    def escribir(self, directorio: str, ruta: str, datos: bytes) -> ArchivoArtefacto: ...
    def publicar(self, clave: str, directorio: str,
                 manifiesto: ManifiestoArtefacto) -> tuple[str, ...]: ...
    def descartar(self, directorio: str) -> None: ...
    def cuarentena(self, directorio: str) -> str | None: ...
    def presentes(self, clave: str) -> Mapping[str, str]: ...
    def leer_documento(self, clave: str, ruta: str) -> Mapping[str, object]: ...
    def leer_verificado(self, clave: str, ruta: str,
                        checksum: str) -> Mapping[str, object]: ...
    def limpiar_abandonados(self, antiguedad: float,
                            ahora: float | None = None) -> tuple[str, ...]: ...


class RepositorioFuentes(Protocol):
    """Estado durable de fuentes y de los artefactos que dependen de ellas."""

    def buscar_por_identidad(self, identidad: str) -> SourceAsset | None: ...
    def buscar_por_origen(self, entrada: str) -> SourceAsset | None: ...
    def registrar(self, fuente: SourceAsset, reemplaza_a: str | None = None) -> SourceAsset: ...
    def relocalizar(self, fuente_id: str, modo: str, ruta_relativa: str | None,
                    ruta_externa: str | None) -> SourceAsset: ...
    def suceder(self, nueva: SourceAsset,
                anterior_id: str) -> tuple[SourceAsset, tuple[str, ...]]: ...
    def artefacto(self, clave: str) -> RegistroArtefacto | None: ...
    def registrar_artefacto(self, registro: RegistroArtefacto,
                            bytes_totales: int = 0) -> RegistroArtefacto: ...


@dataclass(frozen=True)
class SolicitudIngesta:
    """Lo que la persona pide: una entrada y como tratarla."""

    entrada: str
    copiar_local: bool = True
    preferir_hd: bool = True
    nombre: str = ""
    limpiar_temporales: bool = True


@dataclass(frozen=True)
class ResultadoIngesta:
    """Lo que quedo: fuente estable, metadata normalizada y artefacto valido."""

    fuente: SourceAsset
    metadata: MetadataMedia
    clave: str
    artefactos: tuple[str, ...] = ()
    fuente_reutilizada: bool = False
    artefacto_reutilizado: bool = False
    fuente_reemplazada: str | None = None
    #: Id de la fuente cuya copia registrada no contenia sus propios bytes y se
    #: rehizo con el material entrante. No es una reutilizacion: lo guardado
    #: estaba mal, y quien lea el resultado tiene que poder distinguirlo.
    fuente_reparada: str | None = None
    invalidados: tuple[str, ...] = ()
    diagnosticos: tuple[DiagnosticoIngesta, ...] = ()
    temporales_retirados: tuple[str, ...] = ()


def sin_notificar(_diagnostico: DiagnosticoIngesta) -> None:
    """Notificador nulo. Existe para que el caso de uso no compruebe `None`."""


def rutas_de(manifiesto: ManifiestoArtefacto) -> Sequence[str]:
    return tuple(archivo.ruta for archivo in manifiesto.archivos)


__all__ = [
    "ANTIGUEDAD_TEMPORALES", "AlmacenArtefactosIngesta", "AlmacenFuentes", "Cancelacion",
    "DescargadorFuente",
    "NOMBRE_ARCHIVO_SONDEO", "NOMBRE_STAGE_SONDEO", "Notificador", "Progreso", "RepositorioFuentes",
    "ResultadoIngesta", "ServicioHuellas", "SolicitudIngesta", "SondaDetallada",
    "TIPO_ARTEFACTO_SONDEO", "UbicacionFuente", "VERSION_CONTRATO_SONDEO", "nunca_cancelado",
    "rutas_de", "sin_notificar",
]
