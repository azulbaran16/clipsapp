from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipperkick.engine import (  # noqa: E402
    construir_filtro,
    elegir_picos,
    escapar_texto,
    formato_legible,
    formato_nombre,
)
from scripts.check_version import leer_version, validar_etiqueta  # noqa: E402


class FormatoTests(unittest.TestCase):
    def test_formatos_de_tiempo(self):
        self.assertEqual(formato_legible(3723.9), "01:02:03")
        self.assertEqual(formato_nombre(3723.9), "01-02-03")

    def test_escape_para_drawtext(self):
        self.assertEqual(escapar_texto("Canal: 50%, 'sí'"), "Canal\\: 50\\, sí")


class FiltroTests(unittest.TestCase):
    def test_filtro_horizontal_sin_marca(self):
        filtro = construir_filtro(False, "", False)
        self.assertEqual(filtro, "[0:v]null[v];[v]null[out]")

    def test_filtro_vertical_con_logo_y_nombre(self):
        filtro = construir_filtro(True, "Mi canal", True, fuente="")
        self.assertIn("crop=1080:1920", filtro)
        self.assertIn("[1:v]scale=-1:110", filtro)
        self.assertIn("text='Mi canal'", filtro)
        self.assertTrue(filtro.endswith("[out]"))


class PicosTests(unittest.TestCase):
    def test_elije_picos_intensos_y_separados(self):
        niveles = [-20.0] * 300
        niveles[100] = -3.0
        niveles[220] = -5.0
        self.assertEqual(
            [segundo for segundo, _ in elegir_picos(niveles, 2, 30)],
            [100, 220],
        )

    def test_ignora_senal_plana_y_entrada_vacia(self):
        self.assertEqual(elegir_picos([-20.0] * 100, 6, 45), [])
        self.assertEqual(elegir_picos([], 6, 45), [])


class VersionTests(unittest.TestCase):
    def test_version_actual_es_semver(self):
        self.assertEqual(leer_version(), "1.0.0")

    def test_etiqueta_debe_coincidir(self):
        self.assertEqual(validar_etiqueta("v1.0.0", "1.0.0"), "1.0.0")
        with self.assertRaises(ValueError):
            validar_etiqueta("v1.0.1", "1.0.0")

    def test_pyproject_usa_una_fuente_dinamica(self):
        contenido = Path("pyproject.toml").read_text("utf-8")
        self.assertIn('dynamic = ["version"]', contenido)
        self.assertNotIn('\nversion = "1.0.0"', contenido)
        self.assertIn(
            'version = {attr = "clipperkick.__version__"}', contenido
        )


if __name__ == "__main__":
    unittest.main()
