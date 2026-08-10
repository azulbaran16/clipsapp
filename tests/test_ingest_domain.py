"""Reglas puras de la ingesta: metadata, huellas, claves y reutilizacion.

Todo lo de aqui corre sin FFmpeg, sin red y sin SQLite: los sondeos son
documentos grabados. Es deliberado, porque son justo las reglas que tienen que
dar el mismo resultado en Windows y en WSL para que un proyecto sea portable.
"""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipperkick.domain.ingest import (  # noqa: E402
    ESTADO_RECONSTRUIBLE, ArchivoArtefacto, AvisoMedia, ErrorArtefactoIngesta,
    ErrorClaveMaterializacion, ErrorFuenteIngesta, ErrorHuellaIngesta, ErrorMediaDanada,
    ErrorMetadataMedia, HuellaFuente, LinajeArtefacto, ManifiestoArtefacto, ModoFuente,
    OrigenFuente, Racional, RegistroArtefacto, SourceAsset, VERSION_METADATA, canonicalizar,
    clave_materializacion, componer_completa, componer_parcial, detecta_reemplazo, digest_bloque,
    es_clave_materializacion, etiquetar_checksum, leer_racional, normalizar_sondeo, plan_parcial,
    referencia_de_apertura, reutilizable, sondeo_canonico,
)

# --------------------------------------------------------------------------- #
# Sondeos grabados
# --------------------------------------------------------------------------- #

VIDEO_CFR = {
    "index": 0, "codec_name": "h264", "codec_type": "video", "width": 640, "height": 360,
    "pix_fmt": "yuv420p", "r_frame_rate": "30/1", "avg_frame_rate": "30/1",
    "time_base": "1/15360", "duration": "2.000000", "nb_frames": "60",
}
AUDIO_AAC = {
    "index": 1, "codec_name": "aac", "codec_type": "audio", "channels": 2,
    "sample_rate": "48000", "time_base": "1/48000", "duration": "2.000000",
}
FORMATO = {"filename": "C:/videos/entrada.mp4", "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
           "duration": "2.000000", "size": "40960", "nb_streams": 2}


def sondeo(*streams, **formato):
    return {"streams": list(streams), "format": {**FORMATO, **formato}}


CFR = sondeo(VIDEO_CFR, AUDIO_AAC)
VFR = sondeo({**VIDEO_CFR, "r_frame_rate": "60/1", "avg_frame_rate": "24000/1001"}, AUDIO_AAC)
ROTADO = sondeo({**VIDEO_CFR,
                 "side_data_list": [{"side_data_type": "Display Matrix", "rotation": -90}]},
                AUDIO_AAC)
SIN_AUDIO = sondeo(VIDEO_CFR, nb_streams=1)
MULTISTREAM = sondeo(VIDEO_CFR, {**VIDEO_CFR, "index": 1}, AUDIO_AAC,
                     {**AUDIO_AAC, "index": 3, "codec_name": "opus"},
                     {"index": 4, "codec_type": "subtitle", "codec_name": "mov_text"})


# --------------------------------------------------------------------------- #
# Fracciones
# --------------------------------------------------------------------------- #

class RacionalTests(unittest.TestCase):
    def test_se_reduce_y_normaliza_el_signo(self):
        self.assertEqual(Racional(60, 2), Racional(30, 1))
        self.assertEqual(Racional(1, -2), Racional(-1, 2))
        self.assertEqual(Racional(30000, 1001).como_texto(), "30000/1001")

    def test_no_se_aplana_a_coma_flotante(self):
        """29.97 no es 30000/1001: aplanarlo partiria la clave de materializacion."""
        self.assertNotEqual(Racional(30000, 1001), Racional(2997, 100))
        self.assertAlmostEqual(Racional(30000, 1001).valor, 29.97, places=2)

    def test_denominador_cero_es_error_tipado(self):
        with self.assertRaises(ErrorMetadataMedia):
            Racional(1, 0)

    def test_lee_las_formas_que_usa_ffprobe(self):
        self.assertEqual(leer_racional("30000/1001"), Racional(30000, 1001))
        self.assertEqual(leer_racional("25"), Racional(25, 1))
        self.assertEqual(leer_racional(24), Racional(24, 1))

    def test_las_ausencias_de_ffprobe_no_se_convierten_en_cero(self):
        """`0/0` significa "no lo se"; cero fps seria una afirmacion falsa."""
        for ausente in ("0/0", "N/A", "", None, "bandera", True):
            with self.subTest(ausente=ausente):
                self.assertIsNone(leer_racional(ausente))


