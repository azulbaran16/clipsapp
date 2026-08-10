"""Puertos del analisis basico.

El caso de uso solo sabe pedir cinco cosas: localizar la fuente
(`LocalizadorFuente`), extraer su audio (`ExtractorAudio`), medir su sonoridad
(`MedidorSonoridad`), publicar artefactos (`AlmacenArtefactosAnalisis`) y
recordar candidatos (`RepositorioAnalisis`). FFmpeg y SQLite viven detras de
estas firmas, y por eso el analisis entero se puede ejercitar sin instalar nada.

## Por que hay tres etapas y no una

Cada etapa tiene su propia clave de materializacion, y el reparto de la
configuracion entre ellas es lo que hace cierta la promesa del ticket:

```text
audio     <- huella de la fuente        + ConfiguracionExtraccion
rasgos    <- checksum del audio         + ConfiguracionRasgos
ranking   <- checksum de los rasgos     + ConfiguracionRanking + senales opcionales
```

Subir de 6 a 8 clips solo cambia el tercer renglon. Con una sola etapa —o con
una configuracion unica— ese cambio invalidaria la decodificacion del VOD
entero, y ninguna cache posterior podria arreglarlo.

La entrada de cada etapa es el **checksum de lo que produjo la anterior**, no su
clave. Son casi lo mismo hasta que dejan de serlo: la clave describe con que
intencion se produjo algo, el checksum describe que salio. Si unos bytes
publicados se corrompen y se rehacen, la clave no cambia y el checksum si; usar
la clave dejaria vivo un ranking calculado sobre una serie que ya no existe.

## Version del proveedor del ranking

El ranking no tiene proveedor externo: lo calcula el nucleo. Aun asi declara
version, y no una constante decorativa: entra en la clave y es lo que invalida
los rankings publicados el dia que la regla de puntuacion cambie. Sin ella,
mejorar el algoritmo dejaria a los proyectos existentes mostrando para siempre
los candidatos de la version vieja.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from clipperkick.domain.analysis import (
    ConfiguracionExtraccion, ConfiguracionRanking, ConfiguracionRasgos, DiagnosticoAnalisis,
    Momento, ResultadoRanking, SenalExterna,
)
from clipperkick.domain.ingest import (
    ArchivoArtefacto, ManifiestoArtefacto, RegistroArtefacto, SourceAsset,
)


#: Etapa 1: la fuente se convierte en una pista mono normalizada del proyecto.
NOMBRE_STAGE_AUDIO = "analisis.audio"
VERSION_CONTRATO_AUDIO = "1"
TIPO_ARTEFACTO_AUDIO = "audio"
NOMBRE_ARCHIVO_AUDIO = "audio.wav"

#: Etapa 2: esa pista se mide y produce la serie de sonoridad versionada.
NOMBRE_STAGE_RASGOS = "analisis.rasgos"
VERSION_CONTRATO_RASGOS = "1"
TIPO_ARTEFACTO_RASGOS = "features"
NOMBRE_ARCHIVO_RASGOS = "features.json"

#: Etapa 3: la serie se convierte en candidatos explicados.
NOMBRE_STAGE_RANKING = "analisis.ranking"
VERSION_CONTRATO_RANKING = "1"
TIPO_ARTEFACTO_RANKING = "candidates"
NOMBRE_ARCHIVO_RANKING = "candidates.json"
#: Version del ranking del nucleo. Mover la regla de puntuacion obliga a moverla.
VERSION_RANKER_NUCLEO = "nucleo/1"

#: Formatos de los documentos publicados por cada etapa.
FORMATO_RASGOS = "clipsapp-rasgos/1"
FORMATO_RANKING = "clipsapp-candidatos/1"

#: Misma politica de temporales abandonados que la ingesta: un proyecto tiene un
#: unico escritor, de modo que nada vivo puede estar usando un staging de hace
#: una hora.
ANTIGUEDAD_TEMPORALES = 3600.0

#: Tope de una pagina de candidatos. La UI pide lo que quiera; el repositorio no
#: devuelve un proyecto entero por una peticion sin limite.
LIMITE_MAXIMO_PAGINA = 500

Notificador = Callable[[DiagnosticoAnalisis], None]
Progreso = Callable[[float, float | None], None]
#: Token de cancelacion cooperativa. Se declara aqui y no se importa de la
#: ingesta a proposito: es un alias de una linea, y compartirlo ataria el
#: analisis a un paquete cuyo contrato esta congelado.
Cancelacion = Callable[[], bool]


def nunca_cancelado() -> bool:
    """Token nulo. Existe para que nadie tenga que comprobar `None`."""
    return False


def sin_notificar(_diagnostico: DiagnosticoAnalisis) -> None:
    """Notificador nulo. Existe para que el caso de uso no compruebe `None`."""


def sin_progreso(_hechas: float, _totales: float | None) -> None:
    """Progreso nulo. Existe para que el caso de uso no compruebe `None`."""


class OrdenMomentos(str, Enum):
    """Como se ordena una pagina de candidatos. Cerrado a proposito.

    Aceptar un fragmento de SQL del llamador convertiria la UI en autora de
    consultas y haria imposible garantizar que el orden es estable.
    """

    SCORE = "score"
    INICIO = "inicio"
    POSICION = "posicion"


class LocalizadorFuente(Protocol):
    """Donde estan los bytes de una fuente ya registrada."""

    def resolver(self, fuente: SourceAsset) -> str: ...
    def disponible(self, fuente: SourceAsset) -> bool: ...


class ExtractorAudio(Protocol):
    """Deja en `destino` una pista de audio normalizada de la fuente.

    Escribe en la ruta que se le da —dentro del staging del artefacto— en vez de
    devolver bytes: una pista de tres horas no cabe en memoria y forzarla a
    caber convertiria una etapa util en un limite de tamano.
    """

    def extraer(self, origen: str, destino: str, config: ConfiguracionExtraccion,
                cancelado: Cancelacion | None = None) -> None: ...
    def version(self) -> str: ...


class MedidorSonoridad(Protocol):
    """Mide la sonoridad de una pista y devuelve el documento **crudo**.

    Igual que `SondaDetallada` en la ingesta: normalizar es una regla de dominio
    versionada, y dejarla en el adaptador permitiria que dos medidores
    produjesen series distintas bajo la misma version de contrato.
    """

    def medir(self, ruta: str, config: ConfiguracionRasgos,
              cancelado: Cancelacion | None = None) -> Mapping[str, object]: ...
    def version(self) -> str: ...


class ProveedorSenalOpcional(Protocol):
    """Capacidad opcional que aporta una senal ya normalizada al ranking.

    Es el seam por el que entran transcripcion y seguimiento sin que T05 sepa
    nada de ellos. Su ausencia o su fallo no interrumpe el analisis: produce un
    diagnostico y una razon visible en cada candidato.
    """

    def nombre(self) -> str: ...
    def version(self) -> str: ...
    def senal(self, ruta_fuente: str, ruta_audio: str) -> SenalExterna: ...


class AlmacenArtefactosAnalisis(Protocol):
    """Staging, checksum y publicacion atomica por clave de materializacion."""

    def preparar(self, clave: str) -> str: ...
    def escribir(self, directorio: str, ruta: str, datos: bytes) -> ArchivoArtefacto: ...
    def ruta_en(self, directorio: str, ruta: str) -> str: ...
    def declarar(self, directorio: str, ruta: str) -> ArchivoArtefacto: ...
    def publicar(self, clave: str, directorio: str,
                 manifiesto: ManifiestoArtefacto) -> tuple[str, ...]: ...
    def descartar(self, directorio: str) -> None: ...
    def cuarentena(self, directorio: str) -> str | None: ...
    def presentes(self, clave: str) -> Mapping[str, str]: ...
    def ruta_publicada(self, clave: str, ruta: str) -> str: ...
    def leer_verificado(self, clave: str, ruta: str,
                        checksum: str) -> Mapping[str, object]: ...
    def limpiar_abandonados(self, antiguedad: float,
                            ahora: float | None = None) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class PaginaMomentos:
    """Tramo de candidatos mas el total, que es lo que la UI necesita paginar."""

    momentos: tuple[Momento, ...] = ()
    total: int = 0
    desplazamiento: int = 0
    limite: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "momentos", tuple(self.momentos))

    @property
    def hay_mas(self) -> bool:
        return self.desplazamiento + len(self.momentos) < self.total


class RepositorioAnalisis(Protocol):
    """Estado durable de artefactos de analisis y de candidatos."""

    def fuente(self, fuente_id: str) -> SourceAsset | None: ...
    def artefacto(self, clave: str) -> RegistroArtefacto | None: ...
    def registrar_artefacto(self, registro: RegistroArtefacto,
                            bytes_totales: int = 0) -> RegistroArtefacto: ...
    def materializar(self, fuente_id: str, clave: str,
                     momentos: Sequence[Momento]) -> tuple[str, ...]: ...
    def consultar(self, fuente_id: str | None = None, clave: str | None = None,
                  orden: OrdenMomentos = OrdenMomentos.SCORE, descendente: bool = True,
                  limite: int = 50, desplazamiento: int = 0,
                  estados: Sequence[str] = ()) -> PaginaMomentos: ...
    def momento(self, momento_id: str) -> Momento | None: ...


@dataclass(frozen=True)
class SolicitudAnalisis:
    """Lo que la persona pide en el Flujo 3: cuantos clips y de que duracion."""

    fuente_id: str
    ranking: ConfiguracionRanking = field(default_factory=ConfiguracionRanking)
    extraccion: ConfiguracionExtraccion = field(default_factory=ConfiguracionExtraccion)
    rasgos: ConfiguracionRasgos = field(default_factory=ConfiguracionRasgos)
    limpiar_temporales: bool = True


@dataclass(frozen=True)
class ResultadoAnalisis:
    """Lo que quedo: candidatos materializados y de donde salieron.

    Los tres `*_reutilizado` no son telemetria: son la forma de comprobar desde
    fuera —y desde una prueba— que rerankear no volvio a tocar el audio. Sin
    ellos esa garantia solo se podria verificar cronometrando.
    """

    fuente_id: str
    clave_audio: str
    clave_rasgos: str
    clave_ranking: str
    ranking: ResultadoRanking
    momentos: tuple[Momento, ...] = ()
    audio_reutilizado: bool = False
    rasgos_reutilizados: bool = False
    ranking_reutilizado: bool = False
    artefactos: tuple[str, ...] = ()
    senales_omitidas: tuple[str, ...] = ()
    diagnosticos: tuple[DiagnosticoAnalisis, ...] = ()
    temporales_retirados: tuple[str, ...] = ()

    @property
    def suficientes(self) -> bool:
        return self.ranking.suficientes


__all__ = [
    "ANTIGUEDAD_TEMPORALES", "AlmacenArtefactosAnalisis", "Cancelacion", "ExtractorAudio",
    "FORMATO_RANKING", "FORMATO_RASGOS", "LIMITE_MAXIMO_PAGINA", "LocalizadorFuente",
    "MedidorSonoridad", "NOMBRE_ARCHIVO_AUDIO", "NOMBRE_ARCHIVO_RANKING", "NOMBRE_ARCHIVO_RASGOS",
    "NOMBRE_STAGE_AUDIO", "NOMBRE_STAGE_RANKING", "NOMBRE_STAGE_RASGOS", "Notificador",
    "OrdenMomentos", "PaginaMomentos", "Progreso", "ProveedorSenalOpcional", "RepositorioAnalisis",
    "ResultadoAnalisis", "SolicitudAnalisis", "TIPO_ARTEFACTO_AUDIO", "TIPO_ARTEFACTO_RANKING",
    "TIPO_ARTEFACTO_RASGOS", "VERSION_CONTRATO_AUDIO", "VERSION_CONTRATO_RANKING",
    "VERSION_CONTRATO_RASGOS", "VERSION_RANKER_NUCLEO", "nunca_cancelado", "sin_notificar",
    "sin_progreso",
]
