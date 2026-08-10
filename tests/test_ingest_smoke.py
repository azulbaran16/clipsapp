"""Smoke real: ffprobe de verdad sobre fixtures de verdad, descargador fingido.

Todo lo demas de T04 se prueba con sondeos grabados, que es lo que permite que
la suite corra igual en Windows y en WSL. Esta prueba cierra el otro lado: que
el adaptador real hable con el ffprobe instalado y que lo que devuelve pase por
la normalizacion sin sorpresas.

Se salta entera si no hay FFmpeg. Ademas, cada rasgo que depende de como la
version instalada escribe el contenedor —la rotacion, la tasa variable— se
comprueba solo si el fixture generado *realmente* lo trae: afirmar lo contrario
convertiria una diferencia de version de FFmpeg en un fallo de ClipperKick.
"""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipperkick.application.ingest import SolicitudIngesta  # noqa: E402
from clipperkick.domain.ingest import (  # noqa: E402
    AvisoMedia, ErrorMediaDanada, es_clave_materializacion, normalizar_sondeo,
)
from clipperkick.infrastructure.ingest import (  # noqa: E402
    SondaFfprobeDetallada, crear_caso_de_uso_ingesta,
)
from clipperkick.infrastructure.project import crear_proyecto  # noqa: E402

from test_ingest_use_case import DescargadorFalso  # noqa: E402


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
DISPONIBLE = bool(FFMPEG and FFPROBE)

VIDEO_BASE = "testsrc=size=160x120:rate=30:duration=1"
AUDIO_BASE = "sine=frequency=440:duration=1"
CODIFICACION = ("-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p")


def ffmpeg(*argumentos: str) -> None:
    resultado = subprocess.run((FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *argumentos),
                               capture_output=True, text=True, errors="replace")
    if resultado.returncode != 0:
        raise unittest.SkipTest(f"FFmpeg no pudo generar el fixture: {resultado.stderr.strip()}")


