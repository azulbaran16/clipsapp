from contextlib import contextmanager
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipperkick.application.compatibility import CasoDeUsoCompatibilidad  # noqa: E402
from clipperkick.domain import SolicitudClips  # noqa: E402


class _EspacioTemporal:
    @contextmanager
    def crear(self):
        yield "temporal"


class _Herramientas:
    def comprobar_renderizador(self):
        self.comprobado = True


class _Fuente:
    def obtener(self, entrada, carpeta_temporal, hd, log):
        self.argumentos = (entrada, carpeta_temporal, hd)
        return "video.mp4"


class _Analizador:
    def analizar(self, video, log):
        niveles = [-20.0] * 300
        niveles[100] = -3.0
        return niveles


class _Exportador:
    def exportar(self, video, picos, duracion, carpeta_salida, vertical, nombre_canal, logo, log):
        self.argumentos = (video, picos, duracion, carpeta_salida, vertical, nombre_canal, logo)
        return ["salida/clip_01.mp4"]


class CasoDeUsoCompatibilidadTests(unittest.TestCase):
    def test_coordina_adaptadores_inyectados_y_conserva_el_ranking(self):
        fuente, exportador = _Fuente(), _Exportador()
        herramientas = _Herramientas()
        caso = CasoDeUsoCompatibilidad(fuente, _Analizador(), exportador, _EspacioTemporal(), herramientas)
        resultado = caso.procesar(SolicitudClips("entrada", 1, 30, "salida", True, False, "Canal", ""))
        self.assertEqual(resultado, ["salida/clip_01.mp4"])
        self.assertEqual(fuente.argumentos, ("entrada", "temporal", False))
        self.assertEqual(exportador.argumentos[1], [(100, 17.0)])
        self.assertTrue(herramientas.comprobado)
