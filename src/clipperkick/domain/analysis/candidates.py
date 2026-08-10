"""El candidato: un rango, un score desglosado y las razones que lo sostienen.

`Moment` es, segun el plan tecnico, "candidato, rango y razones/puntajes" y
"puede sobrevivir a reranking si sus entradas siguen validas". Esa segunda frase
es la que fija su identidad: si el id fuese la posicion en el ranking, subir de
6 a 8 clips renombraria todos los candidatos y cualquier borrador colgado de uno
de ellos apuntaria a otro momento distinto. Si fuese aleatorio, rerankear
duplicaria los mismos treinta segundos con otro nombre.

Por eso el id se deriva del **rango dentro de su fuente**: el mismo tramo del
mismo VOD es el mismo momento, lo proponga la configuracion que lo proponga. Es
lo que permite que el ranking se rehaga sin que el trabajo humano se pierda.

Los milisegundos son la unidad de la identidad porque los segundos en coma
flotante no son estables al ida y vuelta por SQLite y JSON: `12.3` puede volver
como `12.299999999999999` y producir otro id para el mismo tramo.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
import hashlib
import math

from .errors import ErrorMomentoAnalisis
from .scoring import ComponenteScore, Razon


#: Version del contrato del candidato. Viaja en el manifiesto publicado.
VERSION_CANDIDATO = "clipsapp-candidato/1"
PREFIJO_MOMENTO = "mo1"

ESTADO_CANDIDATO = "candidate"
ESTADO_DESCARTADO = "discarded"
ESTADO_FAVORITO = "favorite"
#: Los descartes son reversibles (Flujo 4): por eso son estados de la fila y no
#: un borrado. Una fila borrada no se puede recuperar y perderia su borrador.
ESTADOS_MOMENTO = (ESTADO_CANDIDATO, ESTADO_FAVORITO, ESTADO_DESCARTADO)


class EstadoAnalisis(str, Enum):
    """Como termino el ranking. Ninguno de los tres es un fallo del proyecto."""

    #: Se propusieron todos los candidatos pedidos.
    COMPLETO = "complete"
    #: Se propusieron menos de los pedidos: el material no da para mas sin
    #: solaparlos o sin bajar del umbral.
    PARCIAL = "partial"
    #: No hubo ninguno. Es el audio plano del ticket: recuperable bajando el
    #: umbral, acortando la duracion o instalando una capacidad.
    SIN_CANDIDATOS = "insufficient"


def milisegundos(segundos: float) -> int:
    if isinstance(segundos, bool) or not isinstance(segundos, (int, float)) \
            or not math.isfinite(float(segundos)):
        raise ErrorMomentoAnalisis("Un rango de momento necesita segundos finitos.")
    return int(round(float(segundos) * 1000))


def identidad_momento(fuente_id: str, inicio_segundos: float, fin_segundos: float) -> str:
    """Id estable de un tramo dentro de su fuente.

    Deliberadamente **no** incluye la clave del ranking: dos configuraciones que
    proponen el mismo tramo proponen el mismo momento, y eso es lo que hace que
    un borrador sobreviva a un reranking.
    """
    if not isinstance(fuente_id, str) or not fuente_id:
        raise ErrorMomentoAnalisis("Un momento necesita saber de que fuente sale.")
    material = f"{fuente_id}|{milisegundos(inicio_segundos)}|{milisegundos(fin_segundos)}"
    return f"{PREFIJO_MOMENTO}-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:32]}"


@dataclass(frozen=True)
class Momento:
    """Candidato materializable: rango, score desglosado y razones."""

    id: str
    fuente_id: str
    inicio_segundos: float
    fin_segundos: float
    score: float
    posicion: int
    clave_analisis: str = ""
    componentes: tuple[ComponenteScore, ...] = ()
    razones: tuple[Razon, ...] = ()
    estado: str = ESTADO_CANDIDATO

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id:
            raise ErrorMomentoAnalisis("Un momento necesita identidad.")
        if not isinstance(self.fuente_id, str) or not self.fuente_id:
            raise ErrorMomentoAnalisis("Un momento necesita saber de que fuente sale.")
        for campo in ("inicio_segundos", "fin_segundos", "score"):
            valor = getattr(self, campo)
            if isinstance(valor, bool) or not isinstance(valor, (int, float)) \
                    or not math.isfinite(float(valor)):
                raise ErrorMomentoAnalisis(f"El momento {self.id!r} declara un '{campo}' invalido.")
            object.__setattr__(self, campo, float(valor))
        if self.inicio_segundos < 0:
            raise ErrorMomentoAnalisis(f"El momento {self.id!r} empieza antes del VOD.")
        if self.fin_segundos <= self.inicio_segundos:
            raise ErrorMomentoAnalisis(f"El momento {self.id!r} no cubre ninguna duracion.")
        if isinstance(self.posicion, bool) or not isinstance(self.posicion, int) \
                or self.posicion <= 0:
            raise ErrorMomentoAnalisis(f"El momento {self.id!r} necesita una posicion positiva.")
        if self.estado not in ESTADOS_MOMENTO:
            raise ErrorMomentoAnalisis(
                f"El momento {self.id!r} declara un estado desconocido: {self.estado!r}.")
        object.__setattr__(self, "componentes", tuple(self.componentes))
        object.__setattr__(self, "razones", tuple(self.razones))

    @property
    def duracion_segundos(self) -> float:
        return self.fin_segundos - self.inicio_segundos

    def solapa_con(self, otro: "Momento") -> bool:
        return (self.inicio_segundos < otro.fin_segundos
                and otro.inicio_segundos < self.fin_segundos)

    def como_documento(self) -> dict[str, object]:
        """Vista canonica: es lo que viaja al manifiesto y a SQLite."""
        return {
            "version": VERSION_CANDIDATO,
            "id": self.id,
            "fuente_id": self.fuente_id,
            "posicion": self.posicion,
            "inicio_segundos": round(self.inicio_segundos, 4),
            "fin_segundos": round(self.fin_segundos, 4),
            "duracion_segundos": round(self.duracion_segundos, 4),
            "score": round(self.score, 4),
            "clave_analisis": self.clave_analisis,
            "estado": self.estado,
            "componentes": [componente.como_documento() for componente in self.componentes],
            "razones": [razon.como_documento() for razon in self.razones],
        }


@dataclass(frozen=True)
class ReferenciaScore:
    """Contra que se midio todo el ranking. Sin esto el score no se puede leer.

    Un candidato dice "+8 dB"; sin saber sobre que base y con que escala se
    normalizo, ese numero no significa nada y no se puede comparar entre dos
    VODs. Viaja en el manifiesto junto a los candidatos, no aparte.
    """

    base_db: float
    escala_db: float
    muestras: int
    duracion_segundos: float

    def como_documento(self) -> dict[str, object]:
        return {"base_db": round(float(self.base_db), 4),
                "escala_db": round(float(self.escala_db), 4),
                "muestras": int(self.muestras),
                "duracion_segundos": round(float(self.duracion_segundos), 4)}


@dataclass(frozen=True)
class ResultadoRanking:
    """Lo que el ranking produjo: candidatos, estado y con que se midio."""

    momentos: tuple[Momento, ...] = ()
    estado: EstadoAnalisis = EstadoAnalisis.SIN_CANDIDATOS
    referencia: ReferenciaScore | None = None
    senales_presentes: tuple[str, ...] = ()
    senales_ausentes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "momentos", tuple(self.momentos))
        object.__setattr__(self, "senales_presentes", tuple(self.senales_presentes))
        object.__setattr__(self, "senales_ausentes", tuple(self.senales_ausentes))

    @property
    def suficientes(self) -> bool:
        return self.estado is not EstadoAnalisis.SIN_CANDIDATOS

    def como_documento(self, config: Mapping[str, object] | None = None) -> dict[str, object]:
        documento: dict[str, object] = {
            "version": VERSION_CANDIDATO,
            "estado": self.estado.value,
            "senales_presentes": list(self.senales_presentes),
            "senales_ausentes": list(self.senales_ausentes),
            "referencia": None if self.referencia is None else self.referencia.como_documento(),
            "candidatos": [momento.como_documento() for momento in self.momentos],
        }
        if config is not None:
            documento["config"] = dict(config)
        return documento


def _texto(valor: object, campo: str) -> str:
    if not isinstance(valor, str) or not valor:
        raise ErrorMomentoAnalisis(f"El manifiesto de candidatos no declara '{campo}'.")
    return valor


def _numero(valor: object, campo: str) -> float:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) \
            or not math.isfinite(float(valor)):
        raise ErrorMomentoAnalisis(f"El manifiesto de candidatos declara un '{campo}' invalido.")
    return float(valor)


def resultado_desde_documento(documento: Mapping[str, object]) -> ResultadoRanking:
    """Relee un manifiesto de candidatos publicado.

    Existe para que reutilizar un ranking sea de verdad reutilizarlo. La
    alternativa —volver a ejecutar `rankear` porque es barato— haria que
    "artefacto reutilizado" significase "no lo volvi a publicar", que es una cosa
    distinta y mas debil: un cambio en la regla de puntuacion se colaria en los
    resultados de un proyecto sin que su clave lo hubiera invalidado.

    Nada del documento es entrada confiable: viene de un archivo que pudo
    escribir otra version.
    """
    if not isinstance(documento, Mapping):
        raise ErrorMomentoAnalisis("El artefacto de candidatos no contiene un documento.")
    try:
        estado = EstadoAnalisis(_texto(documento.get("estado"), "estado"))
    except ValueError as error:
        raise ErrorMomentoAnalisis(
            f"El manifiesto de candidatos declara un estado desconocido: {error}") from error
    candidatos = documento.get("candidatos")
    if not isinstance(candidatos, Sequence) or isinstance(candidatos, (str, bytes)):
        raise ErrorMomentoAnalisis("El manifiesto de candidatos no declara su lista.")
    momentos = tuple(_momento_desde_documento(entrada) for entrada in candidatos)
    referencia_bruta = documento.get("referencia")
    referencia = None
    if isinstance(referencia_bruta, Mapping):
        referencia = ReferenciaScore(
            base_db=_numero(referencia_bruta.get("base_db"), "base_db"),
            escala_db=_numero(referencia_bruta.get("escala_db"), "escala_db"),
            muestras=int(_numero(referencia_bruta.get("muestras"), "muestras")),
            duracion_segundos=_numero(referencia_bruta.get("duracion_segundos"),
                                      "duracion_segundos"))
    return ResultadoRanking(
        momentos=momentos, estado=estado, referencia=referencia,
        senales_presentes=_lista_de_texto(documento.get("senales_presentes")),
        senales_ausentes=_lista_de_texto(documento.get("senales_ausentes")))


def _lista_de_texto(valor: object) -> tuple[str, ...]:
    if not isinstance(valor, Sequence) or isinstance(valor, (str, bytes)):
        return ()
    return tuple(elemento for elemento in valor if isinstance(elemento, str) and elemento)


def _momento_desde_documento(documento: object) -> Momento:
    if not isinstance(documento, Mapping):
        raise ErrorMomentoAnalisis("Un candidato publicado no es un objeto.")
    componentes = documento.get("componentes")
    razones = documento.get("razones")
    return Momento(
        id=_texto(documento.get("id"), "id"),
        fuente_id=_texto(documento.get("fuente_id"), "fuente_id"),
        inicio_segundos=_numero(documento.get("inicio_segundos"), "inicio_segundos"),
        fin_segundos=_numero(documento.get("fin_segundos"), "fin_segundos"),
        score=_numero(documento.get("score"), "score"),
        posicion=int(_numero(documento.get("posicion"), "posicion")),
        clave_analisis=str(documento.get("clave_analisis") or ""),
        componentes=_componentes_desde_documento(componentes),
        razones=_razones_desde_documento(razones),
        estado=str(documento.get("estado") or ESTADO_CANDIDATO))


def _componentes_desde_documento(valor: object) -> tuple[ComponenteScore, ...]:
    if not isinstance(valor, Sequence) or isinstance(valor, (str, bytes)):
        return ()
    componentes = []
    for entrada in valor:
        if not isinstance(entrada, Mapping):
            raise ErrorMomentoAnalisis("Un componente publicado no es un objeto.")
        componentes.append(ComponenteScore(
            senal=_texto(entrada.get("senal"), "senal"),
            valor=_numero(entrada.get("valor"), "valor"),
            referencia=_numero(entrada.get("referencia"), "referencia"),
            escala=_numero(entrada.get("escala"), "escala"),
            normalizado=_numero(entrada.get("normalizado"), "normalizado"),
            peso=_numero(entrada.get("peso"), "peso"),
            unidad=str(entrada.get("unidad") or "dB")))
    return tuple(componentes)


def _razones_desde_documento(valor: object) -> tuple[Razon, ...]:
    if not isinstance(valor, Sequence) or isinstance(valor, (str, bytes)):
        return ()
    razones = []
    for entrada in valor:
        if not isinstance(entrada, Mapping):
            raise ErrorMomentoAnalisis("Una razon publicada no es un objeto.")
        datos = entrada.get("datos")
        razones.append(Razon(_texto(entrada.get("codigo"), "codigo"),
                             dict(datos) if isinstance(datos, Mapping) else {}))
    return tuple(razones)


def sin_solapamientos(momentos: Sequence[Momento]) -> bool:
    """Invariante publica del ranking, comprobable desde fuera."""
    ordenados = sorted(momentos, key=lambda momento: momento.inicio_segundos)
    return all(anterior.fin_segundos <= siguiente.inicio_segundos
               for anterior, siguiente in zip(ordenados, ordenados[1:]))


__all__ = [
    "ESTADOS_MOMENTO", "ESTADO_CANDIDATO", "ESTADO_DESCARTADO", "ESTADO_FAVORITO",
    "EstadoAnalisis", "Momento", "PREFIJO_MOMENTO", "ReferenciaScore", "ResultadoRanking",
    "VERSION_CANDIDATO", "identidad_momento", "milisegundos", "sin_solapamientos",
    "resultado_desde_documento",
]