@unittest.skipUnless(DISPONIBLE, "requiere FFmpeg y ffprobe instalados")
class SmokeFfprobeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tempdir = tempfile.TemporaryDirectory()
        cls.medios = Path(cls._tempdir.name)
        cls.cfr = cls.medios / "cfr.mp4"
        ffmpeg("-f", "lavfi", "-i", VIDEO_BASE, "-f", "lavfi", "-i", AUDIO_BASE,
               *CODIFICACION, "-c:a", "aac", "-shortest", str(cls.cfr))
        cls.mudo = cls.medios / "mudo.mp4"
        ffmpeg("-f", "lavfi", "-i", VIDEO_BASE, *CODIFICACION, str(cls.mudo))
        cls.multi = cls.medios / "multi.mp4"
        ffmpeg("-f", "lavfi", "-i", VIDEO_BASE, "-f", "lavfi", "-i",
               "testsrc2=size=160x120:rate=30:duration=1", "-f", "lavfi", "-i", AUDIO_BASE,
               "-map", "0:v", "-map", "1:v", "-map", "2:a", *CODIFICACION, "-c:a", "aac",
               "-shortest", str(cls.multi))
        cls.vfr = cls.medios / "vfr.mp4"
        ffmpeg("-f", "lavfi", "-i", VIDEO_BASE, "-vf", "select='not(mod(n,3))'",
               "-fps_mode", "vfr", *CODIFICACION, str(cls.vfr))
        cls.rotado = cls.medios / "rotado.mp4"
        # `-display_rotation` es opcion de *entrada*: reescribe la matriz de
        # display del contenedor, que es donde ffprobe la lee. La alternativa
        # `-metadata:s:v:0 rotate=` esta obsoleta y las versiones recientes la
        # ignoran en silencio.
        ffmpeg("-display_rotation", "90", "-i", str(cls.mudo), "-c", "copy", str(cls.rotado))
        cls.danado = cls.medios / "danado.mp4"
        cls.danado.write_bytes(b"esto no es un contenedor" * 64)

    @classmethod
    def tearDownClass(cls):
        cls._tempdir.cleanup()

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.base = Path(self.tempdir.name)

    def sonda(self):
        return SondaFfprobeDetallada()

    def metadata(self, ruta):
        return normalizar_sondeo(self.sonda().describir(str(ruta)))

    def caso(self, nombre="proyecto", descargador=None):
        proyecto = crear_proyecto(self.base / f"{nombre}.clipsapp", nombre)
        self.addCleanup(proyecto.close)
        return proyecto, crear_caso_de_uso_ingesta(proyecto, sonda=self.sonda(),
                                                   descargador=descargador)

    # -- sondeo ---------------------------------------------------------- #

    def test_la_version_del_proveedor_se_lee_del_binario(self):
        version = self.sonda().version()
        self.assertTrue(version.startswith("ffprobe/"))
        self.assertNotIn("sha256:", version, "un ffprobe normal declara su numero de version")

    def test_un_cfr_real_declara_streams_duracion_timebase_fps_y_audio(self):
        metadata = self.metadata(self.cfr)
        video = metadata.principal
        self.assertEqual((video.ancho, video.alto), (160, 120))
        self.assertAlmostEqual(metadata.duracion_segundos, 1.0, delta=0.2)
        self.assertIsNotNone(video.timebase)
        self.assertEqual(video.tasa_declarada.valor, 30.0)
        self.assertFalse(video.tasa_variable)
        self.assertTrue(metadata.tiene_audio)
        self.assertNotIn(AvisoMedia.SIN_AUDIO, metadata.avisos)

    def test_un_archivo_sin_audio_lo_declara_como_estado(self):
        metadata = self.metadata(self.mudo)
        self.assertFalse(metadata.tiene_audio)
        self.assertIn(AvisoMedia.SIN_AUDIO, metadata.avisos)

    def test_varios_flujos_de_video_se_declaran_como_estado(self):
        metadata = self.metadata(self.multi)
        self.assertGreaterEqual(len(metadata.video), 2)
        self.assertIn(AvisoMedia.MULTIPLES_VIDEO, metadata.avisos)

    def test_una_tasa_variable_real_se_declara_si_el_contenedor_la_trae(self):
        crudo = self.sonda().describir(str(self.vfr))
        video = next(flujo for flujo in crudo["streams"] if flujo.get("codec_type") == "video")
        if video.get("r_frame_rate") == video.get("avg_frame_rate"):
            self.skipTest("esta version de FFmpeg no dejo rastro de VFR en el contenedor")
        self.assertIn(AvisoMedia.TASA_VARIABLE, normalizar_sondeo(crudo).avisos)

    def test_una_rotacion_real_se_normaliza_si_el_contenedor_la_trae(self):
        metadata = self.metadata(self.rotado)
        if not metadata.principal.rotacion:
            self.skipTest("esta version de FFmpeg no escribio la rotacion pedida")
        self.assertIn(metadata.principal.rotacion, (90, 270))
        self.assertIn(AvisoMedia.ROTACION, metadata.avisos)
        self.assertEqual((metadata.principal.ancho_mostrado, metadata.principal.alto_mostrado),
                         (120, 160))

    def test_un_archivo_danado_es_un_error_tipado(self):
        with self.assertRaises(ErrorMediaDanada):
            self.sonda().describir(str(self.danado))

    # -- ingesta completa -------------------------------------------------- #

    def test_ingerir_un_archivo_real_publica_y_reutiliza(self):
        proyecto, caso = self.caso()
        primero = caso.ingerir(SolicitudIngesta(str(self.cfr)))
        self.assertTrue(es_clave_materializacion(primero.clave))
        self.assertTrue((proyecto.repositorio.raiz / primero.artefactos[0]).is_file())

        segundo = caso.ingerir(SolicitudIngesta(str(self.cfr)))
        self.assertEqual(segundo.clave, primero.clave)
        self.assertTrue(segundo.fuente_reutilizada)
        self.assertTrue(segundo.artefacto_reutilizado)
        self.assertEqual(segundo.metadata, primero.metadata)

    def test_ingerir_un_archivo_danado_no_deja_fuente_ni_copia(self):
        proyecto, caso = self.caso()
        with self.assertRaises(ErrorMediaDanada):
            caso.ingerir(SolicitudIngesta(str(self.danado)))
        self.assertEqual(proyecto.repositorio.conexion.execute(
            "SELECT COUNT(*) FROM SourceAsset").fetchone()[0], 0)
        self.assertEqual(list((proyecto.repositorio.raiz / "sources").iterdir()), [])

    def test_una_descarga_terminada_y_el_mismo_archivo_local_coinciden(self):
        datos = self.cfr.read_bytes()
        _local_proyecto, local = self.caso("local")
        _remoto_proyecto, remoto = self.caso("remoto", descargador=DescargadorFalso(datos))

        desde_disco = local.ingerir(SolicitudIngesta(str(self.cfr)))
        desde_red = remoto.ingerir(SolicitudIngesta("https://ejemplo/vod"))
        self.assertEqual(desde_disco.fuente.huella.identidad, desde_red.fuente.huella.identidad)
        self.assertEqual(desde_disco.metadata, desde_red.metadata)
        self.assertEqual(desde_disco.clave, desde_red.clave)


if __name__ == "__main__":
    unittest.main()
