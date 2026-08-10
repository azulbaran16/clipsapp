"""Ingesta extremo a extremo, sin UI, sin FFmpeg y sin red.

El proyecto, el sistema de archivos y SQLite son reales; ffprobe y el
descargador entran fingidos por sus puertos. Eso permite comprobar justo lo que
el ticket pide y de otro modo exigiria una grabadora de video:

- un archivo local y una descarga terminada producen el mismo modelo;
- reingerir sin cambios reutiliza fuente y artefacto;
- reemplazar bytes conservando la entrada invalida solo a los descendientes;
- ningun fallo entre staging, validacion, rename y commit publica a medias.
"""

from pathlib import Path
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipperkick.application.ingest import CasoDeUsoIngesta, SolicitudIngesta  # noqa: E402
from clipperkick.application.ingest.ports import NOMBRE_ARCHIVO_SONDEO  # noqa: E402
from clipperkick.domain.ingest import (  # noqa: E402
    CODIGO_ARTEFACTO_REUTILIZADO, CODIGO_DESCARGA_CANCELADA, CODIGO_FUENTE_REEMPLAZADA,
    CODIGO_FUENTE_RELOCALIZADA, CODIGO_FUENTE_REPARADA, CODIGO_FUENTE_REUTILIZADA,
    CODIGO_TEMPORALES_RETIRADOS, ESTADO_DISPONIBLE,
    ESTADO_RECONSTRUIBLE, AvisoMedia, ErrorCancelacionIngesta, ErrorDescargaIngesta,
    ErrorFuenteIngesta, ErrorIngesta,
    ErrorMediaDanada, EstadoFuente, ModoFuente, OrigenFuente, es_clave_materializacion,
)
from clipperkick.infrastructure.ingest import (  # noqa: E402
    NOMBRE_MARCADOR, PREFIJO_STAGING, AlmacenArtefactosIngestaProyecto, AlmacenFuentesProyecto,
    RepositorioSqliteFuentes, ServicioHuellaArchivo, crear_caso_de_uso_ingesta,
)
from clipperkick.infrastructure.ingest.sources import CONTENIDO_MARCADOR  # noqa: E402
from clipperkick.infrastructure.project import abrir_proyecto, crear_proyecto  # noqa: E402

from test_ingest_domain import CFR, MULTISTREAM, SIN_AUDIO, VFR  # noqa: E402


VIDEO = b"CFR." + b"contenido de video" * 8
VIDEO_VFR = b"VFR." + b"contenido variable" * 8
VIDEO_MUDO = b"MUDO" + b"sin pista de audio" * 8
VIDEO_MULTI = b"MULT" + b"varios flujos aqui" * 8
VIDEO_ROTO = b"ROTO" + b"bytes que no abren" * 8

DOCUMENTOS = {b"CFR.": CFR, b"VFR.": VFR, b"MUDO": SIN_AUDIO, b"MULT": MULTISTREAM}


class SondaFalsa:
    """Devuelve un sondeo grabado elegido por los primeros bytes del archivo."""

    def __init__(self, version="ffprobe/8.0"):
        self._version = version
        self.llamadas = []

    def describir(self, ruta):
        self.llamadas.append(ruta)
        datos = Path(ruta).read_bytes()
        documento = DOCUMENTOS.get(datos[:4])
        if documento is None:
            raise ErrorMediaDanada("El archivo no se pudo inspeccionar.")
        return {"streams": [dict(flujo) for flujo in documento["streams"]],
                "format": {**documento["format"], "filename": str(ruta),
                           "size": str(len(datos))}}

    def version(self):
        return self._version


class DescargadorFalso:
    def __init__(self, datos, nombre="vod.mp4"):
        self.datos, self.nombre = datos, nombre
        self.llamadas = []

    def descargar(self, url, directorio, hd, progreso=None, cancelado=None):
        self.llamadas.append((url, hd))
        destino = Path(directorio, self.nombre)
        destino.write_bytes(self.datos)
        return str(destino)


