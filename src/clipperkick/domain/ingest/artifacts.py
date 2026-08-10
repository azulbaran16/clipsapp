"""Artefactos de analisis: que se declara, que se verifica y cuando se reutiliza.

El ticket pone dos condiciones sobre la misma pieza:

1. *Ningun archivo se registra como artefacto valido antes de checksum y
   validacion.*
2. *Un artefacto solo se reutiliza si sus bytes, su manifiesto y su linaje
   siguen siendo validos.*

La primera obliga a que el productor **declare** el checksum de lo que escribio
—no basta con que el almacen lo calcule despues, porque entonces el almacen
estaria certificando lo que encontro, no lo que la etapa dijo producir—. La
segunda obliga a guardar el linaje junto al contenido: dos etapas distintas
pueden producir bytes identicos por casualidad, y sin linaje una adoptaria el
artefacto de la otra.

Ambas viven aqui, en valores puros, para que el almacen de T03, el repositorio
SQLite y el caso de uso compartan una sola definicion de "valido".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from clipperkick.domain.jobs.errors import ErrorIdentificadorJob
from clipperkick.domain.jobs.identity import exigir_ruta_interna

from .errors import ErrorArtefactoIngesta


#: Clave reservada del `metadata` de un manifiesto de salida. El almacen la
#: reconoce y verifica los digests declarados antes de publicar nada. Vive en el
#: dominio —y no en el adaptador— porque productor y almacen tienen que estar de
#: acuerdo en el nombre sin que ninguno importe al otro.
CLAVE_CHECKSUMS = "checksums"
PREFIJO_CHECKSUM = "sha256:"

ESTADO_DISPONIBLE = "available"
ESTADO_RECONSTRUIBLE = "rebuildable"


def etiquetar_checksum(digest: str) -> str:
    """Digest con su algoritmo delante: un hexadecimal suelto no dice de que es."""
    if not isinstance(digest, str) or not digest:
        raise ErrorArtefactoIngesta("Un checksum declarado no puede estar vacio.")
    return digest if digest.startswith(PREFIJO_CHECKSUM) else PREFIJO_CHECKSUM + digest


@dataclass(frozen=True)
class LinajeArtefacto:
    """Quien produjo el artefacto, con que contrato y sobre que entradas."""

    stage: str
    version_contrato: str
    version_proveedor: str
    clave: str
    entradas: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for campo in ("stage", "version_contrato", "version_proveedor", "clave"):
            valor = getattr(self, campo)
            if not isinstance(valor, str) or not valor:
                raise ErrorArtefactoIngesta(f"El linaje de un artefacto necesita '{campo}'.")
        entradas = dict(self.entradas)
        for nombre, huella in entradas.items():
            if not isinstance(nombre, str) or not nombre or not isinstance(huella, str) or not huella:
                raise ErrorArtefactoIngesta("El linaje declara una entrada sin nombre o sin huella.")
        object.__setattr__(self, "entradas", entradas)

    def como_documento(self) -> dict[str, object]:
        return {"stage": self.stage, "version_contrato": self.version_contrato,
                "version_proveedor": self.version_proveedor, "clave": self.clave,
                "entradas": dict(sorted(self.entradas.items()))}


@dataclass(frozen=True)
class ArchivoArtefacto:
    """Archivo declarado por el productor, con su tamano y su digest."""

    ruta: str
    bytes: int
    sha256: str

    def __post_init__(self) -> None:
        try:
            exigir_ruta_interna(self.ruta, "ruta del artefacto")
        except ErrorIdentificadorJob as error:
            raise ErrorArtefactoIngesta(
                f"El artefacto declara una ruta que no es interna: {self.ruta!r}") from error
        if isinstance(self.bytes, bool) or not isinstance(self.bytes, int) or self.bytes < 0:
            raise ErrorArtefactoIngesta("El artefacto declara un tamano que no es un entero valido.")
        object.__setattr__(self, "sha256", etiquetar_checksum(self.sha256))


@dataclass(frozen=True)
class ManifiestoArtefacto:
    """Lo que una etapa afirma haber producido, antes de que nadie lo mire."""

    tipo: str
    archivos: tuple[ArchivoArtefacto, ...] = ()
    linaje: LinajeArtefacto | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.tipo, str) or not self.tipo:
            raise ErrorArtefactoIngesta("Un manifiesto de artefacto necesita su tipo.")
        vistas: set[str] = set()
        for archivo in self.archivos:
            if archivo.ruta in vistas:
                raise ErrorArtefactoIngesta(f"El manifiesto declara dos veces {archivo.ruta!r}.")
            vistas.add(archivo.ruta)
        object.__setattr__(self, "metadata", dict(self.metadata))

    def checksums(self) -> dict[str, str]:
        return {archivo.ruta: archivo.sha256 for archivo in self.archivos}

    def metadata_publicable(self) -> dict[str, object]:
        """Metadata mas los checksums, en la clave que el almacen reconoce."""
        documento = dict(self.metadata)
        documento["kind"] = self.tipo
        documento[CLAVE_CHECKSUMS] = self.checksums()
        if self.linaje is not None:
            documento["linaje"] = self.linaje.como_documento()
        return documento


@dataclass(frozen=True)
class RegistroArtefacto:
    """Artefacto ya publicado, tal y como lo recuerda el proyecto."""

    clave: str
    tipo: str
    rutas: tuple[str, ...]
    checksums: Mapping[str, str]
    linaje: LinajeArtefacto
    estado: str = ESTADO_DISPONIBLE
    fuente_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "rutas", tuple(self.rutas))
        # Los digests se guardan siempre etiquetados. El almacen los calcula en
        # crudo —lo que hay en disco— y la base los persiste; sin normalizar en
        # un unico punto, la misma comparacion daria distinto segun de donde
        # viniera cada lado. Un valor vacio se conserva vacio: es la marca de
        # una fila heredada, y etiquetarla afirmaria un checksum inexistente.
        object.__setattr__(self, "checksums",
                           {ruta: (etiquetar_checksum(digest) if digest else "")
                            for ruta, digest in dict(self.checksums).items()})

    @property
    def disponible(self) -> bool:
        return self.estado == ESTADO_DISPONIBLE


def reutilizable(registro: RegistroArtefacto, linaje_actual: LinajeArtefacto,
                 presentes: Mapping[str, str]) -> bool:
    """`True` si el artefacto registrado sigue sirviendo para el trabajo actual.

    Las tres condiciones se comprueban juntas porque cada una tapa un agujero
    distinto:

    - **estado**: una reconciliacion ya lo declaro reconstruible; da igual lo
      que digan los bytes, alguien decidio que no vale;
    - **linaje**: mismos bytes producidos por otra etapa, otra version de
      contrato o de proveedor no son el mismo artefacto;
    - **bytes**: el sidecar puede estar intacto sobre archivos alterados o
      ausentes, que es exactamente el caso que la reutilizacion no debe aceptar.
    """
    if not registro.disponible:
        return False
    if registro.linaje != linaje_actual:
        return False
    if set(registro.rutas) != set(presentes):
        return False
    for ruta in registro.rutas:
        esperado = registro.checksums.get(ruta)
        if not esperado or etiquetar_checksum(presentes[ruta]) != etiquetar_checksum(esperado):
            return False
    return True


def rutas_declaradas(manifiesto: ManifiestoArtefacto) -> Sequence[str]:
    return tuple(archivo.ruta for archivo in manifiesto.archivos)
