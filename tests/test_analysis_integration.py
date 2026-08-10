"""Proyecto real: artefactos, Moments, cache de extraccion y consultas."""

from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipperkick.application.analysis import CasoDeUsoAnalisis, SolicitudAnalisis  # noqa: E402
from clipperkick.domain.analysis import (  # noqa: E402
    CODIGO_ENRIQUECIMIENTO_OMITIDO, CODIGO_SIN_CANDIDATOS, ConfiguracionRanking,
    ConfiguracionRasgos, EstadoAnalisis,
)
from clipperkick.domain.ingest import (  # noqa: E402
    HuellaFuente, ModoFuente, OrigenFuente, SourceAsset,
)
from clipperkick.infrastructure.analysis import (  # noqa: E402
    AlmacenArtefactosAnalisisProyecto, RepositorioSqliteAnalisis,
)
from clipperkick.infrastructure.ingest.repository import RepositorioSqliteFuentes  # noqa: E402
from clipperkick.infrastructure.ingest.sources import AlmacenFuentesProyecto  # noqa: E402
from clipperkick.infrastructure.project.persistence import crear_proyecto  # noqa: E402


class _Extractor:
    def __init__(self):
        self.llamadas = 0

    def version(self):
        return "extractor-prueba/1"

    def extraer(self, origen, destino, config, cancelado=None):
        self.llamadas += 1
        Path(destino).write_bytes(Path(origen).read_bytes() + b"-audio")


class _Medidor:
    def __init__(self, plana=False):
        self.llamadas = 0
        self.plana = plana

    def version(self):
        return "medidor-prueba/1"

    def medir(self, ruta, config, cancelado=None):
        self.llamadas += 1
        valores = [-24.0] * 70
        if not self.plana:
            valores[16] = -5.0
            valores[50] = -3.0
        return {"muestras": [[indice, valor] for indice, valor in enumerate(valores)],
                "duracion_segundos": 70.0, "unidad": "LUFS"}


class _SenalFallida:
    def nombre(self):
        return "transcript"

    def version(self):
        return "1"

    def senal(self, ruta_fuente, ruta_audio):
        raise RuntimeError("capacidad ausente")


