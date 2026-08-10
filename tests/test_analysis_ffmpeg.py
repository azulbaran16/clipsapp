"""Contrato de comandos y parser ebur128 sin depender de FFmpeg instalado."""

from pathlib import Path
import math
import shutil
import struct
import sys
import tempfile
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipperkick.domain.analysis import ConfiguracionExtraccion, ConfiguracionRasgos  # noqa: E402
from clipperkick.infrastructure.analysis.ffmpeg import (  # noqa: E402
    ExtractorAudioFfmpeg, MedidorEbur128Ffmpeg, ResultadoProceso,
)


class _Ejecutor:
    def __init__(self, resultado=None):
        self.resultado = resultado or ResultadoProceso(0, "ffmpeg version fixture\n", "")
        self.comandos = []

    def ejecutar(self, comando, cancelado=None):
        self.comandos.append(tuple(comando))
        if "-c:a" in comando:
            Path(comando[-1]).write_bytes(b"wav")
        return self.resultado


class AdaptadoresFfmpegTests(unittest.TestCase):
    def test_extraccion_declara_pista_mono_pcm(self):
        ejecutor = _Ejecutor()
        extractor = ExtractorAudioFfmpeg(ejecutor, localizar=lambda _: "ffmpeg-fixture")
        with tempfile.TemporaryDirectory() as temporal:
            destino = str(Path(temporal) / "audio.wav")
            extractor.extraer("fuente.mp4", destino, ConfiguracionExtraccion())
        comando = ejecutor.comandos[-1]
        self.assertIn(("-map", "0:a:0"), tuple(zip(comando, comando[1:])))
        self.assertIn(("-ac", "1"), tuple(zip(comando, comando[1:])))
        self.assertIn(("-ar", "16000"), tuple(zip(comando, comando[1:])))
        self.assertIn(("-c:a", "pcm_s16le"), tuple(zip(comando, comando[1:])))

    def test_parser_ebur128_recupera_muestras_y_suelo(self):
        error = """
[Parsed_ebur128_0] t: 0.0999792 TARGET:-23 LUFS M:-inf S:-120.7
[Parsed_ebur128_0] t: 1.09998 TARGET:-23 LUFS M:-18.2 S:-120.7
"""
        ejecutor = _Ejecutor(ResultadoProceso(0, "", error))
        medidor = MedidorEbur128Ffmpeg(ejecutor, localizar=lambda _: "ffmpeg-fixture")
        documento = medidor.medir("audio.wav", ConfiguracionRasgos(piso_lufs=-70))
        self.assertEqual(documento["muestras"], [[0.0999792, -70.0], [1.09998, -18.2]])
        self.assertEqual(documento["duracion_segundos"], 1.09998)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg no esta instalado")
    def test_smoke_real_sobre_fixture_corto_con_dos_picos(self):
        with tempfile.TemporaryDirectory() as temporal:
            origen = Path(temporal) / "fixture.wav"
            destino = Path(temporal) / "normalizado.wav"
            tasa = 16000
            with wave.open(str(origen), "wb") as archivo:
                archivo.setnchannels(1)
                archivo.setsampwidth(2)
                archivo.setframerate(tasa)
                muestras = bytearray()
                for muestra in range(tasa * 20):
                    segundo = muestra / tasa
                    amplitud = 12000 if 5 <= segundo < 6 or 14 <= segundo < 15 else 800
                    valor = int(amplitud * math.sin(2 * math.pi * 440 * segundo))
                    muestras.extend(struct.pack("<h", valor))
                archivo.writeframes(muestras)

            ExtractorAudioFfmpeg().extraer(
                str(origen), str(destino), ConfiguracionExtraccion())
            documento = MedidorEbur128Ffmpeg().medir(
                str(destino), ConfiguracionRasgos(ventana_suavizado=1))

        self.assertGreater(len(documento["muestras"]), 100)
        valores = [muestra[1] for muestra in documento["muestras"]]
        self.assertGreater(max(valores) - min(valores), 10)
        self.assertGreater(documento["duracion_segundos"], 19)


if __name__ == "__main__":
    unittest.main()