# --------------------------------------------------------------------------- #
# Normalizacion del sondeo
# --------------------------------------------------------------------------- #

class NormalizacionTests(unittest.TestCase):
    def test_cfr_declara_streams_duracion_timebase_fps_y_codecs(self):
        metadata = normalizar_sondeo(CFR)
        video = metadata.principal
        self.assertEqual(metadata.duracion_segundos, 2.0)
        self.assertEqual(video.codec, "h264")
        self.assertEqual((video.ancho, video.alto), (640, 360))
        self.assertEqual(video.timebase, Racional(1, 15360))
        self.assertEqual(video.tasa_declarada, Racional(30, 1))
        self.assertFalse(video.tasa_variable)
        self.assertEqual(metadata.audio[0].codec, "aac")
        self.assertEqual(metadata.audio[0].canales, 2)
        self.assertEqual(metadata.audio[0].tasa_muestreo, 48000)
        self.assertEqual(metadata.avisos, frozenset())

    def test_vfr_es_un_estado_declarado_y_no_una_excepcion(self):
        metadata = normalizar_sondeo(VFR)
        self.assertTrue(metadata.principal.tasa_variable)
        self.assertIn(AvisoMedia.TASA_VARIABLE, metadata.avisos)

    def test_rotacion_negativa_y_etiqueta_producen_el_mismo_valor(self):
        por_matriz = normalizar_sondeo(ROTADO).principal
        por_etiqueta = normalizar_sondeo(
            sondeo({**VIDEO_CFR, "tags": {"rotate": "270"}}, AUDIO_AAC)).principal
        self.assertEqual(por_matriz.rotacion, 270)
        self.assertEqual(por_etiqueta.rotacion, 270)
        self.assertEqual((por_matriz.ancho_mostrado, por_matriz.alto_mostrado), (360, 640))

    def test_rotacion_oblicua_se_conserva_y_se_avisa(self):
        metadata = normalizar_sondeo(sondeo(
            {**VIDEO_CFR, "side_data_list": [{"rotation": 45}]}, AUDIO_AAC))
        self.assertEqual(metadata.principal.rotacion, 45)
        self.assertIn(AvisoMedia.ROTACION_OBLICUA, metadata.avisos)

    def test_sin_audio_y_multistream_son_avisos_explicitos(self):
        sin_audio = normalizar_sondeo(SIN_AUDIO)
        self.assertFalse(sin_audio.tiene_audio)
        self.assertIn(AvisoMedia.SIN_AUDIO, sin_audio.avisos)

        multi = normalizar_sondeo(MULTISTREAM)
        self.assertEqual((len(multi.video), len(multi.audio), multi.otros_flujos), (2, 2, 1))
        self.assertLessEqual({AvisoMedia.MULTIPLES_VIDEO, AvisoMedia.MULTIPLES_AUDIO,
                              AvisoMedia.FLUJOS_IGNORADOS}, multi.avisos)

    def test_media_danada_es_un_error_tipado_y_no_una_metadata_vacia(self):
        for roto in ({}, {"streams": "no-es-lista"}, {"streams": [AUDIO_AAC]},
                     {"streams": [{**VIDEO_CFR, "width": 0}]}):
            with self.subTest(roto=roto):
                with self.assertRaises(ErrorMediaDanada):
                    normalizar_sondeo(roto)

    def test_un_documento_que_no_es_objeto_no_llega_al_normalizador(self):
        with self.assertRaises(ErrorMetadataMedia):
            normalizar_sondeo("no soy un documento")

    def test_sin_duracion_de_contenedor_se_toma_la_mayor_de_los_flujos(self):
        metadata = normalizar_sondeo(sondeo(
            {**VIDEO_CFR, "duration": "1.5"}, {**AUDIO_AAC, "duration": "2.25"}, duration="N/A"))
        self.assertEqual(metadata.duracion_segundos, 2.25)
        self.assertNotIn(AvisoMedia.SIN_DURACION, metadata.avisos)

    def test_sin_duracion_en_ninguna_parte_se_declara_ausente(self):
        crudo = sondeo({clave: valor for clave, valor in VIDEO_CFR.items() if clave != "duration"},
                       duration="N/A")
        metadata = normalizar_sondeo(crudo)
        self.assertIsNone(metadata.duracion_segundos)
        self.assertIn(AvisoMedia.SIN_DURACION, metadata.avisos)

    def test_duracion_derivada_de_marcas_y_timebase(self):
        crudo = sondeo({**{k: v for k, v in VIDEO_CFR.items() if k != "duration"},
                        "duration_ts": 30720}, duration="N/A")
        self.assertAlmostEqual(normalizar_sondeo(crudo).duracion_segundos, 2.0)

    def test_el_documento_canonico_es_estable_y_versionado(self):
        documento = normalizar_sondeo(CFR).como_documento()
        self.assertEqual(documento["version"], VERSION_METADATA)
        self.assertEqual(documento["video"][0]["tasa_declarada"], "30/1")
        self.assertEqual(canonicalizar(documento),
                         canonicalizar(normalizar_sondeo(CFR).como_documento()))

    def test_el_sondeo_canonico_retira_la_ruta_y_conserva_lo_demas(self):
        canonico = sondeo_canonico(CFR)
        self.assertNotIn("filename", canonico["format"])
        self.assertEqual(canonico["format"]["duration"], "2.000000")
        self.assertIn("filename", CFR["format"], "el documento original no se muta")
        otro = sondeo_canonico(sondeo(VIDEO_CFR, AUDIO_AAC, filename="/tmp/otra-ruta.mp4"))
        self.assertEqual(canonicalizar(canonico), canonicalizar(otro),
                         "dos rutas distintas con los mismos bytes producen el mismo documento")


