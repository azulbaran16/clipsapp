"""Serie de sonoridad: el artefacto versionado del que vive el ranking.

Es la pieza que hace posible la promesa del ticket. Rerankear sin volver a tocar
el audio solo es posible si lo medido se puede **guardar, releer y verificar**
sin el archivo original, y eso exige que la serie sea un valor cerrado: sus
muestras, su paso, su suelo declarado y su version.

Dos reglas gobiernan el archivo:

1. **Un tramo no medido no es un silencio.** Se rellena con el piso declarado y
   el documento dice cual es, de modo que quien lo lea distinga "aqui no habia
   nada" de "aqui no se midio". Inventar ceros haria que un hueco de la medicion
   pareciese el momento mas silencioso del VOD.
2. **Las sumas acumuladas son parte del modelo, no una optimizacion.** El
   ranking necesita la media de miles de ventanas solapadas; calcularlas una a
   una convertiria un VOD de tres horas en un bucle cuadratico. Vivir aqui,
   junto a los datos, es lo que permite que `ranking.py` sea legible.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import math

from .errors import ErrorSerieAnalisis


#: Version del contrato de la serie. Entra en la clave de materializacion del
#: ranking: cambiar la forma del documento invalida los rankings publicados
#: aunque el audio y la medicion sean los mismos.
VERSION_SERIE = "clipsapp-sonoridad/1"


def _real(valor: object, nombre: str) -> float:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise ErrorSerieAnalisis(f"La serie declara un '{nombre}' que no es un numero.")
    numero = float(valor)
    if not math.isfinite(numero):
        raise ErrorSerieAnalisis(f"La serie declara un '{nombre}' no finito.")
    return numero


@dataclass(frozen=True)
class SerieSonoridad:
    """Sonoridad momentanea por muestra, con su paso y su suelo declarados."""

    valores: tuple[float, ...]
    paso_segundos: float = 1.0
    inicio_segundos: float = 0.0
    piso: float = -70.0
    version: str = VERSION_SERIE
    #: Sumas acumuladas: `acumuladas[i]` es la suma de las `i` primeras muestras.
    acumuladas: tuple[float, ...] = field(default=(), repr=False, compare=False)

    def __post_init__(self) -> None:
        valores = tuple(_real(valor, "valor") for valor in self.valores)
        object.__setattr__(self, "valores", valores)
        paso = _real(self.paso_segundos, "paso_segundos")
        if paso <= 0:
            raise ErrorSerieAnalisis("La serie declara un paso que no avanza.")
        object.__setattr__(self, "paso_segundos", paso)
        object.__setattr__(self, "inicio_segundos", _real(self.inicio_segundos, "inicio_segundos"))
        object.__setattr__(self, "piso", _real(self.piso, "piso"))
        if not isinstance(self.version, str) or not self.version:
            raise ErrorSerieAnalisis("La serie necesita declarar su version de contrato.")
        acumuladas = [0.0]
        for valor in valores:
            acumuladas.append(acumuladas[-1] + valor)
        object.__setattr__(self, "acumuladas", tuple(acumuladas))

    # ------------------------------------------------------------------ #

    def __len__(self) -> int:
        return len(self.valores)

    @property
    def vacia(self) -> bool:
        return not self.valores

    @property
    def duracion_segundos(self) -> float:
        return len(self.valores) * self.paso_segundos

    def segundo_de(self, indice: int) -> float:
        """Instante del centro conceptual de una muestra: su inicio."""
        return self.inicio_segundos + indice * self.paso_segundos

    def indice_de(self, segundo: float) -> int:
        """Muestra que contiene ese instante, saturada a los extremos."""
        if self.vacia:
            return 0
        crudo = int(math.floor((segundo - self.inicio_segundos) / self.paso_segundos))
        return max(0, min(len(self.valores) - 1, crudo))

    def muestras_de(self, segundos: float) -> int:
        """Cuantas muestras cubren esa duracion. Al menos una si es positiva."""
        if segundos <= 0:
            return 0
        return max(1, int(round(segundos / self.paso_segundos)))

    def muestras_cubriendo(self, segundos: float) -> int:
        """Muestras que cubren esa duracion **entera**, redondeando hacia arriba.

        La distincion con `muestras_de` no es un matiz: la separacion entre
        candidatos se mide con esta, porque redondear hacia abajo dejaria dos
        clips a menos distancia que su propia duracion y el "sin solapamientos"
        dejaria de ser cierto por unas decimas. La tolerancia absorbe el error
        de coma flotante de una division exacta.
        """
        if segundos <= 0:
            return 0
        return max(1, int(math.ceil(segundos / self.paso_segundos - 1e-9)))

    def media(self, desde: int, hasta: int) -> float | None:
        """Media de `[desde, hasta)`, o `None` si el tramo esta vacio.

        Devolver `None` y no el piso es deliberado: una ventana vacia significa
        "no hay con que comparar" —el contraste de entrada del primer segundo
        del VOD, por ejemplo— y sustituirla por el piso inventaria un contraste
        enorme justo donde menos evidencia hay.
        """
        inicio = max(0, desde)
        fin = min(len(self.valores), hasta)
        if fin <= inicio:
            return None
        return (self.acumuladas[fin] - self.acumuladas[inicio]) / (fin - inicio)

    # ------------------------------------------------------------------ #

    def como_documento(self) -> dict[str, object]:
        """Forma canonica que se publica y se hashea."""
        return {"version": self.version, "paso_segundos": self.paso_segundos,
                "inicio_segundos": self.inicio_segundos, "piso": self.piso,
                "valores": [float(valor) for valor in self.valores]}


def serie_desde_documento(documento: Mapping[str, object]) -> SerieSonoridad:
    """Reconstruye la serie de un artefacto publicado.

    Nada del documento es entrada confiable: viene de un archivo que pudo
    escribir otra version. Un campo ilegible es un error tipado y no un valor por
    defecto, porque una serie con un paso inventado produciria rangos que no se
    corresponden con el VOD.
    """
    if not isinstance(documento, Mapping):
        raise ErrorSerieAnalisis("El artefacto de rasgos no contiene un documento de serie.")
    valores = documento.get("valores")
    if not isinstance(valores, Sequence) or isinstance(valores, (str, bytes)):
        raise ErrorSerieAnalisis("El artefacto de rasgos no declara sus valores.")
    version = documento.get("version")
    if not isinstance(version, str) or not version:
        raise ErrorSerieAnalisis("El artefacto de rasgos no declara su version de contrato.")
    return SerieSonoridad(
        valores=tuple(_real(valor, "valor") for valor in valores),
        paso_segundos=_real(documento.get("paso_segundos", 1.0), "paso_segundos"),
        inicio_segundos=_real(documento.get("inicio_segundos", 0.0), "inicio_segundos"),
        piso=_real(documento.get("piso", -70.0), "piso"),
        version=version)


# --------------------------------------------------------------------------- #
# Normalizacion de una medicion cruda
# --------------------------------------------------------------------------- #

#: Version del contrato de medicion. Es el equivalente de `VERSION_METADATA` en
#: la ingesta: cambiar como se agrupan las muestras cambia la serie aunque el
#: audio y el medidor sean los mismos, y por eso viaja en la clave.
VERSION_MEDICION = "clipsapp-medicion/1"


def medicion_canonica(crudo: Mapping[str, object]) -> dict[str, object]:
    """Forma canonica de la medicion cruda del proveedor.

    Se publica junto a la serie normalizada por la misma razon que la ingesta
    guarda el sondeo de ffprobe: la normalizacion es una regla versionada, y
    conservar su entrada permite rederivarla el dia que la regla cambie sin
    volver a decodificar el audio.
    """
    if not isinstance(crudo, Mapping):
        raise ErrorSerieAnalisis("La medicion de sonoridad no devolvio un documento.")
    muestras = crudo.get("muestras")
    if not isinstance(muestras, Sequence) or isinstance(muestras, (str, bytes)):
        raise ErrorSerieAnalisis("La medicion de sonoridad no declara sus muestras.")
    canonicas: list[list[float]] = []
    for muestra in muestras:
        if not isinstance(muestra, Sequence) or isinstance(muestra, (str, bytes)) \
                or len(muestra) != 2:
            raise ErrorSerieAnalisis(
                "Una muestra de sonoridad no es una pareja (segundo, valor).")
        canonicas.append([_real(muestra[0], "segundo"), _real(muestra[1], "sonoridad")])
    canonicas.sort(key=lambda muestra: muestra[0])
    documento: dict[str, object] = {"version": VERSION_MEDICION, "muestras": canonicas,
                                    "unidad": str(crudo.get("unidad") or "LUFS")}
    duracion = crudo.get("duracion_segundos")
    if duracion is not None:
        documento["duracion_segundos"] = _real(duracion, "duracion_segundos")
    return documento


def normalizar_medicion(crudo: Mapping[str, object],
                        paso_segundos: float = 1.0, piso: float = -70.0,
                        ventana_suavizado: int = 1) -> SerieSonoridad:
    """Convierte una medicion cruda en la serie versionada del contrato.

    Tres decisiones, las tres heredadas del flujo que ya funciona:

    1. **Se agrupa por paso quedandose con el maximo.** Un promedio dentro del
       paso apagaria justo lo que se busca —el instante mas sonoro— y una
       reaccion breve dejaria de destacar sobre su propio segundo.
    2. **Los huecos se rellenan con el piso declarado**, no con el valor vecino.
       Interpolar inventaria sonido donde el medidor no dijo nada.
    3. **Se suaviza al final y no antes.** Suavizar antes de agrupar mezclaria
       muestras de pasos distintos y el maximo dejaria de ser el de su paso.
    """
    if paso_segundos <= 0:
        raise ErrorSerieAnalisis("La normalizacion necesita un paso que avance.")
    canonica = medicion_canonica(crudo)
    muestras = canonica["muestras"]
    por_paso: dict[int, float] = {}
    for segundo, valor in muestras:  # type: ignore[misc]
        indice = int(math.floor(segundo / paso_segundos))
        if indice < 0:
            continue
        por_paso[indice] = max(valor, por_paso.get(indice, piso), piso)
    if not por_paso:
        return SerieSonoridad(valores=(), paso_segundos=paso_segundos, piso=piso)
    declarada = canonica.get("duracion_segundos")
    cubiertos = max(por_paso) + 1
    if isinstance(declarada, (int, float)) and declarada > 0:
        cubiertos = max(cubiertos, int(math.floor(float(declarada) / paso_segundos)))
    valores = [por_paso.get(indice, piso) for indice in range(cubiertos)]
    return SerieSonoridad(valores=suavizar(valores, ventana_suavizado),
                          paso_segundos=paso_segundos, piso=piso)


# --------------------------------------------------------------------------- #
# Estadistica robusta
# --------------------------------------------------------------------------- #

def mediana(valores: Sequence[float]) -> float:
    """Mediana por seleccion del elemento central del orden.

    Se elige el elemento `n // 2` y no el promedio de los dos centrales: es lo
    que el flujo actual usa como linea base, y promediar movería la referencia
    de todos los proyectos existentes por una diferencia que nadie pidio.
    """
    if not valores:
        raise ErrorSerieAnalisis("Una serie vacia no tiene mediana.")
    return sorted(valores)[len(valores) // 2]


def percentil(valores: Sequence[float], fraccion: float) -> float:
    """Percentil por indice mas cercano, sin interpolar.

    Sin interpolacion el resultado no depende de la aritmetica de coma flotante
    del interprete, y eso importa: este valor entra en la escala de
    normalizacion y por tanto en los scores que se publican.
    """
    if not valores:
        raise ErrorSerieAnalisis("Una serie vacia no tiene percentiles.")
    ordenados = sorted(valores)
    posicion = int(round(fraccion * (len(ordenados) - 1)))
    return ordenados[max(0, min(len(ordenados) - 1, posicion))]


def suavizar(valores: Sequence[float], ventana: int) -> tuple[float, ...]:
    """Media movil centrada, con la ventana recortada en los bordes.

    Recortar —en vez de rellenar con el piso— evita inventar un desvanecimiento
    en los extremos del VOD que despues el sesgo de bordes volveria a castigar.
    """
    if ventana <= 1 or not valores:
        return tuple(float(valor) for valor in valores)
    total = len(valores)
    mitad = ventana // 2
    acumuladas = [0.0]
    for valor in valores:
        acumuladas.append(acumuladas[-1] + float(valor))
    suavizados = []
    for indice in range(total):
        inicio = max(0, indice - mitad)
        fin = min(total, indice + mitad + 1)
        suavizados.append((acumuladas[fin] - acumuladas[inicio]) / (fin - inicio))
    return tuple(suavizados)