class AnalisisProyectoTests(unittest.TestCase):
    def setUp(self):
        self.temporal = tempfile.TemporaryDirectory()
        self.raiz = Path(self.temporal.name) / "fixture.clipsapp"
        self.proyecto = crear_proyecto(self.raiz, "Fixture")
        self.ruta_fuente = Path(self.temporal.name) / "fuente.bin"
        self.ruta_fuente.write_bytes(b"fixture-audio")
        self.fuente = SourceAsset(
            id="fuente-analisis", nombre="fuente.bin", modo=ModoFuente.REFERENCIADA,
            origen=OrigenFuente.LOCAL,
            huella=HuellaFuente(tamano=self.ruta_fuente.stat().st_size,
                                parcial="parcial-fixture", completa="completa-fixture"),
            ruta_externa=str(self.ruta_fuente), entrada_original=str(self.ruta_fuente))
        RepositorioSqliteFuentes(self.proyecto.repositorio).registrar(self.fuente)
        self.artefactos = AlmacenArtefactosAnalisisProyecto(self.raiz)
        self.repositorio = RepositorioSqliteAnalisis(
            self.proyecto.repositorio, self.artefactos)

    def tearDown(self):
        self.proyecto.close()
        self.temporal.cleanup()

    def caso(self, medidor=None, senales=()):
        self.extractor = _Extractor()
        self.medidor = medidor or _Medidor()
        return CasoDeUsoAnalisis(
            localizador=AlmacenFuentesProyecto(self.raiz), extractor=self.extractor,
            medidor=self.medidor, artefactos=self.artefactos,
            repositorio=self.repositorio, senales_opcionales=senales)

    @staticmethod
    def solicitud(cantidad=2, duracion=10, pesos=None):
        ranking = ConfiguracionRanking(
            cantidad=cantidad, duracion_segundos=duracion, margen_bordes_segundos=0,
            umbral_score=0.1, pesos=pesos or {"pico": 1.0, "sostenido": 0.6,
                                              "contraste": 0.4})
        return SolicitudAnalisis(
            "fuente-analisis", ranking=ranking,
            rasgos=ConfiguracionRasgos(ventana_suavizado=1))

    def test_reranking_reutiliza_audio_y_rasgos_y_persiste_detalle(self):
        caso = self.caso()
        primero = caso.ejecutar(self.solicitud(cantidad=2, duracion=10))
        segundo = caso.ejecutar(self.solicitud(cantidad=1, duracion=12))
        tercero = caso.ejecutar(self.solicitud(cantidad=1, duracion=12))

        self.assertFalse(primero.audio_reutilizado)
        self.assertTrue(segundo.audio_reutilizado)
        self.assertTrue(segundo.rasgos_reutilizados)
        self.assertFalse(segundo.ranking_reutilizado)
        self.assertTrue(tercero.ranking_reutilizado)
        self.assertEqual((self.extractor.llamadas, self.medidor.llamadas), (1, 1))
        self.assertNotEqual(primero.clave_ranking, segundo.clave_ranking)
        self.assertEqual(primero.clave_audio, segundo.clave_audio)
        self.assertEqual(primero.clave_rasgos, segundo.clave_rasgos)

        ruta = Path(self.artefactos.ruta_publicada(
            primero.clave_ranking, "candidates.json"))
        documento = json.loads(ruta.read_text(encoding="utf-8"))
        candidato = documento["candidatos"][0]
        self.assertEqual(candidato["clave_analisis"], primero.clave_ranking)
        self.assertAlmostEqual(
            candidato["duracion_segundos"],
            candidato["fin_segundos"] - candidato["inicio_segundos"])
        self.assertTrue(candidato["componentes"])
        self.assertTrue(candidato["razones"])

    def test_consultas_paginadas_ordenadas_con_razones(self):
        resultado = self.caso().ejecutar(self.solicitud())
        pagina_1 = self.repositorio.consultar(
            clave=resultado.clave_ranking, limite=1, desplazamiento=0)
        pagina_2 = self.repositorio.consultar(
            clave=resultado.clave_ranking, limite=1, desplazamiento=1)

        self.assertEqual((pagina_1.total, len(pagina_1.momentos), pagina_1.hay_mas),
                         (2, 1, True))
        self.assertEqual((pagina_2.total, len(pagina_2.momentos), pagina_2.hay_mas),
                         (2, 1, False))
        self.assertGreaterEqual(pagina_1.momentos[0].score, pagina_2.momentos[0].score)
        self.assertTrue(pagina_1.momentos[0].razones)
        self.assertEqual(
            self.repositorio.momento(pagina_1.momentos[0].id), pagina_1.momentos[0])

    def test_audio_plano_publica_estado_recuperable(self):
        resultado = self.caso(_Medidor(plana=True)).ejecutar(self.solicitud())
        self.assertEqual(resultado.ranking.estado, EstadoAnalisis.SIN_CANDIDATOS)
        self.assertEqual(resultado.momentos, ())
        self.assertIn(CODIGO_SIN_CANDIDATOS,
                      [diagnostico.codigo for diagnostico in resultado.diagnosticos])

    def test_capacidad_opcional_fallida_no_interrumpe_y_queda_visible(self):
        pesos = {"pico": 1.0, "sostenido": 0.6, "contraste": 0.4,
                 "transcript": 0.5}
        resultado = self.caso(senales=(_SenalFallida(),)).ejecutar(
            self.solicitud(pesos=pesos))
        self.assertTrue(resultado.momentos)
        self.assertEqual(resultado.senales_omitidas, ("transcript",))
        self.assertIn(CODIGO_ENRIQUECIMIENTO_OMITIDO,
                      [diagnostico.codigo for diagnostico in resultado.diagnosticos])
        codigos = [razon.codigo for razon in resultado.momentos[0].razones]
        self.assertIn("analisis.razon.senal_ausente", codigos)


if __name__ == "__main__":
    unittest.main()