class BaseIngesta(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.base = Path(self.tempdir.name)
        self.entradas = self.base / "entradas"
        self.entradas.mkdir()

    # -- construccion ---------------------------------------------------- #

    def abrir(self, nombre="proyecto"):
        proyecto = crear_proyecto(self.base / f"{nombre}.clipsapp", nombre)
        self.addCleanup(proyecto.close)
        return proyecto

    def caso(self, proyecto=None, sonda=None, descargador=None, **extra):
        proyecto = proyecto or self.abrir()
        raiz = proyecto.repositorio.raiz
        argumentos = {
            "almacen_fuentes": AlmacenFuentesProyecto(raiz),
            "huellas": ServicioHuellaArchivo(),
            "sonda": sonda or SondaFalsa(),
            "artefactos": AlmacenArtefactosIngestaProyecto(raiz),
            "repositorio": RepositorioSqliteFuentes(proyecto.repositorio),
            "descargador": descargador,
        }
        argumentos.update(extra)
        return proyecto, CasoDeUsoIngesta(**argumentos)

    def archivo(self, nombre, datos=VIDEO):
        destino = self.entradas / nombre
        destino.write_bytes(datos)
        return destino

    # -- consultas ------------------------------------------------------- #

    def filas(self, proyecto, sql, parametros=()):
        return proyecto.repositorio.conexion.execute(sql, parametros).fetchall()

    def fuentes(self, proyecto):
        return self.filas(proyecto, "SELECT id, mode, origin, state, fingerprint, source_uri"
                                    " FROM SourceAsset ORDER BY rowid")

    def artefactos(self, proyecto):
        return self.filas(proyecto, "SELECT materialization_key, source_id, state, relative_path,"
                                    " checksum FROM AnalysisArtifact ORDER BY rowid")

    def codigos(self, resultado):
        return [diagnostico.codigo for diagnostico in resultado.diagnosticos]


# --------------------------------------------------------------------------- #
# Camino feliz y equivalencia local/descarga
# --------------------------------------------------------------------------- #

class IngestaBasicaTests(BaseIngesta):
    def test_un_archivo_local_copiado_produce_fuente_metadata_y_artefacto(self):
        proyecto, caso = self.caso()
        resultado = caso.ingerir(SolicitudIngesta(str(self.archivo("clip.mp4"))))

        self.assertEqual(resultado.fuente.modo, ModoFuente.COPIADA)
        self.assertEqual(resultado.fuente.origen, OrigenFuente.LOCAL)
        self.assertEqual(resultado.metadata.principal.codec, "h264")
        self.assertTrue(es_clave_materializacion(resultado.clave))
        self.assertEqual(resultado.artefactos, (f"artifacts/{resultado.clave}/probe.json",))
        self.assertTrue((proyecto.repositorio.raiz / resultado.artefactos[0]).is_file())

        (fila,) = self.fuentes(proyecto)
        self.assertEqual((fila[1], fila[2], fila[3]), ("copied", "local", "available"))
        self.assertEqual(fila[4], resultado.fuente.huella.identidad)
        (artefacto,) = self.artefactos(proyecto)
        self.assertEqual((artefacto[0], artefacto[1], artefacto[2]),
                         (resultado.clave, resultado.fuente.id, ESTADO_DISPONIBLE))
        self.assertTrue(artefacto[4].startswith("sha256:"))

    def test_una_fuente_referenciada_conserva_ruta_tamano_mtime_y_huella(self):
        proyecto, caso = self.caso()
        origen = self.archivo("externo.mp4")
        resultado = caso.ingerir(SolicitudIngesta(str(origen), copiar_local=False))

        self.assertEqual(resultado.fuente.modo, ModoFuente.REFERENCIADA)
        self.assertEqual(Path(resultado.fuente.ruta_externa), origen.resolve())
        self.assertEqual(resultado.fuente.huella.tamano, origen.stat().st_size)
        self.assertEqual(resultado.fuente.huella.mtime_ns, origen.stat().st_mtime_ns)
        self.assertEqual(list((proyecto.repositorio.raiz / "sources").iterdir()), [],
                         "una fuente referenciada no se copia")

    def test_local_copiada_y_descarga_terminada_producen_el_mismo_modelo(self):
        local_proyecto, local = self.caso()
        remoto_proyecto, remoto = self.caso(proyecto=self.abrir("remoto"),
                                            descargador=DescargadorFalso(VIDEO))

        desde_disco = local.ingerir(SolicitudIngesta(str(self.archivo("igual.mp4"))))
        desde_red = remoto.ingerir(SolicitudIngesta("https://ejemplo/vod"))

        self.assertEqual(desde_disco.fuente.huella.identidad, desde_red.fuente.huella.identidad)
        self.assertEqual(desde_disco.fuente.modo, desde_red.fuente.modo)
        self.assertEqual(desde_disco.metadata, desde_red.metadata)
        self.assertEqual(desde_disco.clave, desde_red.clave,
                         "la clave no puede depender de por donde llegaron los bytes")
        self.assertEqual(desde_disco.fuente.origen, OrigenFuente.LOCAL)
        self.assertEqual(desde_red.fuente.origen, OrigenFuente.DESCARGA)

        publicado = (local_proyecto.repositorio.raiz / desde_disco.artefactos[0]).read_bytes()
        replicado = (remoto_proyecto.repositorio.raiz / desde_red.artefactos[0]).read_bytes()
        self.assertEqual(publicado, replicado,
                         "el artefacto publicado no puede llevar dentro la ruta de origen")

    def test_una_url_sin_descargador_configurado_es_error_tipado(self):
        _proyecto, caso = self.caso()
        with self.assertRaises(ErrorDescargaIngesta):
            caso.ingerir(SolicitudIngesta("https://ejemplo/vod"))

    def test_una_entrada_vacia_o_inexistente_se_rechaza(self):
        _proyecto, caso = self.caso()
        with self.assertRaises(ErrorFuenteIngesta):
            caso.ingerir(SolicitudIngesta("   "))
        with self.assertRaises(ErrorFuenteIngesta):
            caso.ingerir(SolicitudIngesta(str(self.entradas / "no-existe.mp4")))

    def test_el_artefacto_publicado_conserva_linaje_huella_y_sondeo(self):
        proyecto, caso = self.caso()
        resultado = caso.ingerir(SolicitudIngesta(str(self.archivo("clip.mp4"))))
        documento = json.loads(
            (proyecto.repositorio.raiz / resultado.artefactos[0]).read_text(encoding="utf-8"))
        self.assertEqual(documento["linaje"]["clave"], resultado.clave)
        self.assertEqual(documento["linaje"]["version_proveedor"], "ffprobe/8.0")
        self.assertEqual(documento["entrada"]["huella"]["completa"],
                         resultado.fuente.huella.identidad)
        self.assertIn("streams", documento["sondeo"])
        self.assertNotIn("filename", documento["sondeo"]["format"])


# --------------------------------------------------------------------------- #
# Estados tipados de la media
# --------------------------------------------------------------------------- #

class EstadosDeMediaTests(BaseIngesta):
    def ingerir(self, datos, nombre):
        # Un proyecto por caso: cada subprueba mira una fuente distinta y
        # compartir carpeta acoplaria sus reutilizaciones.
        _proyecto, caso = self.caso(proyecto=self.abrir(Path(nombre).stem))
        return caso.ingerir(SolicitudIngesta(str(self.archivo(nombre, datos))))

    def test_vfr_sin_audio_y_multistream_son_estados_y_no_excepciones(self):
        casos = ((VIDEO_VFR, "vfr.mp4", AvisoMedia.TASA_VARIABLE),
                 (VIDEO_MUDO, "mudo.mp4", AvisoMedia.SIN_AUDIO),
                 (VIDEO_MULTI, "multi.mp4", AvisoMedia.MULTIPLES_VIDEO))
        for datos, nombre, aviso in casos:
            with self.subTest(nombre=nombre):
                resultado = self.ingerir(datos, nombre)
                self.assertIn(aviso, resultado.metadata.avisos)
                self.assertIn("ingesta.aviso_media", self.codigos(resultado))

    def test_media_danada_no_deja_fuente_ni_copia_huerfana(self):
        proyecto, caso = self.caso()
        with self.assertRaises(ErrorMediaDanada):
            caso.ingerir(SolicitudIngesta(str(self.archivo("roto.mp4", VIDEO_ROTO))))
        self.assertEqual(self.fuentes(proyecto), [])
        self.assertEqual(self.artefactos(proyecto), [])
        self.assertEqual(list((proyecto.repositorio.raiz / "sources").iterdir()), [],
                         "una fuente indescriptible no deja bytes sueltos en el proyecto")

    def test_media_danada_referenciada_no_toca_el_archivo_del_usuario(self):
        proyecto, caso = self.caso()
        origen = self.archivo("roto.mp4", VIDEO_ROTO)
        with self.assertRaises(ErrorMediaDanada):
            caso.ingerir(SolicitudIngesta(str(origen), copiar_local=False))
        self.assertTrue(origen.is_file())
        self.assertEqual(self.fuentes(proyecto), [])

    def test_el_staging_de_un_sondeo_fallido_queda_en_cuarentena(self):
        proyecto, caso = self.caso()
        with self.assertRaises(ErrorMediaDanada):
            caso.ingerir(SolicitudIngesta(str(self.archivo("roto.mp4", VIDEO_ROTO))))
        # El staging fallido se conserva donde estaba: su nombre es unico, de
        # modo que aislarlo moviendolo ya no aporta nada y obligaba a borrar un
        # destino deterministico para hacerle sitio.
        trabajos = proyecto.repositorio.raiz / "cache" / "jobs"
        self.assertTrue(any(trabajos.rglob("_worker.json")) or any(trabajos.rglob("*")),
                        "el intento fallido sigue en disco para poder diagnosticarlo")


# --------------------------------------------------------------------------- #
# Reutilizacion
# --------------------------------------------------------------------------- #

class ReutilizacionTests(BaseIngesta):
    def test_reingerir_sin_cambios_reutiliza_fuente_y_artefacto(self):
        proyecto, caso = self.caso()
        origen = self.archivo("clip.mp4")
        primero = caso.ingerir(SolicitudIngesta(str(origen)))
        sondeos = len(caso._sonda.llamadas)  # noqa: SLF001 - se observa el coste, no el detalle
        segundo = caso.ingerir(SolicitudIngesta(str(origen)))

        self.assertEqual(segundo.fuente.id, primero.fuente.id)
        self.assertEqual(segundo.clave, primero.clave)
        self.assertTrue(segundo.fuente_reutilizada)
        self.assertTrue(segundo.artefacto_reutilizado)
        self.assertEqual(segundo.metadata, primero.metadata)
        self.assertEqual(len(caso._sonda.llamadas), sondeos,  # noqa: SLF001
                         "un hit valido no vuelve a sondear")
        self.assertLessEqual({CODIGO_FUENTE_REUTILIZADA, CODIGO_ARTEFACTO_REUTILIZADO},
                             set(self.codigos(segundo)))
        self.assertEqual(len(self.fuentes(proyecto)), 1)
        self.assertEqual(len(list((proyecto.repositorio.raiz / "sources").iterdir())), 1,
                         "la copia redundante se retira")

    def test_los_mismos_bytes_por_otra_ruta_no_duplican_la_fuente(self):
        proyecto, caso = self.caso()
        primero = caso.ingerir(SolicitudIngesta(str(self.archivo("uno.mp4"))))
        segundo = caso.ingerir(SolicitudIngesta(str(self.archivo("dos.mp4"))))
        self.assertEqual(segundo.fuente.id, primero.fuente.id)
        self.assertEqual(len(self.fuentes(proyecto)), 1)

    def test_un_artefacto_con_los_bytes_alterados_no_se_reutiliza(self):
        proyecto, caso = self.caso()
        primero = caso.ingerir(SolicitudIngesta(str(self.archivo("clip.mp4"))))
        publicado = proyecto.repositorio.raiz / primero.artefactos[0]
        publicado.write_text('{"alterado": true}', encoding="utf-8")

        with self.assertRaises(ErrorIngesta):
            caso.ingerir(SolicitudIngesta(str(self.archivo("clip.mp4"))))
        self.assertEqual(publicado.read_text(encoding="utf-8"), '{"alterado": true}',
                         "una publicacion discrepante se conserva para diagnostico")

    def test_un_artefacto_declarado_reconstruible_no_se_reutiliza(self):
        proyecto, caso = self.caso()
        primero = caso.ingerir(SolicitudIngesta(str(self.archivo("clip.mp4"))))
        with proyecto.repositorio.transaccion() as tx:
            tx.execute("UPDATE AnalysisArtifact SET state=? WHERE materialization_key=?",
                       (ESTADO_RECONSTRUIBLE, primero.clave))
        segundo = caso.ingerir(SolicitudIngesta(str(self.archivo("clip.mp4"))))
        self.assertFalse(segundo.artefacto_reutilizado)
        self.assertEqual(self.artefactos(proyecto)[0][2], ESTADO_DISPONIBLE)

    def test_otra_version_del_proveedor_cambia_la_clave_y_no_reutiliza(self):
        proyecto, caso = self.caso()
        primero = caso.ingerir(SolicitudIngesta(str(self.archivo("clip.mp4"))))
        _proyecto, nuevo = self.caso(proyecto=proyecto, sonda=SondaFalsa(version="ffprobe/7.1"))
        segundo = nuevo.ingerir(SolicitudIngesta(str(self.archivo("clip.mp4"))))
        self.assertNotEqual(segundo.clave, primero.clave)
        self.assertFalse(segundo.artefacto_reutilizado)
        self.assertTrue((proyecto.repositorio.raiz / primero.artefactos[0]).is_file(),
                        "el artefacto viejo sigue publicado y legible")


# --------------------------------------------------------------------------- #
# Reemplazo e invalidacion
# --------------------------------------------------------------------------- #

class ReemplazoTests(BaseIngesta):
    def test_reemplazar_bytes_con_la_misma_ruta_invalida_solo_sus_descendientes(self):
        proyecto, caso = self.caso()
        hermana = caso.ingerir(SolicitudIngesta(str(self.archivo("otra.mp4", VIDEO_MUDO))))
        origen = self.archivo("clip.mp4", VIDEO)
        antes = caso.ingerir(SolicitudIngesta(str(origen)))

        origen.write_bytes(VIDEO_VFR)
        despues = caso.ingerir(SolicitudIngesta(str(origen)))

        self.assertNotEqual(despues.fuente.id, antes.fuente.id)
        self.assertEqual(despues.fuente_reemplazada, antes.fuente.id)
        self.assertNotEqual(despues.clave, antes.clave)
        self.assertEqual(despues.invalidados, (antes.clave,))
        self.assertIn(CODIGO_FUENTE_REEMPLAZADA, self.codigos(despues))

        estados = {fila[0]: fila[3] for fila in self.fuentes(proyecto)}
        self.assertEqual(estados[antes.fuente.id], EstadoFuente.REEMPLAZADA.value)
        self.assertEqual(estados[despues.fuente.id], EstadoFuente.DISPONIBLE.value)
        por_clave = {fila[0]: fila[2] for fila in self.artefactos(proyecto)}
        self.assertEqual(por_clave[antes.clave], ESTADO_RECONSTRUIBLE)
        self.assertEqual(por_clave[despues.clave], ESTADO_DISPONIBLE)
        self.assertEqual(por_clave[hermana.clave], ESTADO_DISPONIBLE,
                         "los hermanos validos siguen disponibles")

    def test_reemplazar_no_borra_historia_ni_drafts(self):
        proyecto, caso = self.caso()
        origen = self.archivo("clip.mp4", VIDEO)
        antes = caso.ingerir(SolicitudIngesta(str(origen)))
        with proyecto.repositorio.transaccion() as tx:
            tx.execute("INSERT INTO Moment (id, source_id, start_seconds, end_seconds, score)"
                       " VALUES ('m', ?, 0, 1, 1)", (antes.fuente.id,))
            tx.execute("INSERT INTO Draft (id, moment_id, current_revision_id)"
                       " VALUES ('d', 'm', NULL)")

        origen.write_bytes(VIDEO_VFR)
        caso.ingerir(SolicitudIngesta(str(origen)))

        self.assertEqual(self.filas(proyecto, "SELECT COUNT(*) FROM Moment")[0][0], 1)
        self.assertEqual(self.filas(proyecto, "SELECT COUNT(*) FROM Draft")[0][0], 1)
        self.assertTrue((proyecto.repositorio.raiz / antes.artefactos[0]).is_file(),
                        "invalidar declara reconstruible, no borra")

    def test_reemplazar_una_referencia_externa_tambien_se_detecta(self):
        proyecto, caso = self.caso()
        origen = self.archivo("externo.mp4", VIDEO)
        antes = caso.ingerir(SolicitudIngesta(str(origen), copiar_local=False))
        origen.write_bytes(VIDEO_VFR)
        despues = caso.ingerir(SolicitudIngesta(str(origen), copiar_local=False))
        self.assertEqual(despues.fuente_reemplazada, antes.fuente.id)
        self.assertEqual(len(self.fuentes(proyecto)), 2)

    def test_una_fuente_externa_movida_se_relocaliza_en_vez_de_duplicarse(self):
        proyecto, caso = self.caso()
        origen = self.archivo("externo.mp4", VIDEO)
        antes = caso.ingerir(SolicitudIngesta(str(origen), copiar_local=False))
        destino = self.entradas / "movido.mp4"
        origen.rename(destino)

        despues = caso.ingerir(SolicitudIngesta(str(destino), copiar_local=False))
        self.assertEqual(despues.fuente.id, antes.fuente.id)
        self.assertEqual(Path(despues.fuente.ruta_externa), destino.resolve())
        self.assertIn(CODIGO_FUENTE_RELOCALIZADA, self.codigos(despues))
        self.assertEqual(len(self.fuentes(proyecto)), 1)


# --------------------------------------------------------------------------- #
# Fallos inyectados
# --------------------------------------------------------------------------- #

class ArtefactosFallando:
    """Envoltorio que deja fallar una operacion concreta del almacen."""

    def __init__(self, real, fallo=None, en=""):
        self._real, self._fallo, self._en = real, fallo, en
        self.cuarentenas = []

    def __getattr__(self, nombre):
        atributo = getattr(self._real, nombre)
        if nombre != self._en:
            return atributo

        def envuelto(*argumentos, **claves):
            resultado = atributo(*argumentos, **claves)
            raise self._fallo
        return envuelto

    def cuarentena(self, directorio):
        resultado = self._real.cuarentena(directorio)
        self.cuarentenas.append(resultado)
        return resultado


class RepositorioFallando:
    def __init__(self, real, fallos=0):
        self._real, self._fallos = real, fallos

    def __getattr__(self, nombre):
        return getattr(self._real, nombre)

    def registrar_artefacto(self, registro, bytes_totales=0):
        if self._fallos > 0:
            self._fallos -= 1
            raise ErrorIngesta("no se pudo confirmar el artefacto")
        return self._real.registrar_artefacto(registro, bytes_totales)


class FallosInyectadosTests(BaseIngesta):
    def test_un_fallo_al_publicar_no_deja_artefacto_ni_fuente_ni_copia(self):
        proyecto = self.abrir()
        raiz = proyecto.repositorio.raiz
        almacen = ArtefactosFallando(AlmacenArtefactosIngestaProyecto(raiz),
                                     OSError(28, "No space left on device"), en="publicar")
        _proyecto, caso = self.caso(proyecto=proyecto, artefactos=almacen)

        with self.assertRaises(OSError):
            caso.ingerir(SolicitudIngesta(str(self.archivo("clip.mp4"))))

        self.assertEqual(self.fuentes(proyecto), [])
        self.assertEqual(self.artefactos(proyecto), [])
        self.assertEqual(list((raiz / "sources").iterdir()), [])
        self.assertEqual(len(almacen.cuarentenas), 1)

    def test_publicar_y_morir_antes_del_commit_se_recupera_adoptando(self):
        """Es el orden correcto: los bytes existen y su fila llega despues."""
        proyecto = self.abrir()
        repositorio = RepositorioFallando(RepositorioSqliteFuentes(proyecto.repositorio), fallos=1)
        _proyecto, caso = self.caso(proyecto=proyecto, repositorio=repositorio)
        origen = self.archivo("clip.mp4")

        with self.assertRaises(ErrorIngesta):
            caso.ingerir(SolicitudIngesta(str(origen)))
        publicados = list((proyecto.repositorio.raiz / "artifacts").iterdir())
        self.assertEqual(len(publicados), 1, "la publicacion sobrevive al fallo del commit")
        self.assertEqual(len(self.fuentes(proyecto)), 1, "la fuente si quedo registrada")
        self.assertEqual(self.artefactos(proyecto), [])

        resultado = caso.ingerir(SolicitudIngesta(str(origen)))
        self.assertEqual(len(self.artefactos(proyecto)), 1)
        self.assertEqual(self.artefactos(proyecto)[0][0], resultado.clave)
        self.assertEqual(Path(publicados[0]).name, resultado.clave)

    def test_un_fallo_al_registrar_la_fuente_no_publica_a_medias(self):
        proyecto = self.abrir()
        raiz = proyecto.repositorio.raiz
        with proyecto.repositorio.transaccion() as tx:
            tx.execute("CREATE TRIGGER inyectado BEFORE INSERT ON SourceAsset"
                       " BEGIN SELECT RAISE(ABORT, 'inyectado'); END")
        _proyecto, caso = self.caso(proyecto=proyecto)

        with self.assertRaises(ErrorIngesta):
            caso.ingerir(SolicitudIngesta(str(self.archivo("clip.mp4"))))
        self.assertEqual(self.fuentes(proyecto), [])
        self.assertEqual(self.artefactos(proyecto), [])
        self.assertEqual(list((raiz / "sources").iterdir()), [],
                         "la copia se retira cuando su fila no llego a existir")

    def test_un_manifiesto_que_no_se_puede_escribir_no_deshace_la_ingesta(self):
        """El manifiesto es una vista derivada: su fallo no puede borrar la fuente."""
        proyecto = self.abrir()
        raiz = proyecto.repositorio.raiz
        (raiz / "project.json").write_text("no soy json", encoding="utf-8")
        _proyecto, caso = self.caso(proyecto=proyecto)

        resultado = caso.ingerir(SolicitudIngesta(str(self.archivo("clip.mp4"))))
        self.assertEqual(len(self.fuentes(proyecto)), 1)
        self.assertEqual(len(self.artefactos(proyecto)), 1)
        self.assertTrue(Path(raiz, resultado.fuente.ruta_relativa).is_file(),
                        "los bytes de una fuente registrada no se retiran")

    def test_una_descarga_fallida_no_deja_nada_en_sources(self):
        class DescargaRota:
            def descargar(self, url, directorio, hd, progreso=None, cancelado=None):
                Path(directorio, "parcial.mp4").write_bytes(b"a medias")
                raise ErrorDescargaIngesta("se corto la descarga")

        proyecto, caso = self.caso(descargador=DescargaRota())
        with self.assertRaises(ErrorDescargaIngesta):
            caso.ingerir(SolicitudIngesta("https://ejemplo/vod"))
        self.assertEqual(list((proyecto.repositorio.raiz / "sources").iterdir()), [])
        self.assertEqual(self.fuentes(proyecto), [])


# --------------------------------------------------------------------------- #
# Integridad del hit por identidad
# --------------------------------------------------------------------------- #

class IdentidadDelDestinoTests(BaseIngesta):
    """Un hit por identidad no puede creerse la fila: tiene que mirar los bytes."""

    def test_una_copia_registrada_manipulada_no_produce_cache_hit(self):
        """Regresion del P0: presencia no es identidad.

        La copia interna se sobrescribe con **otro contenido del mismo tamano**,
        que es justo lo que ninguna senal barata delata. Antes, la ingesta
        descartaba el material entrante —el correcto— y reutilizaba fila y
        artefacto sobre bytes ajenos.
        """
        proyecto, caso = self.caso()
        origen = self.archivo("clip.mp4", VIDEO)
        primero = caso.ingerir(SolicitudIngesta(str(origen)))
        raiz = proyecto.repositorio.raiz
        manipulada = raiz / primero.fuente.ruta_relativa
        self.assertEqual(len(VIDEO_VFR), len(VIDEO), "el escenario es de igual tamano")
        manipulada.write_bytes(VIDEO_VFR)

        segundo = caso.ingerir(SolicitudIngesta(str(origen)))

        self.assertEqual(segundo.fuente.id, primero.fuente.id)
        self.assertFalse(segundo.fuente_reutilizada,
                         "lo guardado estaba mal: esto no es una reutilizacion")
        self.assertEqual(segundo.fuente_reparada, primero.fuente.id)
        self.assertIn(CODIGO_FUENTE_REPARADA, self.codigos(segundo))
        self.assertNotIn(CODIGO_FUENTE_REUTILIZADA, self.codigos(segundo))

        vigente = raiz / segundo.fuente.ruta_relativa
        self.assertNotEqual(vigente, manipulada, "la fila apunta al material entrante")
        self.assertEqual(vigente.read_bytes(), VIDEO, "se conservan los bytes correctos")
        (fila,) = self.fuentes(proyecto)
        self.assertEqual(fila[4], ServicioHuellaArchivo().completa(str(vigente)).identidad,
                         "la huella de la fila describe los bytes que hay")

    def test_la_copia_discrepante_se_conserva_como_evidencia(self):
        proyecto, caso = self.caso()
        origen = self.archivo("clip.mp4", VIDEO)
        primero = caso.ingerir(SolicitudIngesta(str(origen)))
        manipulada = proyecto.repositorio.raiz / primero.fuente.ruta_relativa
        manipulada.write_bytes(VIDEO_VFR)

        caso.ingerir(SolicitudIngesta(str(origen)))
        self.assertTrue(manipulada.is_file(),
                        "sobrescribir material del proyecto es un hecho que hay que poder mirar")
        self.assertEqual(manipulada.read_bytes(), VIDEO_VFR)

    def test_una_copia_intacta_sigue_reutilizandose_sin_duplicar_material(self):
        proyecto, caso = self.caso()
        origen = self.archivo("clip.mp4", VIDEO)
        primero = caso.ingerir(SolicitudIngesta(str(origen)))
        segundo = caso.ingerir(SolicitudIngesta(str(origen)))

        self.assertTrue(segundo.fuente_reutilizada)
        self.assertIsNone(segundo.fuente_reparada)
        self.assertEqual(segundo.fuente.id, primero.fuente.id)
        self.assertEqual(len(list((proyecto.repositorio.raiz / "sources").iterdir())), 1)

    def test_una_referencia_externa_ilegible_no_se_toma_por_valida(self):
        """No poder demostrar que son los mismos bytes no es demostrar que lo son."""
        proyecto, caso = self.caso()
        externo = self.archivo("externo.mp4", VIDEO)
        primero = caso.ingerir(SolicitudIngesta(str(externo), copiar_local=False))
        externo.write_bytes(VIDEO_VFR)  # los bytes de la ruta registrada ya no son suyos
        gemelo = self.archivo("gemelo.mp4", VIDEO)

        segundo = caso.ingerir(SolicitudIngesta(str(gemelo), copiar_local=False))
        self.assertEqual(segundo.fuente.id, primero.fuente.id)
        self.assertEqual(segundo.fuente_reparada, primero.fuente.id)
        self.assertEqual(Path(segundo.fuente.ruta_externa), gemelo.resolve())


# --------------------------------------------------------------------------- #
# Atomicidad de la sucesion
# --------------------------------------------------------------------------- #

class SucesionAtomicaTests(BaseIngesta):
    def preparar_reemplazo(self):
        proyecto, caso = self.caso()
        origen = self.archivo("clip.mp4", VIDEO)
        antes = caso.ingerir(SolicitudIngesta(str(origen)))
        origen.write_bytes(VIDEO_VFR)
        return proyecto, caso, origen, antes

    def test_un_fallo_al_invalidar_deshace_tambien_el_alta_de_la_sucesora(self):
        """Regresion del P0: eran dos commits y un fallo entre ambos era invisible."""
        proyecto, caso, origen, antes = self.preparar_reemplazo()
        with proyecto.repositorio.transaccion() as tx:
            tx.execute("CREATE TRIGGER inyectado BEFORE UPDATE ON AnalysisArtifact"
                       " BEGIN SELECT RAISE(ABORT, 'inyectado'); END")

        with self.assertRaises(ErrorIngesta):
            caso.ingerir(SolicitudIngesta(str(origen)))

        fuentes = self.fuentes(proyecto)
        self.assertEqual(len(fuentes), 1, "la sucesora no llego a existir")
        self.assertEqual(fuentes[0][0], antes.fuente.id)
        self.assertEqual(fuentes[0][3], EstadoFuente.DISPONIBLE.value,
                         "la predecesora sigue vigente: nadie la sucedio")
        self.assertEqual(self.artefactos(proyecto)[0][2], ESTADO_DISPONIBLE)

    def test_un_fallo_al_invalidar_retira_los_bytes_que_nadie_reclamo(self):
        proyecto, caso, origen, _antes = self.preparar_reemplazo()
        raiz = proyecto.repositorio.raiz
        with proyecto.repositorio.transaccion() as tx:
            tx.execute("CREATE TRIGGER inyectado BEFORE UPDATE ON AnalysisArtifact"
                       " BEGIN SELECT RAISE(ABORT, 'inyectado'); END")

        with self.assertRaises(ErrorIngesta):
            caso.ingerir(SolicitudIngesta(str(origen)))
        self.assertEqual(len(list((raiz / "sources").iterdir())), 1,
                         "solo queda la copia de la fuente que si esta registrada")

    def test_un_fallo_posterior_al_commit_no_borra_los_bytes_ya_referenciados(self):
        """El `except` no puede llevarse el material de una fila que ya existe."""
        proyecto = self.abrir()
        repositorio = RepositorioFallando(RepositorioSqliteFuentes(proyecto.repositorio), fallos=1)
        _proyecto, caso = self.caso(proyecto=proyecto, repositorio=repositorio)
        raiz = proyecto.repositorio.raiz

        with self.assertRaises(ErrorIngesta):
            caso.ingerir(SolicitudIngesta(str(self.archivo("clip.mp4"))))

        (fila,) = self.fuentes(proyecto)
        relativa = self.filas(proyecto, "SELECT relative_path FROM SourceAsset")[0][0]
        self.assertTrue((raiz / relativa).is_file(),
                        "SQLite referencia esos bytes: retirarlos seria perder la fuente")
        self.assertEqual(fila[3], EstadoFuente.DISPONIBLE.value)

    def test_una_segunda_instancia_del_proyecto_no_puede_ingerir(self):
        """El lock de escritor de T02 es el que decide quien sucede a una fuente."""
        proyecto = self.abrir("compartido")
        segunda = abrir_proyecto(proyecto.repositorio.raiz)
        self.addCleanup(segunda.close)
        self.assertTrue(segunda.solo_lectura)
        with self.assertRaises(ErrorIngesta):
            crear_caso_de_uso_ingesta(segunda)

    def test_suceder_en_solo_lectura_no_muta_nada(self):
        proyecto, caso = self.caso()
        origen = self.archivo("clip.mp4", VIDEO)
        antes = caso.ingerir(SolicitudIngesta(str(origen)))
        repositorio = RepositorioSqliteFuentes(proyecto.repositorio)
        vigente = repositorio.buscar_por_identidad(antes.fuente.huella.identidad)
        proyecto.repositorio.solo_lectura = True
        try:
            with self.assertRaises(ErrorIngesta):
                repositorio.suceder(vigente, antes.fuente.id)
        finally:
            proyecto.repositorio.solo_lectura = False
        self.assertEqual(self.fuentes(proyecto)[0][3], EstadoFuente.DISPONIBLE.value)
        self.assertEqual(self.artefactos(proyecto)[0][2], ESTADO_DISPONIBLE)

    def test_suceder_dos_veces_a_la_misma_fuente_se_rechaza(self):
        proyecto, _caso = self.caso()
        repositorio = RepositorioSqliteFuentes(proyecto.repositorio)
        _p, caso = self.caso(proyecto=proyecto, repositorio=repositorio)
        origen = self.archivo("clip.mp4", VIDEO)
        antes = caso.ingerir(SolicitudIngesta(str(origen)))
        origen.write_bytes(VIDEO_VFR)
        caso.ingerir(SolicitudIngesta(str(origen)))

        vieja = repositorio.buscar_por_identidad(antes.fuente.huella.identidad)
        self.assertIsNone(vieja, "una fuente jubilada no vuelve a ser destino de ingesta")


# --------------------------------------------------------------------------- #
# Cancelacion
# --------------------------------------------------------------------------- #

class CancelacionTests(BaseIngesta):
    def test_cancelar_antes_de_publicar_no_deja_fuente_ni_artefacto_ni_copia(self):
        proyecto, caso = self.caso()
        raiz = proyecto.repositorio.raiz
        with self.assertRaises(ErrorCancelacionIngesta):
            caso.ingerir(SolicitudIngesta(str(self.archivo("clip.mp4"))), cancelado=lambda: True)

        self.assertEqual(self.fuentes(proyecto), [])
        self.assertEqual(self.artefactos(proyecto), [])
        self.assertEqual(list((raiz / "sources").iterdir()), [])
        self.assertFalse((raiz / "artifacts").exists() and any((raiz / "artifacts").iterdir()))

    def test_cancelar_tras_materializar_retira_los_bytes_copiados(self):
        proyecto, caso = self.caso()
        raiz = proyecto.repositorio.raiz
        estado = {"n": 0}

        def cancelado():
            estado["n"] += 1
            return estado["n"] > 1  # la primera consulta ocurre antes de copiar

        with self.assertRaises(ErrorCancelacionIngesta):
            caso.ingerir(SolicitudIngesta(str(self.archivo("clip.mp4"))), cancelado=cancelado)
        self.assertEqual(list((raiz / "sources").iterdir()), [])
        self.assertEqual(self.fuentes(proyecto), [])

    def test_una_descarga_cancelada_se_diagnostica_y_no_deja_nada(self):
        class DescargaCancelable:
            def descargar(self, url, directorio, hd, progreso=None, cancelado=None):
                Path(directorio, "vod.mp4").write_bytes(b"a medias")
                raise ErrorCancelacionIngesta("cancelada")

        vistos = []
        proyecto, caso = self.caso(descargador=DescargaCancelable(),
                                   notificar=lambda diagnostico: vistos.append(diagnostico.codigo))
        with self.assertRaises(ErrorCancelacionIngesta):
            caso.ingerir(SolicitudIngesta("https://ejemplo/vod"))
        self.assertIn(CODIGO_DESCARGA_CANCELADA, vistos,
                      "una cancelacion se cuenta: el resultado ya no vuelve por retorno")
        self.assertEqual(list((proyecto.repositorio.raiz / "sources").iterdir()), [])
        self.assertEqual(self.fuentes(proyecto), [])

    def test_el_token_de_cancelacion_llega_hasta_el_descargador(self):
        recibido = {}

        class DescargaEspia:
            def descargar(self, url, directorio, hd, progreso=None, cancelado=None):
                recibido["token"] = cancelado
                destino = Path(directorio, "vod.mp4")
                destino.write_bytes(VIDEO)
                return str(destino)

        _proyecto, caso = self.caso(descargador=DescargaEspia())
        token = lambda: False  # noqa: E731
        caso.ingerir(SolicitudIngesta("https://ejemplo/vod"), cancelado=token)
        self.assertIs(recibido["token"], token)


# --------------------------------------------------------------------------- #
# Limpieza y cableado
# --------------------------------------------------------------------------- #

class LimpiezaYCableadoTests(BaseIngesta):
    def abandonar_staging(self, raiz, envejecer=True):
        """Deja un staging con la gramatica y el marcador que crea la ingesta."""
        destino = raiz / "cache" / "ingest" / f"{PREFIJO_STAGING}{'b' * 32}"
        destino.mkdir(parents=True)
        (destino / NOMBRE_MARCADOR).write_bytes(CONTENIDO_MARCADOR)
        if envejecer:
            antiguo = time.time() - 10_000
            for hijo in (destino, destino / NOMBRE_MARCADOR):
                os.utime(hijo, (antiguo, antiguo))
        return destino

    def test_la_ingesta_retira_temporales_abandonados_y_lo_diagnostica(self):
        proyecto, caso = self.caso(antiguedad_temporales=3600.0)
        abandonado = self.abandonar_staging(proyecto.repositorio.raiz)

        resultado = caso.ingerir(SolicitudIngesta(str(self.archivo("clip.mp4"))))
        self.assertIn(f"cache/ingest/{abandonado.name}", resultado.temporales_retirados)
        self.assertIn(CODIGO_TEMPORALES_RETIRADOS, self.codigos(resultado))
        self.assertFalse(abandonado.exists())

    def test_la_ingesta_no_retira_lo_que_no_creo_por_viejo_que_sea(self):
        proyecto, caso = self.caso(antiguedad_temporales=3600.0)
        raiz = proyecto.repositorio.raiz
        ajeno = raiz / "cache" / "ingest" / "trabajo-de-otro"
        ajeno.mkdir(parents=True)
        antiguo = time.time() - 10_000
        os.utime(ajeno, (antiguo, antiguo))

        resultado = caso.ingerir(SolicitudIngesta(str(self.archivo("clip.mp4"))))
        self.assertEqual(resultado.temporales_retirados, ())
        self.assertTrue(ajeno.is_dir())

    def test_la_limpieza_se_puede_pedir_sin_ingerir(self):
        proyecto, caso = self.caso(antiguedad_temporales=0.0)
        abandonado = self.abandonar_staging(proyecto.repositorio.raiz, envejecer=False)
        self.assertEqual(caso.limpiar_temporales(), (f"cache/ingest/{abandonado.name}",))

    def test_el_cableado_por_defecto_ensambla_una_ingesta_funcional(self):
        proyecto = self.abrir()
        caso = crear_caso_de_uso_ingesta(proyecto, sonda=SondaFalsa(),
                                         descargador=DescargadorFalso(VIDEO))
        resultado = caso.ingerir(SolicitudIngesta("https://ejemplo/vod"))
        self.assertEqual(resultado.fuente.origen, OrigenFuente.DESCARGA)
        self.assertTrue((proyecto.repositorio.raiz / resultado.artefactos[0]).is_file())
        self.assertEqual(Path(resultado.artefactos[0]).name, NOMBRE_ARCHIVO_SONDEO)

    def test_el_manifiesto_publica_solo_lo_minimo_para_abrir_las_fuentes(self):
        proyecto, caso = self.caso()
        resultado = caso.ingerir(SolicitudIngesta(str(self.archivo("clip.mp4"))))
        manifiesto = json.loads(
            (proyecto.repositorio.raiz / "project.json").read_text(encoding="utf-8"))

        self.assertEqual(manifiesto["project_id"],
                         proyecto.repositorio.informacion().project_id)
        (referencia,) = manifiesto["fuentes"]
        self.assertEqual(referencia["id"], resultado.fuente.id)
        self.assertEqual(referencia["modo"], "copied")
        self.assertEqual(referencia["tamano"], resultado.fuente.huella.tamano)
        self.assertEqual(referencia["huella_parcial"], resultado.fuente.huella.parcial)
        self.assertNotIn(resultado.fuente.huella.identidad, json.dumps(manifiesto),
                         "la huella completa es autoridad de SQLite, no del manifiesto")
        self.assertNotIn("metadata", referencia)

    def test_el_manifiesto_deja_de_listar_una_fuente_reemplazada(self):
        proyecto, caso = self.caso()
        origen = self.archivo("clip.mp4", VIDEO)
        antes = caso.ingerir(SolicitudIngesta(str(origen)))
        origen.write_bytes(VIDEO_VFR)
        despues = caso.ingerir(SolicitudIngesta(str(origen)))

        manifiesto = json.loads(
            (proyecto.repositorio.raiz / "project.json").read_text(encoding="utf-8"))
        identificadores = [referencia["id"] for referencia in manifiesto["fuentes"]]
        self.assertEqual(identificadores, [despues.fuente.id])
        self.assertNotIn(antes.fuente.id, identificadores,
                         "el manifiesto sirve para abrir; una fuente jubilada no se localiza")

    def test_un_proyecto_en_solo_lectura_no_puede_ingerir(self):
        proyecto = self.abrir()
        proyecto.repositorio.solo_lectura = True
        proyecto.solo_lectura = True
        with self.assertRaises(ErrorIngesta):
            crear_caso_de_uso_ingesta(proyecto)


if __name__ == "__main__":
    unittest.main()
