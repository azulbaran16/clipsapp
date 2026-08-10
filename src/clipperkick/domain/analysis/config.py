"""Configuracion del analisis, partida por etapa a proposito.

El ticket pide una garantia que no es de rendimiento sino de diseno: *cambiar
cantidad, duracion o pesos rerunea el ranking, no el sondeo ni la extraccion de
audio*. Esa garantia no se puede conseguir con una cache mas lista, porque la
clave de materializacion se deriva de la configuracion: si `cantidad` viaja
dentro de la configuracion de la extraccion, subirla de 6 a 8 cambia la clave
del audio extraido y vuelve a decodificar el VOD entero. No hay optimizacion
posterior que lo arregle.

Por eso hay tres configuraciones y no una, y cada una entra unicamente en la
clave de su etapa:

| Configuracion | Gobierna | Cambiarla invalida |
| --- | --- | --- |
| `ConfiguracionExtraccion` | como se decodifica el audio | extraccion, rasgos y ranking |
| `ConfiguracionRasgos` | como se mide la sonoridad | rasgos y ranking |
| `ConfiguracionRanking` | cuantos, cuanto duran y que pesa | solo el ranking |

La cascada es la correcta: lo que cambia el audio cambia todo lo derivado, y lo
que solo cambia la seleccion no toca nada de lo medido.

Cada configuracion publica un `como_documento()` canonico. Se construye a mano
—no se deriva del dataclass— por la misma razon que `MetadataMedia`: anadir un
campo interno no puede cambiar en silencio la clave de materializacion de todos
los proyectos existentes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math

from .errors import ErrorConfiguracionAnalisis


#: Version del contrato de configuracion. Viaja en cada documento y en la clave:
#: cambiar el significado de un campo obliga a moverla.
VERSION_CONFIG = "clipsapp-analisis-config/1"

#: Senales que el nucleo sabe calcular solo con audio, en CPU y sin red. Los
#: nombres son parte del contrato observable: la UI los traduce y los pesos se
#: declaran contra ellos.
SENAL_PICO = "pico"
SENAL_SOSTENIDO = "sostenido"
SENAL_CONTRASTE = "contraste"
SENALES_BASICAS = (SENAL_PICO, SENAL_SOSTENIDO, SENAL_CONTRASTE)

#: Pesos por defecto del perfil basico. `pico` manda porque es la senal que
#: mejor localiza una reaccion; `sostenido` evita que un golpe de un segundo
#: gane a treinta segundos de energia; `contraste` premia que el momento
#: *empiece* en algo mas silencioso, que es lo que distingue una reaccion de un
#: tramo permanentemente ruidoso.
PESOS_BASICOS = {SENAL_PICO: 1.0, SENAL_SOSTENIDO: 0.6, SENAL_CONTRASTE: 0.4}

#: Margen de borde del VOD por defecto: el menor entre medio minuto y la decima
#: parte del material. Es la regla que el flujo actual ya aplica en muestras.
MARGEN_BORDES_MAXIMO = 30.0
FRACCION_BORDES = 0.1


def _real_positivo(valor: object, nombre: str, *, permitir_cero: bool = False) -> float:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)):
        raise ErrorConfiguracionAnalisis(f"La configuracion necesita un numero en '{nombre}'.")
    numero = float(valor)
    if not math.isfinite(numero):
        raise ErrorConfiguracionAnalisis(f"La configuracion declara un '{nombre}' no finito.")
    if numero < 0 or (numero == 0 and not permitir_cero):
        raise ErrorConfiguracionAnalisis(f"La configuracion declara un '{nombre}' no positivo.")
    return numero


def _entero_positivo(valor: object, nombre: str) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int) or valor <= 0:
        raise ErrorConfiguracionAnalisis(
            f"La configuracion necesita un entero positivo en '{nombre}'.")
    return valor


@dataclass(frozen=True)
class ConfiguracionExtraccion:
    """Como se decodifica el audio de la fuente antes de medir nada.

    El resultado es una pista mono normalizada dentro del proyecto. Existe como
    etapa propia —en vez de medir sobre el VOD— porque el ticket exige que
    rerankear no vuelva a tocar el audio, y porque las capacidades futuras
    (transcripcion, diarizacion) consumen exactamente esta misma pista: sin ella
    cada una decodificaria el VOD por su cuenta.
    """

    tasa_muestreo: int = 16000
    canales: int = 1
    codec: str = "pcm_s16le"

    def __post_init__(self) -> None:
        _entero_positivo(self.tasa_muestreo, "tasa_muestreo")
        _entero_positivo(self.canales, "canales")
        if not isinstance(self.codec, str) or not self.codec:
            raise ErrorConfiguracionAnalisis("La extraccion de audio necesita declarar su codec.")

    def como_documento(self) -> dict[str, object]:
        return {"version": VERSION_CONFIG, "tasa_muestreo": self.tasa_muestreo,
                "canales": self.canales, "codec": self.codec}


@dataclass(frozen=True)
class ConfiguracionRasgos:
    """Como se convierte la pista en una serie de sonoridad comparable."""

    #: Duracion de cada muestra de la serie. Un segundo es lo que el flujo
    #: actual produce y lo que hace legible una razon: "a los 1:42".
    paso_segundos: float = 1.0
    #: Sonoridad atribuida a un tramo sin medida. No es un valor inventado: es
    #: el suelo declarado del contrato, y viaja en el documento para que quien
    #: lea la serie sepa distinguir "silencio" de "no medido".
    piso_lufs: float = -70.0
    #: Suavizado en muestras. Impar para que la ventana quede centrada; un
    #: valor par desplazaria medio paso todos los picos.
    ventana_suavizado: int = 7

    def __post_init__(self) -> None:
        _real_positivo(self.paso_segundos, "paso_segundos")
        if isinstance(self.piso_lufs, bool) or not isinstance(self.piso_lufs, (int, float)):
            raise ErrorConfiguracionAnalisis("La configuracion necesita un 'piso_lufs' numerico.")
        if not math.isfinite(float(self.piso_lufs)):
            raise ErrorConfiguracionAnalisis("La configuracion declara un 'piso_lufs' no finito.")
        _entero_positivo(self.ventana_suavizado, "ventana_suavizado")
        if self.ventana_suavizado % 2 == 0:
            raise ErrorConfiguracionAnalisis(
                "La ventana de suavizado debe ser impar para quedar centrada.")

    def como_documento(self) -> dict[str, object]:
        return {"version": VERSION_CONFIG, "paso_segundos": self.paso_segundos,
                "piso_lufs": float(self.piso_lufs), "ventana_suavizado": self.ventana_suavizado}


@dataclass(frozen=True)
class ConfiguracionRanking:
    """Cuantos candidatos, cuanto duran, que pesa y que se descarta.

    Es la unica configuracion que la persona toca en el Flujo 3, y por eso es la
    unica que puede cambiar sin coste: todo lo que declara se aplica sobre una
    serie ya medida.
    """

    cantidad: int = 6
    duracion_segundos: float = 45.0
    #: Donde queda el pico dentro del clip. 0.4 reproduce el encuadre del flujo
    #: actual: algo de contexto antes de la reaccion y el resto despues.
    adelanto: float = 0.4
    pesos: Mapping[str, float] = field(default_factory=lambda: dict(PESOS_BASICOS))
    #: Separacion minima entre candidatos. `None` la deriva de la duracion, que
    #: es lo que hace estructuralmente imposible que dos rangos se solapen.
    separacion_segundos: float | None = None
    #: Borde del VOD que no produce candidatos. `None` aplica la regla por
    #: defecto: el menor entre 30 s y la decima parte del material.
    margen_bordes_segundos: float | None = None
    #: Score normalizado por debajo del cual un candidato no se propone. Es lo
    #: que convierte un audio plano en "sin candidatos suficientes" en vez de en
    #: seis recortes arbitrarios.
    umbral_score: float = 0.15
    #: Cuanto se mira hacia atras para medir el contraste de entrada.
    ventana_contraste_segundos: float = 30.0
    #: Dispersion minima admitida al normalizar. Sin ella, una serie casi plana
    #: dividiria por un numero diminuto y convertiria el ruido de medida en
    #: picos con score 1.0.
    escala_minima_db: float = 3.0
    #: Percentil que fija el techo de la escala de normalizacion.
    percentil_escala: float = 0.95

    def __post_init__(self) -> None:
        _entero_positivo(self.cantidad, "cantidad")
        _real_positivo(self.duracion_segundos, "duracion_segundos")
        _real_positivo(self.ventana_contraste_segundos, "ventana_contraste_segundos")
        _real_positivo(self.escala_minima_db, "escala_minima_db")
        if not isinstance(self.adelanto, (int, float)) or isinstance(self.adelanto, bool) \
                or not 0.0 <= float(self.adelanto) <= 1.0:
            raise ErrorConfiguracionAnalisis("El adelanto del pico debe estar entre 0 y 1.")
        if not isinstance(self.umbral_score, (int, float)) or isinstance(self.umbral_score, bool) \
                or not 0.0 <= float(self.umbral_score) <= 1.0:
            raise ErrorConfiguracionAnalisis("El umbral de score debe estar entre 0 y 1.")
        if not isinstance(self.percentil_escala, (int, float)) \
                or isinstance(self.percentil_escala, bool) \
                or not 0.0 < float(self.percentil_escala) <= 1.0:
            raise ErrorConfiguracionAnalisis("El percentil de escala debe estar entre 0 y 1.")
        if self.separacion_segundos is not None:
            _real_positivo(self.separacion_segundos, "separacion_segundos", permitir_cero=True)
            if float(self.separacion_segundos) < float(self.duracion_segundos):
                raise ErrorConfiguracionAnalisis(
                    "La separacion entre candidatos no puede ser menor que su duracion.")
        if self.margen_bordes_segundos is not None:
            _real_positivo(self.margen_bordes_segundos, "margen_bordes_segundos",
                           permitir_cero=True)
        object.__setattr__(self, "pesos", self._pesos_validados())

    def _pesos_validados(self) -> dict[str, float]:
        if not isinstance(self.pesos, Mapping) or not self.pesos:
            raise ErrorConfiguracionAnalisis("El ranking necesita al menos una senal con peso.")
        normalizados: dict[str, float] = {}
        for nombre, peso in self.pesos.items():
            if not isinstance(nombre, str) or not nombre:
                raise ErrorConfiguracionAnalisis("Una senal declara un nombre vacio.")
            normalizados[nombre] = _real_positivo(peso, f"peso de {nombre}", permitir_cero=True)
        if sum(normalizados.values()) <= 0:
            raise ErrorConfiguracionAnalisis(
                "Los pesos suman cero: ningun candidato podria explicarse.")
        return normalizados

    # ------------------------------------------------------------------ #

    def separacion_efectiva(self) -> float:
        """Separacion realmente aplicada, ya resuelto el valor por defecto.

        Por defecto es la duracion del clip: con esa distancia entre picos, dos
        rangos construidos con el mismo adelanto no pueden solaparse. No es una
        heuristica, es la condicion que hace cierto el "sin solapamientos".
        """
        return (self.duracion_segundos if self.separacion_segundos is None
                else float(self.separacion_segundos))

    def margen_efectivo(self, duracion_total: float) -> float:
        if self.margen_bordes_segundos is not None:
            return float(self.margen_bordes_segundos)
        return min(MARGEN_BORDES_MAXIMO, max(0.0, duracion_total) * FRACCION_BORDES)

    def peso_de(self, senal: str) -> float:
        return float(self.pesos.get(senal, 0.0))

    def como_documento(self) -> dict[str, object]:
        return {
            "version": VERSION_CONFIG,
            "cantidad": self.cantidad,
            "duracion_segundos": self.duracion_segundos,
            "adelanto": float(self.adelanto),
            "pesos": dict(sorted(self.pesos.items())),
            "separacion_segundos": self.separacion_segundos,
            "margen_bordes_segundos": self.margen_bordes_segundos,
            "umbral_score": float(self.umbral_score),
            "ventana_contraste_segundos": self.ventana_contraste_segundos,
            "escala_minima_db": self.escala_minima_db,
            "percentil_escala": float(self.percentil_escala),
        }
