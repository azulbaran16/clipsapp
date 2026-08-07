"""Valida la versión única del proyecto y su etiqueta de Git."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
import re


RAIZ = Path(__file__).resolve().parents[1]
ARCHIVO_VERSION = RAIZ / "src" / "clipperkick" / "__init__.py"
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def leer_version(ruta: Path = ARCHIVO_VERSION) -> str:
    """Lee ``__version__`` sin importar ni ejecutar el paquete."""
    arbol = ast.parse(ruta.read_text("utf-8"), filename=str(ruta))
    for nodo in arbol.body:
        if not isinstance(nodo, (ast.Assign, ast.AnnAssign)):
            continue
        objetivos = nodo.targets if isinstance(nodo, ast.Assign) else [nodo.target]
        valor = nodo.value
        if any(isinstance(objetivo, ast.Name) and objetivo.id == "__version__" for objetivo in objetivos):
            if isinstance(valor, ast.Constant) and isinstance(valor.value, str):
                return valor.value
    raise ValueError(f"No se encontró __version__ en {ruta}")


def validar_etiqueta(etiqueta: str, version: str) -> str:
    """Comprueba SemVer estable y coincidencia exacta con ``vX.Y.Z``."""
    if not SEMVER.fullmatch(version):
        raise ValueError(f"La versión '{version}' no cumple MAJOR.MINOR.PATCH")
    esperada = f"v{version}"
    if etiqueta != esperada:
        raise ValueError(f"La etiqueta '{etiqueta}' no coincide con '{esperada}'")
    return version


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="Etiqueta Git que debe coincidir, por ejemplo v1.0.0")
    argumentos = parser.parse_args()
    version = leer_version()
    if not SEMVER.fullmatch(version):
        raise SystemExit(f"Versión inválida: {version}")
    if argumentos.tag:
        try:
            validar_etiqueta(argumentos.tag, version)
        except ValueError as error:
            raise SystemExit(str(error)) from error
    print(f"Versión válida: {version}")


if __name__ == "__main__":
    main()