# --------------------------------------------------------------------------- #
# Huellas
# --------------------------------------------------------------------------- #

class HuellaTests(unittest.TestCase):
    def test_un_archivo_pequeno_se_muestrea_entero_una_sola_vez(self):
        self.assertEqual(plan_parcial(500, ventana=1024)[0].longitud, 500)
        self.assertEqual(len(plan_parcial(500, ventana=1024)), 1)
        self.assertEqual(plan_parcial(0, ventana=1024), ())

    def test_las_ventanas_son_disjuntas_ordenadas_y_caben_en_el_archivo(self):
        for tamano in (1025, 4096, 10_000, 1_000_000):
            with self.subTest(tamano=tamano):
                ventanas = plan_parcial(tamano, ventana=1024)
                self.assertEqual(list(ventanas), sorted(ventanas, key=lambda v: v.desplazamiento))
                for anterior, siguiente in zip(ventanas, ventanas[1:]):
                    self.assertLessEqual(anterior.fin, siguiente.desplazamiento)
                self.assertLessEqual(ventanas[-1].fin, tamano)

    def test_el_plan_es_deterministico(self):
        self.assertEqual(plan_parcial(999_999, ventana=4096),
                         plan_parcial(999_999, ventana=4096))

    def test_el_tamano_entra_en_la_composicion(self):
        ventanas = plan_parcial(10_000, ventana=1024)
        digests = [digest_bloque(b"a")] * len(ventanas)
        self.assertNotEqual(componer_parcial(10_000, ventanas, digests),
                            componer_parcial(10_001, ventanas, digests))

    def test_la_identidad_exige_huella_completa(self):
        parcial = HuellaFuente(tamano=10, parcial="p")
        with self.assertRaises(ErrorHuellaIngesta):
            _ = parcial.identidad
        self.assertEqual(parcial.equivalente_a(parcial), False,
                         "sin huella completa la igualdad no se puede afirmar")

    def test_detecta_reemplazo_con_mismo_nombre_y_mismo_tamano(self):
        anterior = HuellaFuente(tamano=10, parcial="p1", completa=componer_completa(10, "a" * 64))
        actual = HuellaFuente(tamano=10, parcial="p2", completa=componer_completa(10, "b" * 64))
        self.assertTrue(detecta_reemplazo(anterior, actual))
        self.assertFalse(detecta_reemplazo(anterior, anterior))

    def test_una_version_de_huella_distinta_siempre_es_reemplazo(self):
        anterior = HuellaFuente(tamano=10, parcial="p", completa="c", version="clipsapp-huella/0")
        actual = HuellaFuente(tamano=10, parcial="p", completa="c")
        self.assertTrue(detecta_reemplazo(anterior, actual))

    def test_la_reconciliacion_barata_compara_tamano_y_parcial(self):
        base = HuellaFuente(tamano=10, parcial="p")
        self.assertTrue(base.reconciliable_con(HuellaFuente(tamano=10, parcial="p", completa="c")))
        self.assertFalse(base.reconciliable_con(HuellaFuente(tamano=11, parcial="p")))

    def test_rechaza_valores_que_no_puede_representar(self):
        for argumentos in ({"tamano": -1, "parcial": "p"}, {"tamano": 1, "parcial": ""},
                           {"tamano": 1, "parcial": "p", "completa": ""},
                           {"tamano": 1, "parcial": "p", "mtime_ns": "ayer"}):
            with self.subTest(argumentos=argumentos):
                with self.assertRaises(ErrorHuellaIngesta):
                    HuellaFuente(**argumentos)


