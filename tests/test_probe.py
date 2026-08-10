from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipperkick.domain import ErrorSondeoMedia  # noqa: E402
from clipperkick.infrastructure.probe import ResultadoEjecucion, SondaFfprobe  # noqa: E402


class SondaFfprobeTests(unittest.TestCase):
    def sonda(self, resultado, disponible=True):
        return SondaFfprobe(lambda _comando: resultado, lambda _nombre: "ffprobe" if disponible else None)

    def test_devuelve_resultado_tipado(self):
        resultado = ResultadoEjecucion(0, '{"format":{"duration":"15.5"},"streams":[{"codec_type":"video","width":1080,"height":1920},{"codec_type":"audio"}]}')
        info = self.sonda(resultado).sondear("clip.mp4")
        self.assertEqual((info.duracion_segundos, info.ancho, info.alto, info.tiene_audio), (15.5, 1080, 1920, True))

    def test_informa_herramienta_ausente(self):
        with self.assertRaises(ErrorSondeoMedia):
            self.sonda(ResultadoEjecucion(0, "{}"), disponible=False).sondear("clip.mp4")

    def test_informa_fallo_del_proceso(self):
        with self.assertRaises(ErrorSondeoMedia):
            self.sonda(ResultadoEjecucion(1, "", "archivo invalido")).sondear("clip.mp4")

    def test_convierte_error_de_lanzamiento_del_ejecutor(self):
        causa = FileNotFoundError("ffprobe desaparecio")

        def ejecutor_fallido(_comando):
            raise causa

        with self.assertRaises(ErrorSondeoMedia) as contexto:
            SondaFfprobe(ejecutor_fallido, lambda _nombre: "C:/ffprobe.exe").sondear("clip.mp4")
        self.assertIs(contexto.exception.__cause__, causa)

    def test_rechaza_payload_invalido_o_incompleto(self):
        for payload in ("no-json", "{}", '{"format":{"duration":"nan"},"streams":[]}'):
            with self.subTest(payload=payload):
                with self.assertRaises(ErrorSondeoMedia):
                    self.sonda(ResultadoEjecucion(0, payload)).sondear("clip.mp4")
