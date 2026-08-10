"""Ranking puro: score explicable, separacion, bordes y estados recuperables."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipperkick.domain.analysis import (  # noqa: E402
    ConfiguracionRanking, ErrorConfiguracionAnalisis, EstadoAnalisis, SerieSonoridad,
    rankear, resultado_desde_documento, sin_solapamientos,
)


class RankingBasicoTests(unittest.TestCase):
    def test_fixture_con_picos_produce_momentos_separados_y_explicables(self):
        valores = [-30.0] * 80
        valores[18] = -8.0
        valores[53] = -6.0
        serie = SerieSonoridad(tuple(valores))
        config = ConfiguracionRanking(
            cantidad=2, duracion_segundos=12, margen_bordes_segundos=0,
            umbral_score=0.1)

        primero = rankear(serie, config, "fuente-1")
        segundo = rankear(serie, config, "fuente-1")

        self.assertEqual(primero, segundo)
        self.assertEqual(primero.estado, EstadoAnalisis.COMPLETO)
        self.assertEqual([momento.posicion for momento in primero.momentos], [1, 2])
        self.assertTrue(sin_solapamientos(primero.momentos))
        self.assertGreater(primero.momentos[0].score, 0)
        self.assertTrue(primero.momentos[0].componentes)
        self.assertTrue(primero.momentos[0].razones)
        componente = primero.momentos[0].componentes[0].como_documento()
        self.assertEqual(
            set(componente),
            {"senal", "valor", "unidad", "referencia", "escala",
             "normalizado", "peso", "aporte"})

    def test_audio_plano_es_estado_recuperable(self):
        resultado = rankear(
            SerieSonoridad((-22.0,) * 60),
            ConfiguracionRanking(cantidad=3, duracion_segundos=8,
                                 margen_bordes_segundos=0),
            "fuente-plana")
        self.assertEqual(resultado.estado, EstadoAnalisis.SIN_CANDIDATOS)
        self.assertEqual(resultado.momentos, ())

    def test_configuracion_no_admite_solapamientos(self):
        with self.assertRaises(ErrorConfiguracionAnalisis):
            ConfiguracionRanking(duracion_segundos=20, separacion_segundos=19)

    def test_margen_de_borde_descarta_el_pico_de_cabecera(self):
        valores = [-30.0] * 50
        valores[1] = -2.0
        valores[25] = -8.0
        resultado = rankear(
            SerieSonoridad(tuple(valores)),
            ConfiguracionRanking(cantidad=1, duracion_segundos=5,
                                 margen_bordes_segundos=5, umbral_score=0.1),
            "fuente")
        self.assertGreater(resultado.momentos[0].inicio_segundos, 10)

    def test_snapshot_del_manifiesto_se_relee_sin_perder_razones(self):
        valores = [-30.0] * 30
        valores[15] = -5.0
        resultado = rankear(
            SerieSonoridad(tuple(valores)),
            ConfiguracionRanking(cantidad=1, duracion_segundos=6,
                                 margen_bordes_segundos=0, umbral_score=0.1),
            "fuente")
        documento = resultado.como_documento({"cantidad": 1})
        releido = resultado_desde_documento(documento)
        self.assertEqual(releido, resultado)
        self.assertEqual(documento["version"], "clipsapp-candidato/1")
        self.assertEqual(documento["estado"], "complete")
        self.assertEqual(documento["candidatos"][0]["razones"], [
            razon.como_documento() for razon in resultado.momentos[0].razones])


if __name__ == "__main__":
    unittest.main()