# --------------------------------------------------------------------------- #
# Clave de materializacion
# --------------------------------------------------------------------------- #

def clave(**cambios):
    argumentos = {"stage": "ingesta.sondeo", "version_contrato": "1/metadata-1",
                  "version_proveedor": "ffprobe/8.0", "entradas": {"fuente": "huella-a"},
                  "config": {}}
    argumentos.update(cambios)
    return clave_materializacion(**argumentos)


class ClaveMaterializacionTests(unittest.TestCase):
    def test_el_orden_de_la_configuracion_no_parte_el_cache(self):
        self.assertEqual(clave(config={"a": 1, "b": [1, 2]}),
                         clave(config={"b": [1, 2], "a": 1}))

    def test_cambia_con_entradas_contrato_proveedor_y_configuracion(self):
        base = clave()
        self.assertNotEqual(base, clave(entradas={"fuente": "huella-b"}))
        self.assertNotEqual(base, clave(version_contrato="2/metadata-1"))
        self.assertNotEqual(base, clave(version_proveedor="ffprobe/7.1"))
        self.assertNotEqual(base, clave(config={"canal": "a"}))
        self.assertNotEqual(base, clave(stage="ingesta.otro"))

    def test_es_estable_entre_ejecuciones_y_usable_como_tramo_de_ruta(self):
        self.assertEqual(clave(), clave())
        self.assertTrue(es_clave_materializacion(clave()))
        self.assertFalse(es_clave_materializacion("mk1-no-hex"))
        self.assertFalse(es_clave_materializacion("../fuera"))

    def test_un_entero_escrito_como_decimal_es_la_misma_configuracion(self):
        self.assertEqual(clave(config={"n": 2}), clave(config={"n": 2.0}))

    def test_rechaza_lo_que_no_puede_representar_sin_ambiguedad(self):
        for invalida in ({"config": {"x": float("nan")}}, {"config": {"x": object()}},
                         {"config": {1: "clave-no-texto"}}, {"entradas": {}},
                         {"entradas": {"fuente": ""}}, {"version_proveedor": ""}):
            with self.subTest(invalida=invalida):
                with self.assertRaises(ErrorClaveMaterializacion):
                    clave(**invalida)

    def test_un_conjunto_se_ordena_por_su_forma_canonica(self):
        self.assertEqual(canonicalizar({"x": {"b", "a"}}), canonicalizar({"x": {"a", "b"}}))


# --------------------------------------------------------------------------- #
# Artefactos
# --------------------------------------------------------------------------- #

LINAJE = LinajeArtefacto(stage="ingesta.sondeo", version_contrato="1/metadata-1",
                         version_proveedor="ffprobe/8.0", clave=clave(),
                         entradas={"fuente": "huella-a"})


def registro(**cambios):
    argumentos = {"clave": clave(), "tipo": "probe", "rutas": ("probe.json",),
                  "checksums": {"probe.json": etiquetar_checksum("ab" * 32)}, "linaje": LINAJE}
    argumentos.update(cambios)
    return RegistroArtefacto(**argumentos)


