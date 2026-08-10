"""Adaptadores de ingesta: huellas, ffprobe, fuentes, artefactos y descarga.

Aqui si se toca disco, pero nunca la red ni FFmpeg: el ejecutor de procesos y el
descargador entran fingidos por el mismo puerto que usa produccion. Lo que se
prueba es exactamente lo que el ticket exige de esta capa —determinismo de la
huella, publicacion que verifica checksum antes de mover nada, y limpieza que no
toca lo que no creo—.
"""

from pathlib import Path
import errno
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipperkick.domain.ingest import (  # noqa: E402
    ArchivoArtefacto, ErrorArtefactoIngesta, ErrorCancelacionIngesta, ErrorDescargaIngesta,
    ErrorEvidenciaHuella, ErrorFuenteIngesta,
    ErrorHuellaIngesta, ErrorHuellaInestable, ErrorMediaDanada, ErrorMetadataMedia, HuellaFuente,
    LinajeArtefacto,
    ManifiestoArtefacto, ModoFuente, OrigenFuente, SourceAsset, clave_materializacion,
    detecta_reemplazo,
)
from clipperkick.infrastructure.ingest import (  # noqa: E402
    NOMBRE_SALIDA, PREFIJO_PARCIAL, PREFIJO_STAGING, SUFIJO_PARCIAL,
    AlmacenArtefactosIngestaProyecto, AlmacenFuentesProyecto, DescargadorYtDlp,
    ServicioHuellaArchivo, SondaFfprobeDetallada, comando_descarga,
)
from clipperkick.domain.jobs import (  # noqa: E402
    ArchivoSalida, ErrorIdentificadorJob, ErrorValidacionSalida, ManifiestoSalida,
)
from clipperkick.infrastructure.ingest.artifacts import (  # noqa: E402
    NOMBRE_INTENTO, digest_archivo,
)
from clipperkick.infrastructure.ingest import sources as fuentes_mod  # noqa: E402
from clipperkick.infrastructure.ingest.fingerprints import _sello_descriptor  # noqa: E402
from clipperkick.infrastructure.jobs import artifacts as artefactos_jobs  # noqa: E402
from clipperkick.infrastructure.jobs.artifacts import (  # noqa: E402
    NOMBRE_PUBLICACION, NOMBRE_SELLADO, AlmacenArtefactosProyecto,
)
from clipperkick.infrastructure.probe import ResultadoEjecucion  # noqa: E402


class _Contextos:
    """Varios context managers como uno solo, para no anidar `with` a mano."""

    def __init__(self, *contextos):
        self._contextos = contextos

    def __enter__(self):
        for contexto in self._contextos:
            contexto.__enter__()
        return self

    def __exit__(self, *argumentos):
        for contexto in reversed(self._contextos):
            contexto.__exit__(*argumentos)
        return False


def _volumen_ajeno():
    """Directorio en un dispositivo distinto al de los temporales, si lo hay.

    En WSL, `/tmp` es tmpfs y `/mnt/c` es DrvFS: dos `st_dev` de verdad. En
    Windows todo cae en la misma unidad y no hay nada que probar aqui.
    """
    try:
        propio = os.stat(tempfile.gettempdir()).st_dev
    except OSError:
        return None
    for candidato in ("/mnt/c/tmp", "/mnt/c/Windows/Temp"):
        ruta = Path(candidato)
        try:
            if ruta.is_dir() and os.stat(ruta).st_dev != propio and os.access(ruta, os.W_OK):
                return ruta
        except OSError:
            continue
    return None


VOLUMEN_AJENO = _volumen_ajeno()

CLAVE = clave_materializacion(stage="ingesta.sondeo", version_contrato="1/m",
                              version_proveedor="ffprobe/8.0", entradas={"fuente": "h"})
LINAJE = LinajeArtefacto(stage="ingesta.sondeo", version_contrato="1/m",
                         version_proveedor="ffprobe/8.0", clave=CLAVE, entradas={"fuente": "h"})


