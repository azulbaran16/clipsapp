"""Reglas semanticas de dependencia para las fronteras de ClipperKick."""

import ast
from pathlib import Path
import unittest


RAIZ = Path(__file__).resolve().parents[1] / "src" / "clipperkick"
PROHIBIDOS = {"tkinter", "PySide6", "sqlite3", "subprocess"}
GRAMATICA_RENDER = ("filter_complex", "drawtext", "[0:v]", "[out]", "gblur", "overlay=")


def modulos(frontera: str):
    return (RAIZ / frontera).rglob("*.py")


def paquete_actual(ruta: Path) -> tuple[str, ...]:
    """Devuelve el paquete del modulo; __init__.py no es un componente."""
    relativo = ruta.relative_to(RAIZ).with_suffix("")
    return ("clipperkick",) + relativo.parts[:-1]


def destino_import_from(ruta: Path, nodo: ast.ImportFrom) -> tuple[str, ...]:
    paquete = paquete_actual(ruta)
    base = paquete[: len(paquete) - nodo.level + 1] if nodo.level else ()
    modulo = tuple(nodo.module.split(".")) if nodo.module else ()
    return base + modulo


def destinos_importados(ruta: Path, contenido: str) -> set[str]:
    destinos: set[str] = set()
    for nodo in ast.walk(ast.parse(contenido, filename=str(ruta))):
        if isinstance(nodo, ast.Import):
            destinos.update(alias.name for alias in nodo.names)
        elif isinstance(nodo, ast.ImportFrom):
            base = ".".join(destino_import_from(ruta, nodo))
            if nodo.module:
                destinos.add(base)
            else:
                destinos.update(f"{base}.{alias.name}" for alias in nodo.names)
    return destinos


def importa_raiz_prohibida(ruta: Path, contenido: str) -> bool:
    return any(destino.split(".")[0] in PROHIBIDOS
               for destino in destinos_importados(ruta, contenido))


class FronterasArquitecturaTests(unittest.TestCase):
    def test_dominio_y_aplicacion_no_importan_detalles_externos(self):
        for frontera in ("domain", "application"):
            for ruta in modulos(frontera):
                self.assertFalse(importa_raiz_prohibida(ruta, ruta.read_text(encoding="utf-8")), ruta)

    def test_grafo_de_capas_resuelve_imports_relativos(self):
        for frontera, prohibida in (("domain", "clipperkick.application"),
                                    ("domain", "clipperkick.infrastructure"),
                                    ("application", "clipperkick.infrastructure")):
            for ruta in modulos(frontera):
                for destino in destinos_importados(ruta, ruta.read_text(encoding="utf-8")):
                    self.assertFalse(destino == prohibida or destino.startswith(prohibida + "."),
                                     f"{ruta}: {destino}")

    def test_subprocess_esta_solo_en_infraestructura(self):
        for ruta in RAIZ.rglob("*.py"):
            if "infrastructure" not in ruta.relative_to(RAIZ).parts:
                self.assertNotIn("subprocess", (destino.split(".")[0]
                                                 for destino in destinos_importados(ruta, ruta.read_text(encoding="utf-8"))), ruta)

    def test_dominio_y_aplicacion_no_contienen_gramatica_del_renderer(self):
        for frontera in ("domain", "application"):
            for ruta in modulos(frontera):
                contenido = ruta.read_text(encoding="utf-8")
                for fragmento in GRAMATICA_RENDER:
                    self.assertNotIn(fragmento, contenido, f"{ruta}: {fragmento}")

    def test_tkinter_invoca_caso_de_uso_y_no_motor(self):
        ui = (RAIZ / "ui.py").read_text(encoding="utf-8")
        self.assertIn("crear_caso_de_uso_compatibilidad", ui)
        self.assertNotIn("Motor(", ui)


class ResolverImportsTests(unittest.TestCase):
    def ruta(self, relativa: str) -> Path:
        return RAIZ / relativa

    def test_rechaza_from_tkinter_en_modulo_paquete_y_subpaquete(self):
        for relativa in ("application/modulo.py", "application/__init__.py",
                         "application/sub/modulo.py", "application/sub/__init__.py"):
            with self.subTest(relativa=relativa):
                self.assertTrue(importa_raiz_prohibida(self.ruta(relativa), "from tkinter import ttk"))

    def test_resuelve_infraestructura_relativa_en_modulo_y_paquete(self):
        casos = (
            ("application/modulo.py", "from ..infrastructure import legacy"),
            ("application/__init__.py", "from ..infrastructure import legacy"),
            ("application/sub/modulo.py", "from ...infrastructure import legacy"),
            ("application/sub/__init__.py", "from ...infrastructure import legacy"),
        )
        for relativa, contenido in casos:
            with self.subTest(relativa=relativa):
                destinos = destinos_importados(self.ruta(relativa), contenido)
                self.assertEqual(destinos, {"clipperkick.infrastructure"})

    def test_detecta_infraestructura_relativa_resuelta(self):
        casos = (
            ("application/modulo.py", "from ..infrastructure import legacy"),
            ("application/__init__.py", "from ..infrastructure import legacy"),
            ("application/sub/modulo.py", "from ...infrastructure import legacy"),
            ("application/sub/__init__.py", "from ...infrastructure import legacy"),
        )
        prohibida = "clipperkick.infrastructure"
        for relativa, contenido in casos:
            with self.subTest(relativa=relativa):
                self.assertTrue(any(destino == prohibida or destino.startswith(prohibida + ".")
                                    for destino in destinos_importados(self.ruta(relativa), contenido)))
