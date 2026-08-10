"""Clave de materializacion: que hace reutilizable un artefacto.

El plan lo fija en una frase: *la clave de materializacion deriva de hashes de
entradas, version de contrato/proveedor y configuracion canonica*. Las tres
partes son necesarias y ninguna sobra:

- **entradas**: si cambian los bytes de la fuente, el resultado ya no describe
  lo que se le pide describir;
- **versiones**: el mismo archivo analizado por otra version del contrato o del
  proveedor produce otra cosa, aunque se llame igual;
- **configuracion canonica**: dos configuraciones que solo difieren en el orden
  de sus claves son *la misma*, y deben compartir artefacto.

De ahi que la canonicalizacion sea el nucleo del archivo. Sin ella, `{"a":1,
"b":2}` y `{"b":2, "a":1}` producirian claves distintas y el cache no acertaria
nunca; con una canonicalizacion permisiva, un `NaN` o un objeto arbitrario
producirian claves que dependen del interprete y el cache acertaria *de mas*.

Lo que no se puede representar sin ambiguedad se rechaza. Una clave silenciosa
y equivocada es peor que un error: reutiliza bytes que nadie volvera a mirar.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from enum import Enum
import hashlib
import json
import math

from .errors import ErrorClaveMaterializacion


#: Version del esquema de clave. Se incluye en el material hasheado, de modo
#: que cambiarla invalida todo el cache de forma explicita y no por accidente.
VERSION_CLAVE = "clipsapp-clave/1"
PREFIJO_CLAVE = "mk1"


def _canonico(valor: object, camino: str = "$") -> object:
    """Reduce el valor a tipos JSON con un orden y una forma unicos."""
    if valor is None or isinstance(valor, (bool, str)):
        return valor
    if isinstance(valor, Enum):
        return _canonico(valor.value, camino)
    if isinstance(valor, int):
        return valor
    if isinstance(valor, float):
        if not math.isfinite(valor):
            raise ErrorClaveMaterializacion(
                f"La configuracion contiene un numero no finito en {camino}.")
        # Un `float` que representa un entero se normaliza: `2.0` y `2` son la
        # misma configuracion escrita de dos formas y no deben partir el cache.
        return int(valor) if valor.is_integer() else valor
    if isinstance(valor, Mapping):
        entradas = []
        for clave, contenido in valor.items():
            if not isinstance(clave, str):
                raise ErrorClaveMaterializacion(
                    f"La configuracion usa una clave que no es texto en {camino}.")
            entradas.append((clave, _canonico(contenido, f"{camino}.{clave}")))
        entradas.sort(key=lambda par: par[0])
        return dict(entradas)
    if isinstance(valor, (Set, frozenset, set)):
        # Un conjunto no tiene orden propio: se ordena por su forma canonica ya
        # serializada para que dos conjuntos iguales produzcan el mismo texto
        # aunque sus elementos no sean comparables entre si.
        elementos = [_canonico(elemento, f"{camino}[]") for elemento in valor]
        return sorted(elementos, key=lambda elemento: json.dumps(
            elemento, ensure_ascii=False, sort_keys=True, allow_nan=False))
    if isinstance(valor, Sequence) and not isinstance(valor, (str, bytes, bytearray)):
        return [_canonico(elemento, f"{camino}[{indice}]")
                for indice, elemento in enumerate(valor)]
    raise ErrorClaveMaterializacion(
        f"La configuracion contiene un valor no representable ({type(valor).__name__}) en {camino}.")


def canonicalizar(valor: object) -> str:
    """Texto canonico de una configuracion. Misma configuracion, mismo texto."""
    return json.dumps(_canonico(valor), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _texto_obligatorio(valor: object, nombre: str) -> str:
    if not isinstance(valor, str) or not valor:
        raise ErrorClaveMaterializacion(f"La clave de materializacion necesita {nombre}.")
    return valor


def clave_materializacion(*, stage: str, version_contrato: str, version_proveedor: str,
                          entradas: Mapping[str, str],
                          config: Mapping[str, object] | None = None) -> str:
    """Clave estable de un artefacto, utilizable ademas como tramo de ruta.

    `entradas` asocia un nombre logico —`fuente`, `transcripcion`...— con la
    huella o checksum de esa entrada. Se exige que sean textos: pasar aqui una
    ruta, un identificador o un mtime haria que la clave cambiase sin que el
    contenido lo hiciera, o —peor— que no cambiase cuando el contenido si.
    """
    stage = _texto_obligatorio(stage, "el nombre de la etapa")
    version_contrato = _texto_obligatorio(version_contrato, "la version del contrato")
    version_proveedor = _texto_obligatorio(version_proveedor, "la version del proveedor")
    if not isinstance(entradas, Mapping) or not entradas:
        raise ErrorClaveMaterializacion("La clave de materializacion necesita sus entradas.")
    normalizadas: dict[str, str] = {}
    for nombre, huella in entradas.items():
        if not isinstance(nombre, str) or not nombre:
            raise ErrorClaveMaterializacion("Una entrada declara un nombre vacio.")
        if not isinstance(huella, str) or not huella:
            raise ErrorClaveMaterializacion(f"La entrada {nombre!r} no declara huella.")
        normalizadas[nombre] = huella
    material = canonicalizar({
        "version": VERSION_CLAVE,
        "stage": stage,
        "version_contrato": version_contrato,
        "version_proveedor": version_proveedor,
        "entradas": normalizadas,
        "config": dict(config or {}),
    })
    return f"{PREFIJO_CLAVE}-{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def es_clave_materializacion(valor: object) -> bool:
    """Forma esperada de una clave. La usa quien lee una fila persistida."""
    if not isinstance(valor, str) or not valor.startswith(PREFIJO_CLAVE + "-"):
        return False
    resto = valor[len(PREFIJO_CLAVE) + 1:]
    return len(resto) == 64 and all(caracter in "0123456789abcdef" for caracter in resto)