class BaseTemporal(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.base = Path(self.tempdir.name)

    def proyecto(self) -> Path:
        raiz = self.base / "proyecto.clipsapp"
        for directorio in ("sources", "artifacts", "cache", "outputs"):
            (raiz / directorio).mkdir(parents=True, exist_ok=True)
        return raiz


# --------------------------------------------------------------------------- #
# Huellas
# --------------------------------------------------------------------------- #

class _EspiaLectura:
    """Archivo binario que ejecuta un efecto despues de cada `read`.

    Envuelve en vez de parchear `builtins.open` para que el sabotaje alcance
    exactamente a la lectura del servicio y no a cualquier otra del proceso.
    """

    def __init__(self, archivo, al_leer, al_cerrar=None):
        self._archivo = archivo
        self._al_leer = al_leer
        self._al_cerrar = al_cerrar

    def read(self, tamano=-1):
        datos = self._archivo.read(tamano)
        self._al_leer()
        return datos

    def seek(self, *argumentos):
        return self._archivo.seek(*argumentos)

    def fileno(self):
        return self._archivo.fileno()

    def close(self):
        resultado = self._archivo.close()
        if self._al_cerrar is not None:
            self._al_cerrar()
        return resultado


class _HuellaConSenal(ServicioHuellaArchivo):
    """Servicio sobre un volumen cuya marca monotonica *si* esta demostrada.

    Las pruebas de determinismo hablan del hashing, no de la evidencia: sin esto
    tendrian que usar archivos que quepan enteros en su ventana y dejarian de
    ejercitar el caso de varias ventanas. La regla de evidencia tiene sus propias
    pruebas, incluida la que comprueba que en DrvFS no se da por buena.
    """

    def _senal_fiable(self, _sello):
        return True


class _HuellaConSabotaje(ServicioHuellaArchivo):
    """Servicio real con un efecto inyectado entre bloque y bloque."""

    def __init__(self, sabotaje, al_cerrar=None, **argumentos):
        super().__init__(**argumentos)
        self._sabotaje = sabotaje
        self._al_cerrar = al_cerrar
        self.aperturas = 0

    def _abrir(self, ruta):
        self.aperturas += 1
        return _EspiaLectura(super()._abrir(ruta), self._sabotaje, self._al_cerrar)


class ServicioHuellaTests(BaseTemporal):
    def archivo(self, nombre: str, datos: bytes) -> Path:
        destino = self.base / nombre
        destino.write_bytes(datos)
        return destino

    def servicio(self, **argumentos):
        """Servicio real, con la marca monotonica dada por demostrada."""
        return _HuellaConSenal(**argumentos)

    def test_los_mismos_bytes_producen_la_misma_huella_con_otro_nombre(self):
        servicio = self.servicio(ventana=64, bloque=16)
        primera = servicio.completa(str(self.archivo("a.bin", b"x" * 500)))
        segunda = servicio.completa(str(self.archivo("b.bin", b"x" * 500)))
        self.assertEqual(primera.identidad, segunda.identidad)
        self.assertEqual(primera.parcial, segunda.parcial)

    def test_reemplazar_los_bytes_con_el_mismo_nombre_y_tamano_cambia_la_huella(self):
        servicio = self.servicio(ventana=64, bloque=16)
        ruta = self.archivo("mismo.bin", b"a" * 500)
        antes = servicio.completa(str(ruta))
        ruta.write_bytes(b"b" * 500)
        despues = servicio.completa(str(ruta))
        self.assertNotEqual(antes.identidad, despues.identidad)
        self.assertNotEqual(antes.parcial, despues.parcial)
        self.assertTrue(detecta_reemplazo(antes, despues))

    def test_la_pasada_unica_produce_la_misma_parcial_que_el_muestreo_suelto(self):
        """Si divergieran, reconciliar y materializar hablarian de archivos distintos."""
        servicio = self.servicio(ventana=64, bloque=7)
        for tamano in (0, 1, 63, 64, 65, 200, 4096):
            with self.subTest(tamano=tamano):
                ruta = self.archivo(f"t{tamano}.bin", bytes(range(256)) * (tamano // 256) +
                                    bytes(range(tamano % 256)))
                self.assertEqual(servicio.completa(str(ruta)).parcial,
                                 servicio.parcial(str(ruta)).parcial)

    def test_registra_tamano_y_mtime(self):
        ruta = self.archivo("c.bin", b"hola")
        huella = ServicioHuellaArchivo().completa(str(ruta))
        self.assertEqual(huella.tamano, 4)
        self.assertEqual(huella.mtime_ns, ruta.stat().st_mtime_ns)

    def test_una_fuente_ausente_o_que_no_es_archivo_es_error_tipado(self):
        servicio = ServicioHuellaArchivo()
        with self.assertRaises(ErrorHuellaIngesta):
            servicio.completa(str(self.base / "no-existe.bin"))
        with self.assertRaises(ErrorHuellaIngesta):
            servicio.completa(str(self.base))

    # -- estabilidad de la pasada ---------------------------------------- #

    def sabotear(self, sabotaje, intentos=1, bloque=4, ventana=64, al_cerrar=None):
        """Servicio real con un efecto inyectado tras cada lectura o al cerrar."""
        return _HuellaConSabotaje(sabotaje, al_cerrar, ventana=ventana, bloque=bloque,
                                  intentos=intentos)

    def test_una_mutacion_en_sitio_del_mismo_tamano_con_mtime_restaurado_se_rechaza(self):
        """El hallazgo: la pasada devolvia la huella de una mezcla inexistente.

        El tamano no cambia y el `mtime` se restaura, de modo que ninguna de las
        senales baratas lo delata. Lo delata la marca de cambio del descriptor:
        `st_ctime_ns` en POSIX y `ChangeTime` en Windows.
        """
        ruta = self.archivo("mezcla.bin", b"AAAABBBB")
        marcas = (ruta.stat().st_atime_ns, ruta.stat().st_mtime_ns)
        estado = {"veces": 0}

        def mutar():
            estado["veces"] += 1
            if estado["veces"] == 1:
                with open(ruta, "r+b") as escritor:
                    escritor.seek(0)
                    escritor.write(b"ZZZZ")
                os.utime(ruta, ns=marcas)

        with self.assertRaises(ErrorHuellaInestable):
            self.sabotear(mutar).completa(str(ruta))
        self.assertEqual(ruta.stat().st_size, 8, "el escenario mantiene el tamano")
        self.assertEqual(ruta.stat().st_mtime_ns, marcas[1], "y restaura el mtime")

    def test_un_reemplazo_de_la_ruta_tras_leerla_se_rechaza(self):
        """La huella seria correcta, pero de un archivo que ya no es esa fuente.

        El reemplazo se hace justo despues de cerrar el descriptor, que es la
        unica ventana real: Windows no deja renombrar sobre un archivo abierto,
        de modo que sustituir *durante* la lectura no es un escenario que pueda
        ocurrir alli. Lo que se prueba es la comprobacion que lo cubre en los dos
        sistemas: la ruta tiene que seguir designando el mismo archivo al acabar.
        """
        ruta = self.archivo("path.bin", b"AAAABBBB")
        suplantador = self.archivo("otro.bin", b"CCCCDDDD")

        with self.assertRaises(ErrorHuellaInestable):
            self.sabotear(lambda: None,
                          al_cerrar=lambda: os.replace(suplantador, ruta)).completa(str(ruta))

    def test_un_archivo_que_se_acorta_a_mitad_no_produce_huella(self):
        ruta = self.archivo("corta.bin", b"z" * 32)
        estado = {"veces": 0}

        def truncar():
            estado["veces"] += 1
            if estado["veces"] == 1:
                with open(ruta, "r+b") as escritor:
                    escritor.truncate(8)

        with self.assertRaises(ErrorHuellaInestable):
            self.sabotear(truncar).completa(str(ruta))

    def test_una_mutacion_puntual_se_reintenta_y_acaba_dando_la_huella_del_disco(self):
        ruta = self.archivo("reintento.bin", b"AAAABBBB")
        estado = {"veces": 0}

        def mutar_una_vez():
            estado["veces"] += 1
            if estado["veces"] == 1:
                with open(ruta, "r+b") as escritor:
                    escritor.seek(0)
                    escritor.write(b"ZZZZ")

        servicio = self.sabotear(mutar_una_vez, intentos=3)
        huella = servicio.completa(str(ruta))
        self.assertEqual(huella,
                         self.servicio(ventana=64, bloque=4).completa(str(ruta)),
                         "la huella devuelta es la del contenido que quedo en disco")
        self.assertGreaterEqual(servicio.aperturas, 2, "hubo que repetir la pasada")

    def test_una_huella_estable_gasta_una_sola_pasada(self):
        ruta = self.archivo("estable.bin", b"x" * 48)
        servicio = self.sabotear(lambda: None, intentos=3, bloque=16)
        servicio.completa(str(ruta))
        self.assertEqual(servicio.aperturas, 1,
                         "una sola pasada de SHA por intento, y un solo intento si es estable")

    # -- evidencia demostrable ------------------------------------------- #

    def sin_senal(self, **argumentos):
        """Servicio sobre un volumen que no ofrece marca monotonica fiable.

        Se fuerza el veredicto en vez de buscar un FAT32: lo que hay que probar
        es la decision —que sin senal y sin cobertura no se acepta nada—, no la
        deteccion de la plataforma, que se comprueba aparte.
        """
        class SinSenal(ServicioHuellaArchivo):
            def __init__(self, **claves):
                super().__init__(**claves)
                self.aperturas = 0

            def _senal_fiable(self, _sello):
                return False

            def _abrir(self, ruta):
                self.aperturas += 1
                return super()._abrir(ruta)

        return SinSenal(**argumentos)

    def test_sin_senal_monotonica_un_archivo_no_cubierto_se_rechaza(self):
        """Ni la relectura ni el sello alcanzan al tramo no muestreado."""
        ruta = self.archivo("grande.bin", bytes(range(256)) * 4)
        with self.assertRaises(ErrorEvidenciaHuella) as capturado:
            self.sin_senal(ventana=8, bloque=8, intentos=3).completa(str(ruta))
        self.assertIsInstance(capturado.exception, ErrorHuellaInestable,
                              "sigue siendo una huella que no se puede afirmar")

    def test_sin_senal_monotonica_un_archivo_cubierto_sigue_siendo_valido(self):
        """Si las ventanas cubren el archivo entero, la relectura *es* la prueba."""
        ruta = self.archivo("pequeno.bin", b"x" * 64)
        huella = self.sin_senal(ventana=1024, bloque=16).completa(str(ruta))
        self.assertEqual(huella, self.servicio(ventana=1024, bloque=16).completa(str(ruta)))

    def test_la_evidencia_insuficiente_no_se_reintenta(self):
        """Volver a mirar no hace aparecer la senal que falta."""
        ruta = self.archivo("grande2.bin", bytes(range(256)) * 4)
        servicio = self.sin_senal(ventana=8, bloque=8, intentos=3)
        with self.assertRaises(ErrorEvidenciaHuella):
            servicio.completa(str(ruta))
        self.assertEqual(servicio.aperturas, 1, "un solo intento: no hay nada que reintentar")

    def test_la_parcial_sigue_disponible_sin_senal_monotonica(self):
        """La parcial nunca afirma identidad, y es la unica reconciliacion barata."""
        ruta = self.archivo("grande3.bin", bytes(range(256)) * 4)
        huella = self.sin_senal(ventana=8, bloque=8).parcial(str(ruta))
        self.assertIsNone(huella.completa)
        with self.assertRaises(ErrorHuellaIngesta):
            _ = huella.identidad

    def test_una_mutacion_fuera_de_las_ventanas_se_rechaza_con_senal_real(self):
        """El escenario del review, ahora sobre el volumen real de la corrida."""
        ruta = self.archivo("fuera.bin", bytes(range(128)))
        marcas = (ruta.stat().st_atime_ns, ruta.stat().st_mtime_ns)
        estado = {"veces": 0}

        def mutar():
            estado["veces"] += 1
            if estado["veces"] == 5:
                with open(ruta, "r+b") as escritor:
                    escritor.seek(16)
                    escritor.write(b"ZZZZZZZZ")
                os.utime(ruta, ns=marcas)

        with self.assertRaises(ErrorHuellaInestable):
            self.sabotear(mutar, ventana=8, bloque=8, intentos=1).completa(str(ruta))

    @unittest.skipUnless(os.name == "nt", "el USN es la senal de NTFS")
    def test_en_ntfs_el_usn_basta_como_senal(self):
        ruta = self.archivo("senal.bin", b"x" * 32)
        servicio = ServicioHuellaArchivo(ventana=8, bloque=8)
        with open(ruta, "rb") as archivo:
            self.assertTrue(servicio._senal_fiable(_sello_descriptor(archivo)),
                            "este volumen no lleva journal USN; revisa el entorno")

    @unittest.skipIf(os.name == "nt", "en Windows la senal es el USN, no `ctime`")
    def test_en_posix_sin_sitio_donde_medir_no_se_da_por_buena_la_senal(self):
        """Que `st_ctime_ns` exista no demuestra que sirva para lo que se necesita.

        Sobre ext4 y tmpfs dos escrituras seguidas comparten marca, que es
        justamente el caso que la huella teme. Sin un sitio propio donde medirlo,
        la respuesta conservadora es "no se puede demostrar".
        """
        ruta = self.archivo("senal.bin", b"x" * 32)
        servicio = ServicioHuellaArchivo(ventana=8, bloque=8)
        with open(ruta, "rb") as archivo:
            self.assertFalse(servicio._senal_fiable(_sello_descriptor(archivo)))

    def test_la_sonda_de_resolucion_se_mide_una_vez_y_no_deja_residuo(self):
        prueba = self.base / "sonda"
        servicio = ServicioHuellaArchivo(ventana=8, bloque=8, directorio_prueba=prueba)
        ruta = self.archivo("reactividad.bin", b"x" * 32)
        with open(ruta, "rb") as archivo:
            sello = _sello_descriptor(archivo)
            primera = servicio._ctime_reacciona(sello.dev)
            self.assertIs(servicio._ctime_reacciona(sello.dev), primera,
                          "el veredicto se memoriza por dispositivo")
        self.assertIsInstance(primera, bool)
        self.assertEqual(list(prueba.iterdir()) if prueba.is_dir() else [], [],
                         "la sonda se retira siempre")

    def test_un_volumen_donde_ctime_reacciona_admite_archivos_grandes(self):
        """Con la senal demostrada, un archivo no cubierto ya no se rechaza."""
        class ConSenal(ServicioHuellaArchivo):
            def _senal_fiable(self, _sello):
                return True

        ruta = self.archivo("grande4.bin", bytes(range(256)) * 4)
        huella = ConSenal(ventana=1024, bloque=8).completa(str(ruta))
        self.assertIsNotNone(huella.completa)

    def test_la_huella_parcial_tambien_rechaza_una_mutacion(self):
        ruta = self.archivo("parcial.bin", b"AAAABBBB")
        estado = {"veces": 0}

        def mutar():
            estado["veces"] += 1
            if estado["veces"] == 1:
                with open(ruta, "r+b") as escritor:
                    escritor.seek(0)
                    escritor.write(b"ZZZZ")

        with self.assertRaises(ErrorHuellaInestable):
            self.sabotear(mutar, ventana=4).parcial(str(ruta))


# --------------------------------------------------------------------------- #
# ffprobe
# --------------------------------------------------------------------------- #

class SondaDetalladaTests(unittest.TestCase):
    def sonda(self, respuestas, disponible=True):
        llamadas = []

        def ejecutor(comando):
            llamadas.append(tuple(comando))
            return respuestas[min(len(llamadas) - 1, len(respuestas) - 1)]

        instancia = SondaFfprobeDetallada(
            ejecutor, lambda _n: "ffprobe" if disponible else None)
        return instancia, llamadas

    def test_lee_la_version_del_binario_y_la_memoriza(self):
        sonda, llamadas = self.sonda([ResultadoEjecucion(0, "ffprobe version 8.0-full_build\n")])
        self.assertEqual(sonda.version(), "ffprobe/8.0-full_build")
        self.assertEqual(sonda.version(), "ffprobe/8.0-full_build")
        self.assertEqual(len(llamadas), 1, "la version no cambia a mitad de sesion")

    def test_una_version_ilegible_se_reduce_al_digest_de_su_salida(self):
        primera, _ = self.sonda([ResultadoEjecucion(0, "algo raro")])
        segunda, _ = self.sonda([ResultadoEjecucion(0, "algo raro")])
        tercera, _ = self.sonda([ResultadoEjecucion(0, "otro binario")])
        self.assertTrue(primera.version().startswith("ffprobe/sha256:"))
        self.assertEqual(primera.version(), segunda.version())
        self.assertNotEqual(primera.version(), tercera.version())

    def test_devuelve_el_documento_crudo_sin_normalizar(self):
        sonda, llamadas = self.sonda([ResultadoEjecucion(0, '{"streams": [], "format": {}}')])
        self.assertEqual(sonda.describir("clip.mp4"), {"streams": [], "format": {}})
        self.assertIn("-show_streams", llamadas[0])
        self.assertIn("-show_format", llamadas[0])

    def test_un_archivo_danado_es_error_tipado_por_codigo_o_por_declaracion(self):
        por_codigo, _ = self.sonda([ResultadoEjecucion(1, "", "Invalid data found")])
        with self.assertRaises(ErrorMediaDanada):
            por_codigo.describir("roto.mp4")
        por_declaracion, _ = self.sonda([ResultadoEjecucion(
            0, '{"error": {"code": -1094995529, "string": "Invalid data found"}}')])
        with self.assertRaises(ErrorMediaDanada):
            por_declaracion.describir("roto.mp4")

    def test_una_salida_ilegible_se_distingue_de_un_archivo_danado(self):
        sonda, _ = self.sonda([ResultadoEjecucion(0, "no-json")])
        with self.assertRaises(ErrorMetadataMedia):
            sonda.describir("clip.mp4")

    def test_sin_herramienta_no_se_lanza_nada(self):
        sonda, llamadas = self.sonda([ResultadoEjecucion(0, "{}")], disponible=False)
        with self.assertRaises(ErrorMediaDanada):
            sonda.describir("clip.mp4")
        self.assertEqual(llamadas, [])


# --------------------------------------------------------------------------- #
# Almacen de fuentes
# --------------------------------------------------------------------------- #

class AlmacenFuentesTests(BaseTemporal):
    def setUp(self):
        super().setUp()
        self.raiz = self.proyecto()
        self.almacen = AlmacenFuentesProyecto(self.raiz)
        self.origen = self.base / "clip original.mp4"
        self.origen.write_bytes(b"video")

    def test_copiar_deja_el_original_y_publica_una_ruta_relativa(self):
        ubicacion = self.almacen.incorporar(str(self.origen))
        self.assertTrue(self.origen.is_file())
        self.assertTrue(Path(ubicacion.ruta_absoluta).is_file())
        self.assertFalse(Path(ubicacion.ruta_relativa).is_absolute())
        self.assertTrue(ubicacion.ruta_relativa.startswith("sources/"))
        self.assertEqual(Path(ubicacion.ruta_absoluta).read_bytes(), b"video")

    def test_mover_retira_el_original(self):
        temporal = self.base / "descargado.mp4"
        temporal.write_bytes(b"video")
        self.almacen.incorporar(str(temporal), mover=True)
        self.assertFalse(temporal.exists())

    def test_el_nombre_visible_se_reduce_a_un_componente_valido(self):
        for nombre in ("CON.mp4", "a" * 300 + ".mp4", "../fuera.mp4", "raro:flujo.mp4",
                       "termina.  ", ""):
            with self.subTest(nombre=nombre):
                ubicacion = self.almacen.incorporar(str(self.origen), nombre)
                hijo = Path(ubicacion.ruta_relativa).name
                self.assertNotIn("..", hijo)
                self.assertLessEqual(len(hijo), 128)
                self.assertTrue(Path(ubicacion.ruta_absoluta).is_file())

    def test_una_entrada_inexistente_se_rechaza_antes_de_tocar_nada(self):
        with self.assertRaises(ErrorFuenteIngesta):
            self.almacen.incorporar(str(self.base / "no-existe.mp4"))
        with self.assertRaises(ErrorFuenteIngesta):
            self.almacen.normalizar(str(self.base / "no-existe.mp4"))
        self.assertEqual(list((self.raiz / "sources").iterdir()), [])

    def test_referenciar_normaliza_sin_copiar(self):
        ubicacion = self.almacen.referenciar(str(self.base / "." / "clip original.mp4"))
        self.assertIsNone(ubicacion.ruta_relativa)
        self.assertEqual(Path(ubicacion.ruta_absoluta), self.origen.resolve())
        self.assertEqual(list((self.raiz / "sources").iterdir()), [])

    def test_retirar_solo_alcanza_lo_que_vive_dentro_del_proyecto(self):
        ubicacion = self.almacen.incorporar(str(self.origen))
        self.almacen.retirar(ubicacion.ruta_relativa)
        self.assertFalse(Path(ubicacion.ruta_absoluta).exists())
        for escapada in ("../fuera.mp4", "/etc/passwd", "sources/../../fuera.mp4"):
            with self.subTest(escapada=escapada):
                with self.assertRaises(ErrorFuenteIngesta):
                    self.almacen.retirar(escapada)

    def test_disponible_sigue_a_los_bytes_y_no_a_la_fila(self):
        ubicacion = self.almacen.incorporar(str(self.origen))
        fuente = SourceAsset(id="s", nombre="c", modo=ModoFuente.COPIADA,
                             origen=OrigenFuente.LOCAL,
                             huella=HuellaFuente(tamano=5, parcial="p", completa="c"),
                             ruta_relativa=ubicacion.ruta_relativa)
        self.assertTrue(self.almacen.disponible(fuente))
        Path(ubicacion.ruta_absoluta).unlink()
        self.assertFalse(self.almacen.disponible(fuente))

    # -- la barrera de directorio es parte de la transaccion --------------- #

    def fsync_roto(self):
        """`sincronizar_directorio` falla, como en la inyeccion del review."""
        def fallar(_ruta):
            raise OSError(5, "fsync fault")

        return unittest.mock.patch.object(fuentes_mod, "sincronizar_directorio", fallar)

    def sources(self):
        return sorted(hijo.name for hijo in (self.raiz / "sources").iterdir())

    def parciales(self):
        ingest = self.raiz / "cache" / "ingest"
        return sorted(hijo.name for hijo in ingest.iterdir()) if ingest.is_dir() else []

    def test_un_fallo_de_fsync_tras_el_rename_no_deja_fuente_huerfana(self):
        """Regresion del P1: el rename ya ocurrio y el metodo salia por excepcion.

        Nadie creaba token de propiedad y `sources/` no se recorre al limpiar, de
        modo que el archivo final quedaba sin fila y sin forma de adopcion.
        """
        with self.fsync_roto():
            with self.assertRaises(ErrorFuenteIngesta) as capturado:
                self.almacen.incorporar(str(self.origen))
        self.assertNotIsInstance(capturado.exception, OSError, "el OSError no se filtra")
        self.assertIsInstance(capturado.exception.__cause__, OSError)
        self.assertEqual(self.sources(), [], "no queda ninguna fuente huerfana")
        self.assertEqual(self.parciales(), [], "tampoco el parcial de la copia")
        self.assertTrue(self.origen.is_file(), "la entrada original no se toca al copiar")

    def test_un_fallo_de_fsync_en_mover_restaura_el_origen(self):
        """Los bytes venian de un staging que el caller va a retirar: hay que devolverlos."""
        descargado = self.raiz / "cache" / "ingest" / "descargado.mp4"
        descargado.parent.mkdir(parents=True, exist_ok=True)
        descargado.write_bytes(b"video descargado")

        with self.fsync_roto():
            with self.assertRaises(ErrorFuenteIngesta):
                self.almacen.incorporar(str(descargado), mover=True)

        self.assertEqual(self.sources(), [], "no queda destino huerfano")
        self.assertTrue(descargado.is_file(), "el origen vuelve a su sitio")
        self.assertEqual(descargado.read_bytes(), b"video descargado")

    def entre_volumenes(self, externo):
        """Doble fiel de dos dispositivos: cruzar la frontera falla con `EXDEV`.

        No basta con decir `_mismo_volumen=False`, que es lo que hacia la version
        anterior de esta prueba: con ambos caminos en el mismo disco, el
        `os.replace` de la compensacion funcionaba y tapaba el defecto. Aqui
        cualquier rename que cruce falla como fallaria de verdad.
        """
        real = os.replace
        frontera = str(externo.resolve())

        def dentro(ruta):
            return str(Path(ruta).resolve()).startswith(frontera)

        def cruzado(origen, destino):
            if dentro(origen) != dentro(destino):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            return real(origen, destino)

        def mismo_volumen(_self, origen, destino):
            return dentro(origen) == dentro(destino)

        return _Contextos(unittest.mock.patch("os.replace", cruzado),
                          unittest.mock.patch.object(AlmacenFuentesProyecto, "_mismo_volumen",
                                                     mismo_volumen))

    def origen_externo(self, datos=b"video cruzado"):
        externo = self.base / "externo"
        externo.mkdir(exist_ok=True)
        ruta = externo / "descargado.mp4"
        ruta.write_bytes(datos)
        return externo, ruta

    def test_un_fallo_de_fsync_cross_volume_deja_el_origen_intacto(self):
        """Regresion del P1: el origen se borraba antes de confirmar el destino.

        Restaurarlo exigia un `os.replace` entre dispositivos —imposible por
        `EXDEV`—, asi que el original desaparecia y quedaba un huerfano en
        `sources/`, namespace que la limpieza no recorre.
        """
        externo, descargado = self.origen_externo()
        with self.entre_volumenes(externo), self.fsync_roto():
            with self.assertRaises(ErrorFuenteIngesta) as capturado:
                self.almacen.incorporar(str(descargado), mover=True)

        self.assertNotIsInstance(capturado.exception, OSError)
        self.assertIsInstance(capturado.exception.__cause__, OSError)
        self.assertTrue(descargado.is_file(), "el origen no se toco: nunca hubo que restaurarlo")
        self.assertEqual(descargado.read_bytes(), b"video cruzado")
        self.assertEqual(self.sources(), [], "no queda huerfano en sources/")
        self.assertEqual(self.parciales(), [])

    def test_cross_volume_correcto_retira_el_origen_solo_al_final(self):
        externo, descargado = self.origen_externo()
        orden = []
        real = fuentes_mod.sincronizar_directorio

        def espiar(ruta):
            orden.append(("barrera", descargado.is_file()))
            return real(ruta)

        with self.entre_volumenes(externo):
            with unittest.mock.patch.object(fuentes_mod, "sincronizar_directorio", espiar):
                ubicacion = self.almacen.incorporar(str(descargado), mover=True)

        self.assertIn(("barrera", True), orden,
                      "el origen seguia vivo cuando se confirmo el destino")
        self.assertFalse(descargado.exists(), "y se retira despues, ya con el destino a salvo")
        self.assertIsNone(ubicacion.origen_pendiente)
        self.assertEqual(Path(ubicacion.ruta_absoluta).read_bytes(), b"video cruzado")

    def test_si_el_origen_no_se_puede_retirar_se_reporta_sin_fingir_exito(self):
        """El destino esta confirmado: deshacerlo seria destruir trabajo bueno."""
        externo, descargado = self.origen_externo()
        real_unlink = Path.unlink

        def unlink_roto(self_ruta, *argumentos, **claves):
            if self_ruta == descargado:
                raise OSError(13, "permiso denegado")
            return real_unlink(self_ruta, *argumentos, **claves)

        with self.entre_volumenes(externo):
            with unittest.mock.patch.object(Path, "unlink", unlink_roto):
                ubicacion = self.almacen.incorporar(str(descargado), mover=True)

        self.assertEqual(ubicacion.origen_pendiente, str(descargado))
        self.assertTrue(descargado.is_file(), "el original sigue ahi y se dice")
        self.assertEqual(Path(ubicacion.ruta_absoluta).read_bytes(), b"video cruzado")

    @unittest.skipUnless(VOLUMEN_AJENO is not None,
                         "requiere dos volumenes reales (WSL: /tmp y /mnt/c)")
    def test_un_fallo_de_fsync_entre_volumenes_reales_deja_el_origen_intacto(self):
        """Sin fingir dispositivos: origen y proyecto en `st_dev` distintos."""
        with tempfile.TemporaryDirectory(dir=str(VOLUMEN_AJENO)) as ajeno:
            descargado = Path(ajeno) / "real.mp4"
            descargado.write_bytes(b"cross-volume-source")
            self.assertNotEqual(descargado.stat().st_dev, self.raiz.stat().st_dev,
                                "el escenario exige volumenes de verdad distintos")
            with self.fsync_roto():
                with self.assertRaises(ErrorFuenteIngesta):
                    self.almacen.incorporar(str(descargado), mover=True)
            self.assertTrue(descargado.is_file())
            self.assertEqual(descargado.read_bytes(), b"cross-volume-source")
        self.assertEqual(self.sources(), [])

    def test_un_fallo_de_rename_se_traduce_y_no_deja_parcial(self):
        real = os.replace

        def fallar(origen, destino):
            if str(destino).startswith(str(self.raiz / "sources")):
                raise OSError(13, "rename fault")
            return real(origen, destino)

        with unittest.mock.patch("os.replace", fallar):
            with self.assertRaises(ErrorFuenteIngesta) as capturado:
                self.almacen.incorporar(str(self.origen))
        self.assertIsInstance(capturado.exception.__cause__, OSError)
        self.assertEqual(self.sources(), [])
        self.assertEqual(self.parciales(), [], "el parcial se retira al fallar el rename")

    def test_un_rollback_que_tampoco_puede_sincronizar_no_tapa_el_error(self):
        """La compensacion es el mejor esfuerzo: lo que se cuenta es el fallo original."""
        with self.fsync_roto():
            with self.assertRaises(ErrorFuenteIngesta) as capturado:
                self.almacen.incorporar(str(self.origen))
        self.assertIn("confirmar la incorporacion", str(capturado.exception))
        self.assertEqual(self.sources(), [])

    def test_tras_compensar_la_limpieza_no_encuentra_nada_pendiente(self):
        with self.fsync_roto():
            with self.assertRaises(ErrorFuenteIngesta):
                self.almacen.incorporar(str(self.origen))
        self.assertEqual(self.almacen.limpiar_abandonados(antiguedad=0.0), ())
        self.assertEqual(self.sources(), [])

    def test_una_incorporacion_correcta_confirma_el_directorio(self):
        vistas = []
        real = fuentes_mod.sincronizar_directorio

        def espiar(ruta):
            vistas.append(Path(ruta).name)
            return real(ruta)

        with unittest.mock.patch.object(fuentes_mod, "sincronizar_directorio", espiar):
            self.almacen.incorporar(str(self.origen))
        self.assertIn("sources", vistas, "la barrera forma parte del protocolo")

    def test_el_staging_se_retira_siempre(self):
        with self.almacen.staging() as temporal:
            Path(temporal, "parcial.bin").write_bytes(b"x")
            guardado = Path(temporal)
        self.assertFalse(guardado.exists())

    def envejecer(self, *rutas, segundos=10_000):
        antiguo = time.time() - segundos
        for ruta in rutas:
            for hijo in ([ruta, *ruta.rglob("*")] if ruta.is_dir() else [ruta]):
                os.utime(hijo, (antiguo, antiguo))

    def staging_propio(self):
        """Directorio con la gramatica y el marcador que crea `staging()`."""
        with self.almacen.staging() as temporal:
            destino = Path(temporal)
            (destino / "parcial.bin").write_bytes(b"x")
            copia = self.base / destino.name
            shutil.copytree(destino, copia)
        shutil.copytree(copia, destino)
        shutil.rmtree(copia)
        return destino

    def test_los_parciales_no_viven_en_sources_y_no_comparten_namespace(self):
        """El hallazgo: `sources/*.tmp` confundia una fuente con un residuo."""
        registrados = []
        real_copiar = shutil.copyfile

        def espiar(origen, destino, *argumentos, **claves):
            registrados.append(Path(destino))
            return real_copiar(origen, destino, *argumentos, **claves)

        with unittest.mock.patch("shutil.copyfile", espiar):
            self.almacen.incorporar(str(self.origen))
        self.assertEqual(len(registrados), 1)
        parcial = registrados[0]
        self.assertEqual(parcial.parent, self.raiz / "cache" / "ingest")
        self.assertTrue(parcial.name.startswith(PREFIJO_PARCIAL))
        self.assertTrue(parcial.name.endswith(SUFIJO_PARCIAL))
        self.assertEqual([hijo.suffix for hijo in (self.raiz / "sources").iterdir()], [".mp4"])

    def test_la_limpieza_jamas_borra_una_fuente_publicada_aunque_se_llame_tmp(self):
        """Regresion del P0: `capture.tmp` es un nombre legitimo de fuente."""
        publicada = self.almacen.incorporar(str(self.origen), "capture.tmp")
        ruta = Path(publicada.ruta_absoluta)
        otra = self.raiz / "sources" / "suelta.part"
        otra.write_bytes(b"x")
        self.envejecer(ruta, otra)

        self.assertEqual(self.almacen.limpiar_abandonados(antiguedad=3600.0), ())
        self.assertTrue(ruta.is_file(), "una fuente publicada no es un temporal")
        self.assertTrue(otra.is_file(), "`sources/` no se recorre nunca")

    def test_la_limpieza_no_toca_hijos_ajenos_de_cache_ingest(self):
        ajeno_dir = self.raiz / "cache" / "ingest" / "foreign-attempt"
        (ajeno_dir / "dentro").mkdir(parents=True)
        ajeno_archivo = self.raiz / "cache" / "ingest" / "notas.part"
        ajeno_archivo.parent.mkdir(parents=True, exist_ok=True)
        ajeno_archivo.write_bytes(b"x")
        imitador = self.raiz / "cache" / "ingest" / f"{PREFIJO_STAGING}{'0' * 32}"
        imitador.mkdir(parents=True)
        (imitador / "dentro.bin").write_bytes(b"x")  # nombre correcto, sin marcador
        self.envejecer(ajeno_dir, ajeno_archivo, imitador)

        self.assertEqual(self.almacen.limpiar_abandonados(antiguedad=3600.0), ())
        self.assertTrue(ajeno_dir.is_dir(), "un directorio ajeno se queda por viejo que sea")
        self.assertTrue(ajeno_archivo.is_file(), "`notas.part` no encaja en la gramatica")
        self.assertTrue(imitador.is_dir(), "sin marcador no es nuestro")

    def test_la_limpieza_retira_staging_y_parciales_propios_ya_abandonados(self):
        staging = self.staging_propio()
        parcial = self.raiz / "cache" / "ingest" / f"{PREFIJO_PARCIAL}{'a' * 32}{SUFIJO_PARCIAL}"
        parcial.write_bytes(b"x")
        self.envejecer(staging, parcial)

        retirados = self.almacen.limpiar_abandonados(antiguedad=3600.0)
        self.assertEqual(retirados, (f"cache/ingest/{parcial.name}",
                                     f"cache/ingest/{staging.name}"))
        self.assertFalse(staging.exists())
        self.assertFalse(parcial.exists())

    def test_la_antiguedad_se_mide_por_la_actividad_real_del_arbol(self):
        """Un staging con el contenedor viejo y contenido fresco sigue en uso."""
        staging = self.staging_propio()
        self.envejecer(staging)
        os.utime(staging / "parcial.bin", None)  # acaba de escribirse un bloque

        self.assertEqual(self.almacen.limpiar_abandonados(antiguedad=3600.0), ())
        self.assertTrue(staging.is_dir())

    def test_un_staging_reciente_no_se_retira(self):
        staging = self.staging_propio()
        self.assertEqual(self.almacen.limpiar_abandonados(antiguedad=3600.0), ())
        self.assertTrue(staging.is_dir())


# --------------------------------------------------------------------------- #
# Almacen de artefactos
# --------------------------------------------------------------------------- #

class AlmacenArtefactosIngestaTests(BaseTemporal):
    def setUp(self):
        super().setUp()
        self.raiz = self.proyecto()
        self.almacen = AlmacenArtefactosIngestaProyecto(self.raiz)

    def preparar(self, datos=b'{"a": 1}'):
        directorio = self.almacen.preparar(CLAVE)
        archivo = self.almacen.escribir(directorio, "probe.json", datos)
        return directorio, archivo

    def test_escribir_declara_tamano_y_digest_de_lo_escrito(self):
        _directorio, archivo = self.preparar(b"12345")
        self.assertEqual(archivo.bytes, 5)
        self.assertTrue(archivo.sha256.startswith("sha256:"))

    def test_publica_y_deja_el_artefacto_bajo_su_clave(self):
        directorio, archivo = self.preparar()
        manifiesto = ManifiestoArtefacto(tipo="probe", archivos=(archivo,), linaje=LINAJE)
        publicados = self.almacen.publicar(CLAVE, directorio, manifiesto)
        self.assertEqual(publicados, (f"artifacts/{CLAVE}/probe.json",))
        self.assertEqual(self.almacen.presentes(CLAVE),
                         {"probe.json": archivo.sha256.split(":", 1)[1]})
        self.assertEqual(self.almacen.leer_documento(CLAVE, "probe.json"), {"a": 1})
        self.assertFalse(Path(directorio).exists(), "el staging se retira al publicar")

    def test_los_presentes_no_incluyen_el_sidecar_de_identidad(self):
        directorio, archivo = self.preparar()
        self.almacen.publicar(CLAVE, directorio,
                              ManifiestoArtefacto(tipo="probe", archivos=(archivo,), linaje=LINAJE))
        self.assertTrue((self.raiz / "artifacts" / CLAVE / NOMBRE_PUBLICACION).is_file())
        self.assertNotIn(NOMBRE_PUBLICACION, self.almacen.presentes(CLAVE))

    def test_un_checksum_que_no_cuadra_impide_publicar_nada(self):
        directorio, archivo = self.preparar()
        Path(directorio, "probe.json").write_bytes(b'{"a": 2}')  # los bytes cambian tras declararse
        manifiesto = ManifiestoArtefacto(
            tipo="probe", archivos=(ArchivoArtefacto("probe.json", 8, archivo.sha256),),
            linaje=LINAJE)
        with self.assertRaises(ErrorArtefactoIngesta):
            self.almacen.publicar(CLAVE, directorio, manifiesto)
        self.assertFalse((self.raiz / "artifacts" / CLAVE).exists())
        self.assertTrue(Path(directorio, "probe.json").is_file(),
                        "el staging se conserva para diagnostico")

    def test_un_linaje_que_no_es_de_esta_clave_no_se_publica(self):
        directorio, archivo = self.preparar()
        ajeno = LinajeArtefacto(stage="otra", version_contrato="1", version_proveedor="p",
                                clave=clave_materializacion(
                                    stage="otra", version_contrato="1", version_proveedor="p",
                                    entradas={"fuente": "z"}))
        with self.assertRaises(ErrorArtefactoIngesta):
            self.almacen.publicar(CLAVE, directorio, ManifiestoArtefacto(
                tipo="probe", archivos=(archivo,), linaje=ajeno))
        self.assertFalse((self.raiz / "artifacts" / CLAVE).exists())

    def test_republicar_lo_mismo_adopta_en_vez_de_fallar(self):
        """Es el caso de morir entre el rename y el commit: hay que poder retomarlo."""
        directorio, archivo = self.preparar()
        manifiesto = ManifiestoArtefacto(tipo="probe", archivos=(archivo,), linaje=LINAJE)
        self.almacen.publicar(CLAVE, directorio, manifiesto)
        segundo, archivo2 = self.preparar()
        publicados = self.almacen.publicar(
            CLAVE, segundo, ManifiestoArtefacto(tipo="probe", archivos=(archivo2,), linaje=LINAJE))
        self.assertEqual(publicados, (f"artifacts/{CLAVE}/probe.json",))

    def test_republicar_bytes_distintos_bajo_la_misma_clave_no_sobrescribe(self):
        directorio, archivo = self.preparar()
        self.almacen.publicar(CLAVE, directorio,
                              ManifiestoArtefacto(tipo="probe", archivos=(archivo,), linaje=LINAJE))
        segundo, otro = self.preparar(b'{"a": 99}')
        with self.assertRaises(ErrorArtefactoIngesta):
            self.almacen.publicar(CLAVE, segundo, ManifiestoArtefacto(
                tipo="probe", archivos=(otro,), linaje=LINAJE))
        self.assertEqual(self.almacen.leer_documento(CLAVE, "probe.json"), {"a": 1})

    def test_una_clave_invalida_nunca_llega_a_componer_una_ruta(self):
        for invalida in ("../fuera", "mk1-no-hex", "", "C:/x"):
            with self.subTest(invalida=invalida):
                with self.assertRaises(ErrorArtefactoIngesta):
                    self.almacen.preparar(invalida)

    def envejecer(self, *rutas, segundos=10_000):
        antiguo = time.time() - segundos
        for ruta in rutas:
            for hijo in ([ruta, *ruta.rglob("*")] if ruta.is_dir() else [ruta]):
                os.utime(hijo, (antiguo, antiguo))

    def test_una_cuarentena_real_sobrevive_al_barrido_por_vieja_que_sea(self):
        """Probe 5 del review: la evidencia vivia justo donde el barrido miraba.

        Se construye la cuarentena con el ArtifactStore, no imitando un nombre:
        `preparar` estrena el intento y `cuarentena` lo preserva en sitio, que es
        exactamente el estado que un sondeo fallido deja. Envejecerlo mas alla
        del umbral es lo que antes bastaba para perderla.
        """
        almacen = AlmacenArtefactosProyecto(self.raiz)
        intento = Path(almacen.preparar(CLAVE, "sondeo"))
        (intento / "evidencia.bin").write_bytes(b"por que fallo el sondeo")
        preservado = almacen.cuarentena(str(intento))
        self.assertEqual(preservado, str(intento), "la cuarentena preserva en sitio")
        self.envejecer(intento)

        retirados = self.almacen.limpiar_abandonados(antiguedad=3600.0)

        self.assertEqual(retirados, (), "la ingesta ya no barre nada bajo `cache/jobs/`")
        self.assertEqual((intento / "evidencia.bin").read_bytes(), b"por que fallo el sondeo")

    def test_la_limpieza_de_artefactos_no_retira_ningun_intento(self):
        directorio, _archivo = self.preparar()
        job = self.raiz / "cache" / "jobs" / str(uuid.uuid4())
        (job / "intento").mkdir(parents=True)
        self.envejecer(Path(directorio), job / "intento")

        self.assertEqual(self.almacen.limpiar_abandonados(antiguedad=3600.0), ())
        self.assertTrue(Path(directorio).is_dir(), "un sondeo viejo es evidencia, no basura")
        self.assertTrue((job / "intento").is_dir(), "un intento de T03 tampoco se toca")

    # -- ventana entre validar y publicar --------------------------------- #

    def test_una_mutacion_tras_mover_al_staging_de_publicacion_no_se_publica(self):
        """Mutante focal en la ventana que el hallazgo P1 describia.

        `sincronizar_archivo` corre sobre los archivos ya movidos al staging
        privado y **antes** de medirlos. Mutar ahi reproduce exactamente la
        escritura que antes se colaba entre el checksum y el `rename`: ahora cae
        dentro de lo que se digiere, y el checksum declarado la delata.
        """
        directorio, archivo = self.preparar(b'{"a": 1}')
        manifiesto = ManifiestoArtefacto(tipo="probe", archivos=(archivo,), linaje=LINAJE)
        real = artefactos_jobs.sincronizar_archivo

        def mutar_y_sincronizar(ruta):
            Path(ruta).write_bytes(b'{"a": 2}')
            return real(ruta)

        with unittest.mock.patch.object(artefactos_jobs, "sincronizar_archivo",
                                        mutar_y_sincronizar):
            with self.assertRaises(ErrorArtefactoIngesta):
                self.almacen.publicar(CLAVE, directorio, manifiesto)
        self.assertFalse((self.raiz / "artifacts" / CLAVE).exists(),
                         "no se publica nada que contradiga su propio checksum")

    def test_un_fallo_de_publicacion_devuelve_los_archivos_al_intento(self):
        """La cuarentena solo sirve si conserva lo que la etapa produjo."""
        directorio, archivo = self.preparar(b'{"a": 1}')
        mentiroso = ArchivoArtefacto("probe.json", archivo.bytes, "ab" * 32)
        with self.assertRaises(ErrorArtefactoIngesta):
            self.almacen.publicar(CLAVE, directorio, ManifiestoArtefacto(
                tipo="probe", archivos=(mentiroso,), linaje=LINAJE))
        self.assertEqual(Path(directorio, "probe.json").read_bytes(), b'{"a": 1}',
                         "el intento conserva su salida para poder diagnosticarla")
        self.assertFalse((self.raiz / "artifacts" / CLAVE).exists())
        self.assertFalse((self.raiz / "artifacts" / f"{CLAVE}.publicando").exists())

    def test_una_mutacion_dentro_de_sincronizar_arbol_no_se_publica(self):
        """El mutante del review, ahora anterior al digest y por tanto detectado.

        `sincronizar_arbol` es la ultima operacion que recorre los archivos; el
        digest va despues. Sobrescribir ahi —mismo tamano— cae dentro de lo que
        se digiere y el checksum declarado lo delata.
        """
        directorio, archivo = self.preparar(b'{"value":"AAAA"}')
        manifiesto = ManifiestoArtefacto(tipo="probe", archivos=(archivo,), linaje=LINAJE)
        real = artefactos_jobs.sincronizar_arbol

        def mutar_y_sincronizar(raiz):
            for hijo in Path(raiz).rglob("probe.json"):
                hijo.write_bytes(b'{"value":"BBBB"}')
            return real(raiz)

        with unittest.mock.patch.object(artefactos_jobs, "sincronizar_arbol",
                                        mutar_y_sincronizar):
            with self.assertRaises(ErrorArtefactoIngesta):
                self.almacen.publicar(CLAVE, directorio, manifiesto)
        self.assertFalse((self.raiz / "artifacts" / CLAVE).exists())
        self.assertEqual(Path(directorio, "probe.json").read_bytes(), b'{"value":"AAAA"}',
                         "el intento conserva su salida")

    def test_un_handle_retenido_por_el_productor_no_altera_lo_publicado(self):
        """Publicar copia: el descriptor del worker apunta a otro inode."""
        directorio, archivo = self.preparar(b'{"value":"AAAA"}')
        manifiesto = ManifiestoArtefacto(tipo="probe", archivos=(archivo,), linaje=LINAJE)
        retenido = open(Path(directorio, "probe.json"), "r+b")
        self.addCleanup(retenido.close)

        self.almacen.publicar(CLAVE, directorio, manifiesto)
        retenido.seek(0)
        retenido.write(b'{"value":"BBBB"}')
        retenido.flush()

        publicado = self.raiz / "artifacts" / CLAVE / "probe.json"
        self.assertEqual(publicado.read_bytes(), b'{"value":"AAAA"}')
        self.assertEqual(self.almacen.presentes(CLAVE),
                         {"probe.json": archivo.sha256.split(":", 1)[1]},
                         "los bytes publicados siguen coincidiendo con su sidecar")

    def stagings_observados(self, intentos):
        """Nombres del staging de publicacion en `intentos` publicaciones de la misma clave."""
        vistos = []
        real = artefactos_jobs.copiar_sellado

        def espiar(origen, destino):
            vistos.append(Path(destino).parent.name)
            return real(origen, destino)

        with unittest.mock.patch.object(artefactos_jobs, "copiar_sellado", espiar):
            for _ in range(intentos):
                directorio, archivo = self.preparar()
                # Cada intento falla en la validacion, de modo que `final` nunca
                # existe y los dos sellados compiten por el mismo destino: es la
                # unica forma de observar si su nombre se puede predecir.
                mentiroso = ArchivoArtefacto("probe.json", archivo.bytes, "ab" * 32)
                with self.assertRaises(ErrorArtefactoIngesta):
                    self.almacen.publicar(CLAVE, directorio, ManifiestoArtefacto(
                        tipo="probe", archivos=(mentiroso,), linaje=LINAJE))
        return vistos

    def test_dos_sellados_de_la_misma_clave_no_comparten_ruta(self):
        """Con nombre deterministico, el productor sabe exactamente a que apuntar."""
        vistos = self.stagings_observados(2)
        self.assertEqual(len(vistos), 2)
        self.assertNotEqual(vistos[0], vistos[1],
                            "el staging de publicacion no puede derivarse del job")

    def test_el_staging_de_publicacion_lleva_prefijo_reconocible_y_sufijo_aleatorio(self):
        (nombre,) = self.stagings_observados(1)
        tronco, separador, aleatorio = nombre.partition(".publicando-")
        self.assertEqual(tronco, CLAVE)
        self.assertTrue(separador)
        self.assertEqual(len(aleatorio), 32)
        self.assertFalse((self.raiz / "artifacts" / f"{CLAVE}.publicando").exists(),
                         "el nombre deterministico ya no se usa")

    def sellado(self, sufijo="0" * 32, clave=None, con_marcador=True, vivo=False,
                payload=None):
        """Crea un directorio con forma de sellado y el marcador que se decida.

        Devuelve `(ruta, posesion)`. `posesion` es `None` salvo cuando se pide
        uno vivo, en cuyo caso se retiene el **reclamo** de ese sellado —que es
        lo que define la propiedad desde F05— como lo tendria una publicacion en
        curso; la prueba lo suelta al terminar.
        """
        destino = self.raiz / "artifacts" / f"{clave or CLAVE}.publicando-{sufijo}"
        destino.mkdir(parents=True)
        (destino / "probe.json").write_bytes(b"a medias")
        if con_marcador:
            if payload is None:
                artefactos_jobs.escribir_marca(destino / NOMBRE_SELLADO, clave or CLAVE)
            else:
                (destino / NOMBRE_SELLADO).write_bytes(payload)
        posesion = None
        if vivo:
            posesion = self.reclamo(sufijo)
            self.assertTrue(posesion is not None and posesion.tomado)
            self.addCleanup(posesion.soltar)
        return destino, posesion

    def publicar_bien(self, clave=None, linaje=None):
        directorio, archivo = self.preparar()
        return self.almacen.publicar(
            clave or CLAVE, directorio,
            ManifiestoArtefacto(tipo="probe", archivos=(archivo,), linaje=linaje or LINAJE))

    # -- recuperacion no destructiva -------------------------------------- #

    def residuo(self, sufijo, clave=None, con_marcador=True, carga=b"ORIGINAL"):
        """Deja en disco un sellado abandonado, como lo dejaria un corte."""
        destino = self.raiz / "artifacts" / f"{clave or CLAVE}.publicando-{sufijo}"
        destino.mkdir(parents=True)
        (destino / "payload.bin").write_bytes(carga)
        if con_marcador:
            artefactos_jobs.escribir_marca(destino / NOMBRE_SELLADO, clave or CLAVE)
        return destino

    def test_un_residuo_abandonado_sobrevive_a_una_publicacion_nueva(self):
        """T04 no borra arboles que ya estaban en disco: los conserva.

        La decision esta escrita: Python no ofrece una cadena portable de
        operaciones relativas a handles para recorrer y borrar sin volver a
        resolver la ruta, y revalidar-y-borrar deja siempre una ventana. Se
        prefiere una fuga reconstruible a destruir algo que no se puede demostrar
        que sea lo medido.
        """
        abandonado = self.residuo("a" * 32)
        self.publicar_bien()

        self.assertTrue(abandonado.is_dir(), "un residuo preexistente no se retira")
        self.assertEqual((abandonado / "payload.bin").read_bytes(), b"ORIGINAL")
        self.assertEqual(self.almacen.leer_documento(CLAVE, "probe.json"), {"a": 1},
                         "y la publicacion nueva termina igual")

    def test_un_residuo_no_bloquea_ni_se_convierte_en_artefacto(self):
        abandonado = self.residuo("b" * 32)
        self.publicar_bien()

        self.assertNotIn("payload.bin", self.almacen.presentes(CLAVE),
                         "un residuo no aporta archivos a lo publicado")
        publicados = AlmacenArtefactosProyecto(self.raiz).publicados(CLAVE)
        self.assertTrue(all("publicando" not in ruta for ruta in publicados))
        self.assertTrue(abandonado.is_dir())

    def test_un_residuo_no_impide_adoptar_una_publicacion_valida(self):
        self.publicar_bien()
        self.residuo("c" * 32)
        # Republicar lo mismo adopta la publicacion final que ya existe.
        self.publicar_bien()
        self.assertEqual(self.almacen.leer_documento(CLAVE, "probe.json"), {"a": 1})

    def sustituir_tras_validar(self, momento):
        """Sustituye el arbol justo despues de la validacion que `momento` nombre.

        Son las dos sustituciones post-validacion que el review exige: la que
        antes ocurria al entrar al borrado de la reclamacion inicial, y la del
        barrido. Ya no existe ninguna de las dos rutas destructivas, asi que lo
        que se demuestra es que sustituir no provoca escritura ni borrado alguno.
        """
        original = self.residuo("d" * 32, carga=b"ORIGINAL")
        impostor = self.raiz / "artifacts" / f"{CLAVE}.publicando-{'e' * 32}"
        impostor.mkdir(parents=True)
        (impostor / "payload.bin").write_bytes(b"FOREIGN")
        artefactos_jobs.escribir_marca(impostor / NOMBRE_SELLADO, CLAVE)

        borrados, movidos = [], []
        real_rmtree, real_replace = shutil.rmtree, os.replace
        publicados = self.raiz / "artifacts"

        def espiar_rmtree(ruta, *argumentos, **claves):
            borrados.append(Path(ruta))
            return real_rmtree(ruta, *argumentos, **claves)

        def espiar_replace(origen, destino):
            movidos.append((Path(origen), Path(destino)))
            if momento == "publicar" and len(movidos) == 1:
                # La sustitucion clasica: el arbol validado se aparca y otro
                # ocupa su nombre justo despues de mirarlo.
                real_replace(original, self.raiz / "artifacts" / "aparcado")
                real_replace(impostor, original)
            return real_replace(origen, destino)

        with (unittest.mock.patch("shutil.rmtree", espiar_rmtree),
              unittest.mock.patch("os.replace", espiar_replace)):
            self.publicar_bien()
        return borrados, publicados

    def test_sustituir_un_residuo_tras_validar_no_borra_ni_escribe_fuera(self):
        for momento in ("inventario", "publicar"):
            with self.subTest(momento=momento):
                borrados, publicados = self.sustituir_tras_validar(momento)

                supervivientes = {ruta.read_bytes()
                                  for ruta in self.raiz.rglob("payload.bin")}
                self.assertIn(b"ORIGINAL", supervivientes)
                self.assertIn(b"FOREIGN", supervivientes)
                for ruta in borrados:
                    self.assertFalse(ruta == publicados or publicados in ruta.parents,
                                     f"la recuperacion no puede borrar en el area publicada: {ruta}")
                shutil.rmtree(self.raiz / "artifacts", ignore_errors=True)
                (self.raiz / "artifacts").mkdir(parents=True, exist_ok=True)

    def test_la_recuperacion_no_ejecuta_ningun_borrado_recursivo(self):
        """Ni un `rmtree` sobre algo descubierto en disco."""
        self.residuo("f" * 32)
        borrados = []
        real = shutil.rmtree

        def espiar(ruta, *argumentos, **claves):
            borrados.append(Path(ruta))
            return real(ruta, *argumentos, **claves)

        with unittest.mock.patch("shutil.rmtree", espiar):
            self.publicar_bien()

        cache = self.raiz / "cache"
        for ruta in borrados:
            self.assertTrue(cache in ruta.parents or ruta == cache,
                            f"solo se retira lo que esta invocacion posee en `cache/`: {ruta}")

    def test_el_almacen_ya_no_expone_ninguna_via_de_borrado_recuperador(self):
        """La garantia se comprueba en la forma, no solo en la conducta.

        Una prueba de conducta demuestra que *esta* ruta no borro nada; que no
        exista el metodo demuestra que no hay ninguna que pueda.
        """
        almacen = AlmacenArtefactosProyecto(self.raiz)
        for retirado in ("_reclamar", "_barrer_tumbas", "_retirar_stagings",
                         "_borrar_arbol", "reclamo"):
            self.assertFalse(hasattr(almacen, retirado),
                             f"{retirado} pertenecia al borrado automatico de residuos")

    def redirigir(self, ancestro, victima):
        """Sustituye `ancestro` por un enlace a `victima`. Junction en Windows."""
        victima.mkdir(parents=True, exist_ok=True)
        (victima / "FOREIGN.bin").write_bytes(b"FOREIGN")
        if ancestro.exists():
            shutil.rmtree(ancestro)
        if os.name == "nt":
            creado = subprocess.run(["cmd", "/c", "mklink", "/J", str(ancestro), str(victima)],
                                    capture_output=True, text=True).returncode == 0
        else:
            try:
                ancestro.symlink_to(victima, target_is_directory=True)
                creado = True
            except (OSError, NotImplementedError):
                creado = False
        if not creado:
            self.skipTest("este sistema no permite redirigir el ancestro")

    def test_sustituir_el_ancestro_en_preparar_no_borra_ni_escribe_fuera(self):
        """Probe del review: `preparar()` ya no retira ningun residuo previo.

        Mientras `preparar` borraba el intento anterior, redirigir un ancestro
        justo despues de validar la contencion llevaba ese `rmtree` fuera del
        proyecto. Ahora el nombre se estrena, de modo que no hay nada que
        retirar y la sustitucion no puede convertirse en un borrado.
        """
        almacen = AlmacenArtefactosProyecto(self.raiz)
        victima = self.base / "victima-preparar"
        trabajos = self.raiz / "cache" / "jobs"
        trabajos.mkdir(parents=True, exist_ok=True)
        self.redirigir(trabajos / CLAVE, victima)

        def preparar():
            # Con el ancestro redirigido, la ruta derivada cae fuera del
            # proyecto y la frontera de identidad se niega antes de tocar nada.
            with self.assertRaises(ErrorIdentificadorJob):
                almacen.preparar(CLAVE, "sondeo")

        borrados, movidos = self.espiar(preparar)

        self.assertEqual((borrados, movidos), ([], []),
                         "preparar no borra ni mueve nada preexistente")
        self.assertEqual((victima / "FOREIGN.bin").read_bytes(), b"FOREIGN")
        self.assertEqual(sorted(hijo.name for hijo in victima.iterdir()), ["FOREIGN.bin"],
                         "tampoco escribe fuera del proyecto")

    def test_la_cuarentena_conserva_en_sitio_y_no_toca_nada_externo(self):
        """Probe del review: la cuarentena ya no mueve a un destino predecible."""
        almacen = AlmacenArtefactosProyecto(self.raiz)
        intento = Path(almacen.preparar(CLAVE, "sondeo"))
        (intento / "ORIGINAL.bin").write_bytes(b"ORIGINAL")
        victima = self.base / "victima-cuarentena"
        # `_cuarentena` era el destino determinista al que se movia la evidencia.
        # Redirigirlo comprobaba que ese traslado no escapaba del proyecto; ahora
        # comprueba algo mas fuerte: que ya no se mira siquiera.
        self.redirigir(self.raiz / "cache" / "jobs" / "_cuarentena", victima)

        borrados, movidos = self.espiar(lambda: almacen.cuarentena(str(intento)))

        self.assertEqual((borrados, movidos), ([], []),
                         "la cuarentena no borra ni mueve: preserva")
        self.assertEqual((intento / "ORIGINAL.bin").read_bytes(), b"ORIGINAL")
        self.assertEqual((victima / "FOREIGN.bin").read_bytes(), b"FOREIGN")
        self.assertEqual(almacen.cuarentena(str(intento)), str(intento),
                         "y devuelve la ruta util para diagnosticar")

    def espiar(self, accion):
        """Ejecuta `accion` registrando todo borrado recursivo y todo rename."""
        borrados, movidos = [], []
        real_rmtree, real_replace = shutil.rmtree, os.replace

        def espiar_rmtree(ruta, *argumentos, **claves):
            borrados.append(Path(ruta))
            return real_rmtree(ruta, *argumentos, **claves)

        def espiar_replace(origen, destino):
            movidos.append((Path(origen), Path(destino)))
            return real_replace(origen, destino)

        with (unittest.mock.patch("shutil.rmtree", espiar_rmtree),
              unittest.mock.patch("os.replace", espiar_replace)):
            accion()
        return borrados, movidos

    def test_un_residuo_previo_no_bloquea_ni_es_borrado_por_un_intento_nuevo(self):
        almacen = AlmacenArtefactosProyecto(self.raiz)
        previo = self.raiz / "cache" / "jobs" / CLAVE / "sondeo"
        previo.mkdir(parents=True)
        (previo / "ORIGINAL.bin").write_bytes(b"ORIGINAL")

        nuevo = Path(almacen.preparar(CLAVE, "sondeo"))
        self.assertTrue(nuevo.is_dir())
        self.assertNotEqual(nuevo, previo)
        self.assertEqual((previo / "ORIGINAL.bin").read_bytes(), b"ORIGINAL",
                         "el residuo previo no se retira para hacer sitio")

    def test_descartar_solo_alcanza_lo_que_esta_instancia_preparo(self):
        almacen = AlmacenArtefactosProyecto(self.raiz)
        ajeno = self.raiz / "cache" / "jobs" / CLAVE / "sondeo-" + "" if False else \
            self.raiz / "cache" / "jobs" / CLAVE / ("sondeo-" + "0" * 32)
        ajeno.mkdir(parents=True)
        (ajeno / "ORIGINAL.bin").write_bytes(b"ORIGINAL")

        almacen.descartar(str(ajeno))
        self.assertTrue(ajeno.is_dir(), "una ruta que esta instancia no creo se preserva")

        propio = Path(almacen.preparar(CLAVE, "sondeo"))
        almacen.descartar(str(propio))
        self.assertFalse(propio.exists(), "lo que si creo se retira igual que antes")

    # -- inventario -------------------------------------------------------- #

    def test_el_inventario_describe_los_residuos_sin_tocarlos(self):
        propio = self.residuo("1" * 32)
        ajeno = self.raiz / "artifacts" / f"{CLAVE}.publicando-{'2' * 32}"
        ajeno.mkdir(parents=True)
        (ajeno / "user.txt").write_bytes(b"trabajo de otro")

        almacen = AlmacenArtefactosProyecto(self.raiz)
        antes = sorted(ruta.name for ruta in (self.raiz / "artifacts").rglob("*"))
        inventario = {Path(registro["ruta"]).name: registro
                      for registro in almacen.residuos()}

        self.assertIn(propio.name, inventario)
        self.assertTrue(inventario[propio.name]["marcador_propio"])
        self.assertIn(ajeno.name, inventario)
        self.assertFalse(inventario[ajeno.name]["marcador_propio"],
                         "sin marcador propio se lista, pero no se declara nuestro")
        self.assertEqual(sorted(ruta.name for ruta in (self.raiz / "artifacts").rglob("*")),
                         antes, "el inventario solo lee")

    def test_el_inventario_no_confunde_una_publicacion_con_un_residuo(self):
        self.publicar_bien()
        almacen = AlmacenArtefactosProyecto(self.raiz)
        self.assertEqual(almacen.residuos(), (), "lo publicado no es un residuo")
        self.assertTrue(almacen.publicados(CLAVE))

    def test_el_inventario_se_puede_acotar_a_un_job(self):
        self.residuo("3" * 32)
        otra = clave_materializacion(stage="otro", version_contrato="1",
                                     version_proveedor="p", entradas={"fuente": "z"})
        self.residuo("4" * 32, clave=otra)

        almacen = AlmacenArtefactosProyecto(self.raiz)
        self.assertEqual(len(almacen.residuos()), 2)
        self.assertEqual([registro["job_id"] for registro in almacen.residuos(CLAVE)], [CLAVE])

    def test_el_marcador_de_propiedad_no_forma_parte_de_lo_publicado(self):
        self.publicar_bien()
        publicado = self.raiz / "artifacts" / CLAVE
        self.assertFalse((publicado / NOMBRE_SELLADO).exists())
        self.assertEqual(set(self.almacen.presentes(CLAVE)), {"probe.json"})

    def test_un_fallo_de_publicacion_no_deja_el_marcador_bloqueado(self):
        """En Windows un archivo abierto no se borra: el residuo sobreviviria."""
        directorio, archivo = self.preparar()
        mentiroso = ArchivoArtefacto("probe.json", archivo.bytes, "ab" * 32)
        with self.assertRaises(ErrorArtefactoIngesta):
            self.almacen.publicar(CLAVE, directorio, ManifiestoArtefacto(
                tipo="probe", archivos=(mentiroso,), linaje=LINAJE))
        residuos = [hijo for hijo in (self.raiz / "artifacts").iterdir()
                    if hijo.name.startswith(f"{CLAVE}.publicando")]
        self.assertEqual(residuos, [], "el sellado fallido se retira entero")

    def test_publicar_no_consume_la_salida_del_intento(self):
        directorio, archivo = self.preparar()
        self.almacen.publicar(CLAVE, directorio,
                              ManifiestoArtefacto(tipo="probe", archivos=(archivo,), linaje=LINAJE))
        self.assertFalse(Path(directorio).exists(),
                         "el staging del intento se descarta al terminar bien")

    # -- lectura verificada ----------------------------------------------- #

    def test_leer_verificado_devuelve_los_bytes_que_acaba_de_comprobar(self):
        directorio, archivo = self.preparar()
        self.almacen.publicar(CLAVE, directorio,
                              ManifiestoArtefacto(tipo="probe", archivos=(archivo,), linaje=LINAJE))
        self.assertEqual(self.almacen.leer_verificado(CLAVE, "probe.json", archivo.sha256),
                         {"a": 1})

    def test_leer_verificado_rechaza_bytes_que_ya_no_coinciden(self):
        """Cierra la segunda ventana: comprobar y volver a abrir eran dos lecturas."""
        directorio, archivo = self.preparar()
        self.almacen.publicar(CLAVE, directorio,
                              ManifiestoArtefacto(tipo="probe", archivos=(archivo,), linaje=LINAJE))
        (self.raiz / "artifacts" / CLAVE / "probe.json").write_bytes(b'{"a": 99}')
        with self.assertRaises(ErrorArtefactoIngesta):
            self.almacen.leer_verificado(CLAVE, "probe.json", archivo.sha256)

    def test_leer_verificado_exige_un_checksum_declarado(self):
        directorio, archivo = self.preparar()
        self.almacen.publicar(CLAVE, directorio,
                              ManifiestoArtefacto(tipo="probe", archivos=(archivo,), linaje=LINAJE))
        with self.assertRaises(ErrorArtefactoIngesta):
            self.almacen.leer_verificado(CLAVE, "probe.json", "")


class ValidacionConChecksumTests(BaseTemporal):
    """La verificacion de digests declarados vive en la unica puerta comun.

    `validar` la atraviesan tanto un job de T03 desde el coordinador como una
    ingesta desde su almacen; ponerla en cualquier otro sitio dejaria un camino
    por el que se publica sin comprobar nada.
    """

    def setUp(self):
        super().setUp()
        self.raiz = self.proyecto()
        self.almacen = AlmacenArtefactosProyecto(self.raiz)
        self.directorio = self.almacen.preparar("trabajo", "intento")
        Path(self.directorio, "salida.json").write_bytes(b"contenido")
        self.digest = digest_archivo(Path(self.directorio, "salida.json"))

    def manifiesto(self, metadata):
        return ManifiestoSalida(archivos=(ArchivoSalida("salida.json", 9),), metadata=metadata)

    def test_un_manifiesto_sin_checksums_se_valida_como_siempre(self):
        self.almacen.validar(self.directorio, self.manifiesto({}))

    def test_un_checksum_correcto_pasa_con_o_sin_etiqueta(self):
        for declarado in (self.digest, f"sha256:{self.digest}"):
            with self.subTest(declarado=declarado):
                self.almacen.validar(self.directorio,
                                     self.manifiesto({"checksums": {"salida.json": declarado}}))

    def test_un_checksum_que_no_cuadra_detiene_la_validacion(self):
        with self.assertRaises(ErrorValidacionSalida):
            self.almacen.validar(self.directorio,
                                 self.manifiesto({"checksums": {"salida.json": "ab" * 32}}))

    def test_declarar_el_checksum_de_algo_que_no_se_publica_es_un_error(self):
        with self.assertRaises(ErrorValidacionSalida):
            self.almacen.validar(
                self.directorio,
                self.manifiesto({"checksums": {"salida.json": self.digest,
                                               "fantasma.json": "ab" * 32}}))

    def test_un_documento_de_checksums_ilegible_no_se_ignora(self):
        for roto in ("no-es-objeto", {"salida.json": 5}, {"../fuera.json": "ab" * 32}):
            with self.subTest(roto=roto):
                with self.assertRaises(ErrorValidacionSalida):
                    self.almacen.validar(self.directorio, self.manifiesto({"checksums": roto}))


# --------------------------------------------------------------------------- #
# Descargador
# --------------------------------------------------------------------------- #

#: Hijo local que escribe una linea de progreso y despues se queda esperando.
#: Sustituye a yt-dlp sin red: lo que se prueba es el ciclo de cancelacion, no
#: el descargador remoto.
GUION_LARGO = (
    "import sys, time\n"
    "sys.stdout.write('[download]   1.0%\\n'); sys.stdout.flush()\n"
    "time.sleep(120)\n"
)
#: Igual, pero callado: reproduce el hijo que aun no ha emitido nada cuando
#: llega la cancelacion.
GUION_CALLADO = "import time\ntime.sleep(120)\n"
#: Ignora la parada cooperativa. Solo tiene sentido en POSIX: en Windows
#: `terminate()` es `TerminateProcess` y no se puede desatender.
GUION_SORDO = (
    "import signal, sys, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "sys.stdout.write('[download]   1.0%\\n'); sys.stdout.flush()\n"
    "time.sleep(120)\n"
)


def guion_que_descarga(destino: str, porcentaje: str = "42.0") -> str:
    return ("import sys\n"
            f"open({destino!r}, 'wb').write(b'video')\n"
            f"sys.stdout.write('[download]  {porcentaje}%\\n')\n")


class DescargadorTests(BaseTemporal):
    def setUp(self):
        super().setUp()
        self.destino = self.base / "staging"
        self.destino.mkdir()
        self.procesos = []

    def lanzador(self, guion):
        def lanzar(_comando):
            proceso = subprocess.Popen(
                [sys.executable, "-c", guion], stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            self.procesos.append(proceso)
            return proceso

        return lanzar

    def descargador(self, guion, gracia=2.0):
        return DescargadorYtDlp(self.lanzador(guion), lambda _n: "yt-dlp", gracia=gracia)

    def descargar(self, guion, progreso=None, cancelado=None, gracia=2.0):
        return self.descargador(guion, gracia).descargar(
            "https://ejemplo/vod", str(self.destino), True, progreso, cancelado)

    def afirmar_recolectado(self):
        """Ningun hijo queda vivo ni sin recolectar al volver de `descargar`."""
        for proceso in self.procesos:
            self.assertIsNotNone(proceso.returncode,
                                 "el adaptador tiene que haber hecho wait() del hijo")
            self.assertIsNotNone(proceso.poll())

    # -- camino feliz ----------------------------------------------------- #

    def test_una_descarga_terminada_devuelve_el_archivo_y_reporta_progreso(self):
        visto = []
        resultado = self.descargar(guion_que_descarga(str(self.destino / NOMBRE_SALIDA)),
                                   lambda hechas, totales: visto.append((hechas, totales)))
        self.assertEqual(Path(resultado).read_bytes(), b"video")
        self.assertEqual(visto, [(42.0, 100.0)])
        self.afirmar_recolectado()

    def test_un_codigo_de_salida_distinto_de_cero_es_error_tipado(self):
        guion = ("import sys\n"
                 f"open({str(self.destino / NOMBRE_SALIDA)!r}, 'wb').write(b'x')\n"
                 "sys.exit(3)\n")
        with self.assertRaises(ErrorDescargaIngesta):
            self.descargar(guion)
        self.afirmar_recolectado()

    def test_un_archivo_vacio_no_se_adopta_como_fuente(self):
        guion = f"open({str(self.destino / NOMBRE_SALIDA)!r}, 'wb').write(b'')\n"
        with self.assertRaises(ErrorDescargaIngesta):
            self.descargar(guion)

    def test_una_descarga_que_no_deja_su_salida_se_rechaza(self):
        with self.assertRaises(ErrorDescargaIngesta):
            self.descargar("pass\n")

    def test_un_resultado_fuera_del_directorio_pedido_se_rechaza(self):
        ajeno = self.base / "ajeno.mp4"
        ajeno.write_bytes(b"video")
        with self.assertRaises(ErrorDescargaIngesta):
            self.descargador("pass\n")._exigir_dentro(str(ajeno), self.destino, "u")

    def test_sin_la_herramienta_instalada_el_error_lo_dice(self):
        descargador = DescargadorYtDlp(self.lanzador("pass\n"), lambda _n: None)
        with self.assertRaises(ErrorDescargaIngesta) as capturado:
            descargador.descargar("https://ejemplo/vod", str(self.destino))
        self.assertIn("yt-dlp", str(capturado.exception))
        self.assertEqual(self.procesos, [], "no se lanza nada si falta la herramienta")

    # -- cancelacion ------------------------------------------------------ #

    def test_cancelar_antes_de_empezar_no_llega_a_lanzar_proceso(self):
        with self.assertRaises(ErrorCancelacionIngesta):
            self.descargar(GUION_LARGO, cancelado=lambda: True)
        self.assertEqual(self.procesos, [])

    def test_cancelar_sin_haber_recibido_salida_termina_y_espera_al_hijo(self):
        """`cancel-before-output`: el hijo esta callado y aun asi se atiende."""
        llamadas = {"n": 0}

        def cancelado():
            llamadas["n"] += 1
            return llamadas["n"] > 1  # la primera consulta ocurre antes de lanzar

        with self.assertRaises(ErrorCancelacionIngesta):
            self.descargar(GUION_CALLADO, cancelado=cancelado)
        self.assertEqual(len(self.procesos), 1)
        self.afirmar_recolectado()

    def test_cancelar_despues_de_recibir_salida_tambien_recolecta(self):
        visto = []

        def cancelado():
            return bool(visto)

        with self.assertRaises(ErrorCancelacionIngesta):
            self.descargar(GUION_LARGO, progreso=lambda h, t: visto.append(h),
                           cancelado=cancelado)
        self.assertEqual(visto, [1.0], "se leyo la salida antes de parar")
        self.afirmar_recolectado()

    @unittest.skipIf(os.name == "nt",
                     "en Windows terminate() es TerminateProcess y no se puede ignorar")
    def test_un_hijo_que_ignora_la_parada_cooperativa_se_fuerza_tras_el_timeout(self):
        visto = []
        with self.assertRaises(ErrorCancelacionIngesta):
            self.descargar(GUION_SORDO, progreso=lambda h, t: visto.append(h),
                           cancelado=lambda: bool(visto), gracia=0.5)
        self.afirmar_recolectado()
        self.assertNotEqual(self.procesos[0].returncode, 0)

    def test_tras_cancelar_el_staging_del_proyecto_se_puede_retirar(self):
        """En Windows un handle vivo del hijo impediria borrar el directorio."""
        raiz = self.proyecto()
        fuentes = AlmacenFuentesProyecto(raiz)
        guardado = {}
        with fuentes.staging() as temporal:
            guardado["ruta"] = Path(temporal)
            with self.assertRaises(ErrorCancelacionIngesta):
                self.descargador(GUION_LARGO).descargar(
                    "https://ejemplo/vod", temporal, True, None, lambda: True)
        self.assertFalse(guardado["ruta"].exists(),
                         "el staging se retira porque nadie lo sigue ocupando")

    def test_el_comando_declara_la_altura_y_no_toca_playlists(self):
        alto = comando_descarga("yt-dlp", "https://x", "d/vod.mp4", True)
        bajo = comando_descarga("yt-dlp", "https://x", "d/vod.mp4", False)
        self.assertIn("--no-playlist", alto)
        self.assertIn("height<=1080", " ".join(alto))
        self.assertIn("height<=720", " ".join(bajo))


if __name__ == "__main__":
    unittest.main()