class ArtefactoTests(unittest.TestCase):
    def test_reutiliza_solo_si_bytes_manifiesto_y_linaje_siguen_validos(self):
        presentes = {"probe.json": "ab" * 32}
        self.assertTrue(reutilizable(registro(), LINAJE, presentes))

    def test_no_reutiliza_con_bytes_distintos_ausentes_o_de_mas(self):
        for presentes in ({"probe.json": "cd" * 32}, {},
                          {"probe.json": "ab" * 32, "extra.json": "ef" * 32}):
            with self.subTest(presentes=presentes):
                self.assertFalse(reutilizable(registro(), LINAJE, presentes))

    def test_no_reutiliza_con_otro_linaje_ni_estado_reconstruible(self):
        otro = LinajeArtefacto(stage=LINAJE.stage, version_contrato=LINAJE.version_contrato,
                               version_proveedor="ffprobe/7.1", clave=LINAJE.clave,
                               entradas=LINAJE.entradas)
        presentes = {"probe.json": "ab" * 32}
        self.assertFalse(reutilizable(registro(), otro, presentes))
        self.assertFalse(reutilizable(registro(estado=ESTADO_RECONSTRUIBLE), LINAJE, presentes))

    def test_el_manifiesto_declara_checksums_en_la_clave_reservada(self):
        manifiesto = ManifiestoArtefacto(
            tipo="probe", archivos=(ArchivoArtefacto("probe.json", 3, "ab" * 32),), linaje=LINAJE)
        publicable = manifiesto.metadata_publicable()
        self.assertEqual(publicable["checksums"], {"probe.json": "sha256:" + "ab" * 32})
        self.assertEqual(publicable["kind"], "probe")
        self.assertEqual(publicable["linaje"]["version_proveedor"], "ffprobe/8.0")

    def test_un_archivo_de_artefacto_no_puede_escapar_del_directorio(self):
        for ruta in ("../fuera.json", "/absoluta.json", "C:/x.json", ""):
            with self.subTest(ruta=ruta):
                with self.assertRaises(ErrorArtefactoIngesta):
                    ArchivoArtefacto(ruta, 1, "ab" * 32)

    def test_el_manifiesto_rechaza_declarar_dos_veces_el_mismo_archivo(self):
        archivo = ArchivoArtefacto("probe.json", 3, "ab" * 32)
        with self.assertRaises(ErrorArtefactoIngesta):
            ManifiestoArtefacto(tipo="probe", archivos=(archivo, archivo), linaje=LINAJE)

    def test_un_linaje_incompleto_no_se_puede_construir(self):
        with self.assertRaises(ErrorArtefactoIngesta):
            LinajeArtefacto(stage="", version_contrato="1", version_proveedor="p", clave="k")


# --------------------------------------------------------------------------- #
# SourceAsset
# --------------------------------------------------------------------------- #

HUELLA = HuellaFuente(tamano=10, parcial="p", completa=componer_completa(10, "a" * 64),
                      mtime_ns=1)


def fuente(**cambios):
    argumentos = {"id": "s1", "nombre": "clip.mp4", "modo": ModoFuente.COPIADA,
                  "origen": OrigenFuente.LOCAL, "huella": HUELLA,
                  "ruta_relativa": "sources/abc_clip.mp4"}
    argumentos.update(cambios)
    return SourceAsset(**argumentos)


class SourceAssetTests(unittest.TestCase):
    def test_una_fuente_copiada_se_localiza_con_ruta_relativa_confinada(self):
        self.assertTrue(fuente().es_interna)
        for ruta in ("../fuera.mp4", "/abs.mp4", "C:/x.mp4", "sources/../../fuera.mp4"):
            with self.subTest(ruta=ruta):
                with self.assertRaises(ErrorFuenteIngesta):
                    fuente(ruta_relativa=ruta)

    def test_una_fuente_referenciada_conserva_su_ruta_normalizada(self):
        externa = fuente(modo=ModoFuente.REFERENCIADA, ruta_relativa=None,
                         ruta_externa="/media/externa/clip.mp4")
        self.assertFalse(externa.es_interna)
        with self.assertRaises(ErrorFuenteIngesta):
            fuente(modo=ModoFuente.REFERENCIADA, ruta_relativa=None, ruta_externa=None)

    def test_no_se_registra_una_fuente_sin_huella_completa(self):
        with self.assertRaises(ErrorFuenteIngesta):
            fuente(huella=HuellaFuente(tamano=10, parcial="p"))

    def test_la_referencia_de_apertura_lleva_lo_minimo_y_nada_mas(self):
        referencia = referencia_de_apertura(fuente())
        self.assertEqual(set(referencia), {"id", "modo", "origen", "ruta_relativa", "ruta_externa",
                                           "tamano", "mtime_ns", "huella_parcial",
                                           "version_huella"})
        self.assertNotIn("huella_completa", referencia)
        self.assertNotIn("metadata", referencia)


if __name__ == "__main__":
    unittest.main()
