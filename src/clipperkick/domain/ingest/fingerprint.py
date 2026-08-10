"""Huellas de fuente: que se lee, como se compone y cuando hay reemplazo.

La regla del plan es corta —*una fuente externa conserva ruta normalizada,
tamano, mtime y huella parcial/completa para detectar reemplazos*— pero tiene
dos consumidores con exigencias opuestas:

| Huella | Quien la usa | Que necesita |
| --- | --- | --- |
| parcial | reconciliacion al abrir | ser barata sobre archivos de gigabytes |
| completa | materializacion y cache | no admitir dos contenidos distintos |

Por eso son dos y no una. La parcial muestrea ventanas fijas —cabeza, centro y
cola— y la completa recorre el archivo entero. Una fuente reemplazada
conservando nombre y ruta se detecta con la parcial en el caso normal (el
tamano cambia, o cambian los bytes muestreados) y **siempre** con la completa,
que es la que decide si un artefacto puede reutilizarse.

El *plan* de lectura vive aqui, en el dominio, y no en el adaptador que lee el
archivo: si el adaptador eligiera las ventanas, dos adaptadores —o dos
versiones del mismo— producirian huellas distintas para el mismo archivo y la
reutilizacion dejaria de ser reproducible sin que nada pareciese roto.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import hashlib

from .errors import ErrorHuellaIngesta


#: Version del esquema de huella. Cambiarla invalida las comparaciones con las
#: huellas ya guardadas, de modo que solo se mueve cuando el plan o la
#: composicion cambian de verdad.
VERSION_HUELLA = "clipsapp-huella/1"
ALGORITMO = "sha256"

#: Ventana de muestreo de la huella parcial. Un mebibyte es suficiente para
#: cubrir cabecera, indice y cola de los contenedores habituales, y barato de
#: leer incluso sobre un sistema de archivos montado.
TAMANO_VENTANA = 1 << 20

#: Bloque de lectura para la huella completa.
BLOQUE_LECTURA = 1 << 20


@dataclass(frozen=True)
class Ventana:
    """Tramo de archivo que la huella parcial muestrea."""

    desplazamiento: int
    longitud: int

    def __post_init__(self) -> None:
        for campo in ("desplazamiento", "longitud"):
            valor = getattr(self, campo)
            if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
                raise ErrorHuellaIngesta(f"El {campo} de una ventana debe ser un entero no negativo.")

    @property
    def fin(self) -> int:
        return self.desplazamiento + self.longitud


def digest_bloque(datos: bytes) -> str:
    """Digest de un bloque. El algoritmo lo elige el dominio, no el adaptador."""
    return hashlib.sha256(datos).hexdigest()


def nuevo_acumulador():
    """Acumulador incremental del mismo algoritmo que `digest_bloque`.

    Existe para que el adaptador que lee un archivo de gigabytes no tenga que
    elegir el algoritmo por su cuenta —eso lo convertiria en una segunda
    autoridad sobre la forma de la huella— ni cargar el contenido entero en
    memoria para pasarlo por `digest_bloque`.
    """
    return hashlib.sha256()


def plan_parcial(tamano: int, ventana: int = TAMANO_VENTANA) -> tuple[Ventana, ...]:
    """Ventanas a muestrear para un archivo de `tamano` bytes.

    Devuelve ventanas ordenadas, disjuntas y contenidas en el archivo. Para un
    archivo que cabe en una sola ventana devuelve exactamente una que lo cubre
    entero: no tiene sentido muestrear tres veces lo mismo, y ademas hace que la
    parcial de un archivo pequeno sea tan concluyente como la completa.
    """
    if isinstance(tamano, bool) or not isinstance(tamano, int) or tamano < 0:
        raise ErrorHuellaIngesta("El tamano de la fuente debe ser un entero no negativo.")
    if isinstance(ventana, bool) or not isinstance(ventana, int) or ventana <= 0:
        raise ErrorHuellaIngesta("La ventana de muestreo debe ser un entero positivo.")
    if tamano == 0:
        return ()
    if tamano <= ventana:
        return (Ventana(0, tamano),)
    candidatas = [
        Ventana(0, ventana),
        Ventana(max(0, (tamano - ventana) // 2), ventana),
        Ventana(tamano - ventana, ventana),
    ]
    # Se fusionan los solapes en vez de recortarlos: dos ventanas que se pisan
    # harian que unos bytes contasen dos veces y que el plan dependiese del
    # orden en que se recorren, que es justo lo que impide reproducirlo.
    fusionadas: list[Ventana] = []
    for candidata in sorted(candidatas, key=lambda v: v.desplazamiento):
        if fusionadas and candidata.desplazamiento <= fusionadas[-1].fin:
            anterior = fusionadas[-1]
            fin = max(anterior.fin, candidata.fin)
            fusionadas[-1] = Ventana(anterior.desplazamiento, fin - anterior.desplazamiento)
        else:
            fusionadas.append(candidata)
    return tuple(fusionadas)


def componer_parcial(tamano: int, ventanas: Sequence[Ventana],
                     digests: Sequence[str]) -> str:
    """Compone la huella parcial a partir de los digests de cada ventana.

    El tamano entra en la composicion porque es la senal mas barata de que un
    archivo cambio, y porque sin el dos archivos con las mismas ventanas
    muestreadas —uno truncado del otro por el centro— colisionarian.
    """
    if len(ventanas) != len(digests):
        raise ErrorHuellaIngesta("El plan de muestreo y sus digests no se corresponden.")
    piezas = [VERSION_HUELLA, ALGORITMO, "parcial", str(tamano)]
    for ventana, digest in zip(ventanas, digests):
        if not isinstance(digest, str) or not digest:
            raise ErrorHuellaIngesta("Una ventana no produjo digest.")
        piezas.append(f"{ventana.desplazamiento}:{ventana.longitud}:{digest}")
    return hashlib.sha256("|".join(piezas).encode("utf-8")).hexdigest()


def componer_completa(tamano: int, digest: str) -> str:
    """Compone la huella completa a partir del digest del contenido entero."""
    if not isinstance(digest, str) or not digest:
        raise ErrorHuellaIngesta("El contenido no produjo digest.")
    piezas = (VERSION_HUELLA, ALGORITMO, "completa", str(tamano), digest)
    return hashlib.sha256("|".join(piezas).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class HuellaFuente:
    """Identidad de contenido de una fuente, con su coste de calculo declarado."""

    tamano: int
    parcial: str
    completa: str | None = None
    mtime_ns: int | None = None
    version: str = VERSION_HUELLA

    def __post_init__(self) -> None:
        if isinstance(self.tamano, bool) or not isinstance(self.tamano, int) or self.tamano < 0:
            raise ErrorHuellaIngesta("El tamano de la huella debe ser un entero no negativo.")
        if not isinstance(self.parcial, str) or not self.parcial:
            raise ErrorHuellaIngesta("Una huella necesita al menos su parte parcial.")
        if self.completa is not None and (not isinstance(self.completa, str) or not self.completa):
            raise ErrorHuellaIngesta("La huella completa declarada esta vacia.")
        if self.mtime_ns is not None and (isinstance(self.mtime_ns, bool)
                                          or not isinstance(self.mtime_ns, int)):
            raise ErrorHuellaIngesta("El mtime de la huella debe ser un entero de nanosegundos.")
        if not isinstance(self.version, str) or not self.version:
            raise ErrorHuellaIngesta("Una huella declara la version de su esquema.")

    @property
    def identidad(self) -> str:
        """Clave con la que la fuente se registra y se busca.

        Es la huella **completa** a proposito. La parcial sirve para descartar
        rapido, no para afirmar identidad: dos archivos distintos pueden
        compartir tamano y ventanas muestreadas, y registrar uno como el otro
        haria que sus artefactos se cruzasen en silencio.
        """
        if self.completa is None:
            raise ErrorHuellaIngesta(
                "La fuente todavia no tiene huella completa; no puede identificarse.")
        return self.completa

    def reconciliable_con(self, otra: "HuellaFuente") -> bool:
        """Comparacion barata: misma version, mismo tamano y misma parcial."""
        return (self.version == otra.version and self.tamano == otra.tamano
                and self.parcial == otra.parcial)

    def equivalente_a(self, otra: "HuellaFuente") -> bool:
        """Igualdad concluyente. Sin ambas huellas completas, no la afirma."""
        if not self.reconciliable_con(otra):
            return False
        if self.completa is None or otra.completa is None:
            return False
        return self.completa == otra.completa

    def como_documento(self) -> dict[str, object]:
        return {"version": self.version, "algoritmo": ALGORITMO, "tamano": self.tamano,
                "parcial": self.parcial, "completa": self.completa}


def detecta_reemplazo(anterior: HuellaFuente, actual: HuellaFuente) -> bool:
    """`True` si la fuente registrada ya no es la que hay en disco.

    Con ambas huellas completas la respuesta es exacta. Sin ellas se decide por
    la parcial, que puede dar un falso *negativo* sobre archivos enormes con un
    cambio fuera de las ventanas: por eso la materializacion nunca se apoya en
    la parcial, solo la reconciliacion, donde equivocarse solo cuesta un sondeo.
    """
    if anterior.version != actual.version:
        return True
    if anterior.completa is not None and actual.completa is not None:
        return anterior.completa != actual.completa
    return not anterior.reconciliable_con(actual)
