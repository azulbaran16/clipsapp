"""Score explicable: componentes con sus valores y razones localizables.

El Core Flow lo dice sin margen: *el score nunca aparece sin explicacion*, y los
motivos son "reaccion fuerte", "cambio de escena", no una frase generada. De ahi
las dos formas que viven aqui:

- `ComponenteScore` es la aritmetica hecha visible. Guarda el valor **crudo** en
  dB, la referencia contra la que se midio, la escala con la que se normalizo,
  el resultado normalizado y el peso. Con esos cinco numeros cualquiera puede
  rehacer el aporte a mano; con solo el score final, nadie.
- `Razon` es un **codigo mas datos**, nunca texto. La UI traduce el codigo y
  formatea los datos en el idioma de la persona. Una frase generada aqui seria
  intraducible, no comprobable y quedaria congelada en los artefactos
  publicados.

La consecuencia practica es que anadir una senal —transcripcion, sujeto— no
cambia esta forma: aporta otro componente y otra razon con su propio codigo.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import math

from .errors import ErrorAnalisis


#: Codigos de razon. Son contrato observable: la UI los traduce y las pruebas
#: los comparan; ninguna capa interpreta el texto que los acompana.
RAZON_PICO_SONORO = "analisis.razon.pico_sonoro"
RAZON_ENERGIA_SOSTENIDA = "analisis.razon.energia_sostenida"
RAZON_CONTRASTE_PREVIO = "analisis.razon.contraste_previo"
RAZON_SENAL_OPCIONAL = "analisis.razon.senal_opcional"
#: Advertencia, no motivo: el candidato se propone igual, pero la UI debe poder
#: marcarlo como de baja confianza tal y como pide el Flujo 4.
RAZON_BAJA_CONFIANZA = "analisis.razon.baja_confianza"
#: El candidato se propuso sin una senal que si estaba pesada en la
#: configuracion. Es lo que hace visible un enriquecimiento ausente o fallido en
#: lugar de dejar el score mas bajo sin explicacion.
RAZON_SENAL_AUSENTE = "analisis.razon.senal_ausente"

#: Valor normalizado a partir del cual una senal se considera contribuyente y
#: merece razon propia. Por debajo aporto algo, pero decir que "destaca" seria
#: falso y llenaria la tarjeta de motivos irrelevantes.
UMBRAL_RAZON = 0.25
#: Score normalizado por debajo del cual un candidato se marca de baja
#: confianza aunque supere el umbral de propuesta.
UMBRAL_CONFIANZA = 0.35


def _redondear(valor: float, decimales: int = 4) -> float:
    """Redondeo canonico de todo lo que se publica.

    Los scores viajan a un artefacto que se hashea y a un snapshot que se
    compara. Sin un redondeo unico, el ultimo bit de un `float` haria fallar la
    comparacion en una maquina y no en otra sin que nada hubiera cambiado.
    """
    return round(float(valor), decimales)


@dataclass(frozen=True)
class ComponenteScore:
    """Aporte de una senal, con todo lo necesario para rehacerlo a mano."""

    senal: str
    #: Medida cruda de la senal en su propia unidad (dB sobre la referencia para
    #: las senales de audio, fraccion 0..1 para una senal externa ya normalizada).
    valor: float
    referencia: float
    escala: float
    normalizado: float
    peso: float
    unidad: str = "dB"

    def __post_init__(self) -> None:
        if not isinstance(self.senal, str) or not self.senal:
            raise ErrorAnalisis("Un componente del score necesita el nombre de su senal.")
        for campo in ("valor", "referencia", "escala", "normalizado", "peso"):
            numero = getattr(self, campo)
            if isinstance(numero, bool) or not isinstance(numero, (int, float)) \
                    or not math.isfinite(float(numero)):
                raise ErrorAnalisis(f"El componente {self.senal!r} declara un '{campo}' invalido.")
            object.__setattr__(self, campo, _redondear(numero))
        if not 0.0 <= self.normalizado <= 1.0:
            raise ErrorAnalisis(
                f"El componente {self.senal!r} declara un valor normalizado fuera de [0, 1].")

    @property
    def aporte(self) -> float:
        return _redondear(self.peso * self.normalizado)

    def como_documento(self) -> dict[str, object]:
        return {"senal": self.senal, "valor": self.valor, "unidad": self.unidad,
                "referencia": self.referencia, "escala": self.escala,
                "normalizado": self.normalizado, "peso": self.peso, "aporte": self.aporte}


@dataclass(frozen=True)
class Razon:
    """Motivo localizable: un codigo y sus datos. Nunca una frase."""

    codigo: str
    datos: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.codigo, str) or not self.codigo:
            raise ErrorAnalisis("Una razon necesita su codigo.")
        datos = {}
        for clave, valor in dict(self.datos).items():
            if not isinstance(clave, str) or not clave:
                raise ErrorAnalisis(f"La razon {self.codigo!r} declara un dato sin nombre.")
            datos[clave] = (_redondear(valor)
                            if isinstance(valor, float) and math.isfinite(valor) else valor)
        object.__setattr__(self, "datos", datos)

    def como_documento(self) -> dict[str, object]:
        return {"codigo": self.codigo, "datos": dict(sorted(self.datos.items()))}


#: Que razon explica cada senal basica. Vive en una tabla y no en una cadena de
#: `if` para que anadir una senal sea anadir una fila.
RAZONES_POR_SENAL = {
    "pico": RAZON_PICO_SONORO,
    "sostenido": RAZON_ENERGIA_SOSTENIDA,
    "contraste": RAZON_CONTRASTE_PREVIO,
}


def score_total(componentes: Sequence[ComponenteScore]) -> float:
    """Media ponderada de los aportes, normalizada por los pesos presentes.

    Se divide entre la suma de los pesos **presentes** y no entre la de los
    configurados. Es la decision que mantiene comparables los scores de un
    proyecto analizado con transcripcion y otro sin ella: si el divisor
    incluyese senales ausentes, el segundo proyecto tendria todos sus candidatos
    artificialmente peores y el umbral significaria cosas distintas en cada uno.
    La ausencia se cuenta, pero como razon visible, no como castigo silencioso.
    """
    if not componentes:
        return 0.0
    pesos = sum(componente.peso for componente in componentes)
    if pesos <= 0:
        return 0.0
    return _redondear(sum(componente.aporte for componente in componentes) / pesos)


def razones_de(componentes: Sequence[ComponenteScore], score: float,
               ausentes: Sequence[str] = (), umbral: float = UMBRAL_RAZON,
               umbral_confianza: float = UMBRAL_CONFIANZA) -> tuple[Razon, ...]:
    """Motivos de un candidato, en orden de aporte descendente.

    El orden no es cosmetico: la UI muestra los primeros y el ticket exige que
    lo que se lea primero sea lo que mas peso tuvo.
    """
    contribuyentes = [componente for componente in componentes
                      if componente.normalizado >= umbral]
    contribuyentes.sort(key=lambda componente: (-componente.aporte, componente.senal))
    razones = [
        Razon(RAZONES_POR_SENAL.get(componente.senal, RAZON_SENAL_OPCIONAL),
              {"senal": componente.senal, "valor": componente.valor,
               "unidad": componente.unidad, "normalizado": componente.normalizado})
        for componente in contribuyentes
    ]
    razones.extend(Razon(RAZON_SENAL_AUSENTE, {"senal": senal}) for senal in sorted(ausentes))
    if score < umbral_confianza:
        razones.append(Razon(RAZON_BAJA_CONFIANZA, {"score": _redondear(score)}))
    return tuple(razones)
