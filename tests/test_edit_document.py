from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import hashlib
import json
import random
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from edit_fixtures import documento  # noqa: E402
from clipperkick.domain.edit.analysis import fusionar_analisis  # noqa: E402
from clipperkick.domain.edit.document import validar_assets  # noqa: E402
from clipperkick.domain.edit.errors import (  # noqa: E402
    ErrorAssetEdicion, ErrorCatalogoEdicion, ErrorDocumentoEdicion, ErrorEsquemaEdicion,
)
from clipperkick.domain.edit.patches import (  # noqa: E402
    CorregirPalabra, CorregirTextoCue, DividirCue, EstablecerTransicion, UnirCues,
    impacto_de,
)
from clipperkick.domain.edit.schema import (  # noqa: E402
    deserializar, migrar_documento, serializar, texto_canonico,
)
from clipperkick.domain.edit.timebase import Rango  # noqa: E402
from clipperkick.domain.edit.tracks import (  # noqa: E402
    CapaAudio, CaptionTrack, Cue, EffectTrack, EventoEfecto, ReferenciaAsset,
    Superposicion,
)


class RoundTripTests(unittest.TestCase):
    def test_round_trip_es_canonico_e_inmutable(self):
        original = documento(texto="¡Hola, ñandú!")
        texto = serializar(original)
        self.assertEqual(serializar(deserializar(texto)), texto)
        self.assertEqual(deserializar(texto), original)
        self.assertEqual(texto, json.dumps(json.loads(texto), ensure_ascii=False,
                                           sort_keys=True, separators=(",", ":")))
        with self.assertRaises(FrozenInstanceError):
            original.duracion = 1

    def test_snapshot_del_documento_completo_es_estable(self):
        digest = hashlib.sha256(serializar(documento()).encode("utf-8")).hexdigest()
        self.assertEqual(digest, "c7d0ca882589ae410471ae5dc7efb572a18ce01e5091f988d421a95d29f7cb17")

    def test_round_trip_conserva_ticks_exactos_en_muchos_limites(self):
        randomizer = random.Random(20260810)
        for index in range(100):
            duration = randomizer.randint(4_001, 50_000)
            original = documento(document_id=f"doc-{index}", duracion=duration)
            recovered = deserializar(serializar(original))
            self.assertEqual(recovered, original)
            self.assertIsInstance(recovered.duracion, int)

    def test_rechaza_version_futura_y_json_invalido(self):
        data = documento().como_documento()
        data["schema_version"] = 999
        with self.assertRaises(ErrorEsquemaEdicion):
            deserializar(texto_canonico(data))
        with self.assertRaises(ErrorEsquemaEdicion):
            deserializar("{")


class ValidacionesTests(unittest.TestCase):
    def test_rechaza_evento_fuera_de_duracion_y_parametro_desconocido(self):
        original = documento()
        with self.assertRaises(ErrorDocumentoEdicion):
            original.con(effect_track=EffectTrack(eventos=(
                EventoEfecto("fx", "zoom_punch", Rango(9_000, 11_000)),
            )))
        with self.assertRaises(ErrorCatalogoEdicion):
            EventoEfecto("fx", "zoom_punch", Rango(1, 2), {"inventado": 1})

    def test_rechaza_assets_ausentes_y_versiones_incorrectas(self):
        original = documento()
        asset = ReferenciaAsset("logo", "2", "grafico")
        with_asset = original.con(overlay_track=replace(
            original.overlay_track,
            elementos=(Superposicion("logo-1", asset, Rango(1_000, 2_000)),),
        ))
        with self.assertRaises(ErrorAssetEdicion):
            validar_assets(with_asset, {})
        with self.assertRaises(ErrorAssetEdicion):
            validar_assets(with_asset, {"logo": "1"})
        validar_assets(with_asset, {"logo": "2"})

    def test_rechaza_tipo_de_asset_incompatible_con_pista_y_registro(self):
        music = ReferenciaAsset("asset", "1", "musica")
        with self.assertRaises(ErrorDocumentoEdicion):
            Superposicion("overlay", music, Rango(1_000, 2_000))
        sfx = ReferenciaAsset("asset", "1", "sfx")
        with self.assertRaises(ErrorDocumentoEdicion):
            CapaAudio("bgm", asset=sfx)

        graphic = ReferenciaAsset("logo", "2", "grafico")
        original = documento()
        with_asset = original.con(overlay_track=replace(
            original.overlay_track,
            elementos=(Superposicion("logo-1", graphic, Rango(1_000, 2_000)),),
        ))
        with self.assertRaises(ErrorAssetEdicion):
            validar_assets(with_asset, {"logo": ("2", "musica")})
        with self.assertRaises(ErrorAssetEdicion):
            validar_assets(with_asset, {
                "logo": ReferenciaAsset("otro-logo", "2", "grafico")})

    def test_parametros_de_efecto_quedan_profunda_y_defensivamente_congelados(self):
        values = {"magnitud": 20}
        effect = EventoEfecto("fx", "zoom_punch", Rango(1_000, 2_000), values)
        values["magnitud"] = 90
        original = documento().con(effect_track=EffectTrack(eventos=(effect,)))
        self.assertEqual(original.effect_track.eventos[0].parametros["magnitud"], 20)
        with self.assertRaises(TypeError):
            original.effect_track.eventos[0].parametros["magnitud"] = 99


class MigracionTests(unittest.TestCase):
    def fixture_v1(self):
        return {
            "schema_version": 1,
            "document_id": "doc-v1",
            "source_id": "source-1",
            "preset": {"nombre": "gaming", "version": "1"},
            "source_segments": [{
                "id": "segment-1", "inicio_segundos": 1.0, "fin_segundos": 3.5,
            }],
            "framing_track": {"keyframes": [{
                "id": "frame-1", "segundo": 0,
                "ventana": {"x": 0, "y": 0, "ancho": 1000, "alto": 1000},
            }]},
            "caption_track": {"cues": [{
                "id": "cue-1", "inicio_segundos": 0.25, "fin_segundos": 1.5,
                "texto": "corregido", "palabras": [],
            }]},
        }

    def test_migra_version_anterior_a_ticks_y_protege_datos_como_manuales(self):
        migrated = migrar_documento(self.fixture_v1())
        recovered = deserializar(texto_canonico(self.fixture_v1()))
        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(recovered.duracion, recovered.timebase.ticks_por_segundo * 5 // 2)
        self.assertEqual(
            recovered.caption_track.cues[0].rango,
            Rango(recovered.timebase.ticks_por_segundo // 4,
                  recovered.timebase.ticks_por_segundo * 3 // 2),
        )
        self.assertIn("texto", recovered.caption_track.cues[0].manual)
        self.assertEqual(recovered.caption_track.cues[0].palabras, ())
        self.assertEqual(recovered.como_documento()["schema_version"], 3)
        self.assertEqual(serializar(recovered), serializar(deserializar(serializar(recovered))))

    def test_migra_v2_con_ids_de_palabra_deterministas_y_revision_desconocida(self):
        v2 = json.loads(serializar(documento()))
        v2["schema_version"] = 2
        v2.pop("revision_id")
        for cue in v2["caption_track"]["cues"]:
            for word in cue["palabras"]:
                word.pop("id")
        recovered = deserializar(texto_canonico(v2))
        self.assertEqual(tuple(word.id for word in recovered.caption_track.cues[0].palabras),
                         ("cue-1.palabra.0", "cue-1.palabra.1"))
        self.assertIsNone(recovered.revision_id)


class PatchesYOverridesTests(unittest.TestCase):
    def test_patch_de_texto_no_mueve_tiempos_y_marca_override(self):
        original = documento()
        changed = CorregirTextoCue("cue-1", "texto humano").aplicar(original)
        self.assertEqual(changed.caption_track.cues[0].rango,
                         original.caption_track.cues[0].rango)
        self.assertIn("texto", changed.caption_track.cues[0].manual)

    def test_rerun_respeta_override_y_actualiza_lo_automatico(self):
        original = documento()
        current = CorregirTextoCue("cue-1", "texto humano").aplicar(original)
        proposed_cue = replace(
            original.caption_track.cues[0], texto="texto automatico nuevo",
            oculto=True, manual=frozenset(),
        )
        proposed = original.con(caption_track=CaptionTrack(cues=(proposed_cue,)))
        merged = fusionar_analisis(current, proposed)
        self.assertEqual(merged.caption_track.cues[0].texto, "texto humano")
        self.assertTrue(merged.caption_track.cues[0].oculto)
        self.assertIn("texto", merged.caption_track.cues[0].manual)

    def test_impacto_de_lote_usa_el_estado_creado_por_el_patch_anterior(self):
        original = documento()
        patches = (
            DividirCue("cue-1", 2_000, "cue-2", "hola", "mundo"),
            CorregirTextoCue("cue-2", "mundo corregido"),
        )
        self.assertEqual(impacto_de(original, patches), Rango(750, 4_250))

    def test_rerun_no_confunde_override_de_transicion_con_otro_mapa_temporal(self):
        original = documento()
        current = EstablecerTransicion("segment-1", "fundido").aplicar(original)
        merged = fusionar_analisis(current, original)
        self.assertEqual(merged.source_segments[0].transicion_entrada, "fundido")
        self.assertIn("transicion_entrada", merged.source_segments[0].manual)

    def test_rerun_acepta_segmentos_migrados_con_el_mismo_mapa(self):
        migrated = deserializar(texto_canonico(MigracionTests().fixture_v1()))
        proposal = migrated.con(source_segments=tuple(
            replace(segment, manual=frozenset()) for segment in migrated.source_segments))
        self.assertEqual(fusionar_analisis(migrated, proposal).source_segments,
                         migrated.source_segments)

    def test_unir_cues_protege_la_lista_completa_frente_al_rerun(self):
        original = documento()
        split = DividirCue("cue-1", 2_000, "cue-2", "hola", "mundo").aplicar(original)
        joined = UnirCues("cue-1", "cue-2").aplicar(split)
        proposal = split.con(caption_track=replace(
            split.caption_track, manual=frozenset(),
            cues=tuple(replace(cue, manual=frozenset()) for cue in split.caption_track.cues),
        ))
        merged = fusionar_analisis(joined, proposal)
        self.assertEqual(tuple(cue.id for cue in merged.caption_track.cues), ("cue-1",))
        self.assertIn("cues", merged.caption_track.manual)

    def test_patch_de_palabra_sincroniza_texto_y_rerun_respeta_el_token(self):
        original = documento()
        changed = CorregirPalabra("cue-1", "word-2", "planeta").aplicar(original)
        self.assertEqual(changed.caption_track.cues[0].texto, "hola planeta")
        self.assertEqual(changed.caption_track.cues[0].palabras[1].rango,
                         original.caption_track.cues[0].palabras[1].rango)
        merged = fusionar_analisis(changed, original)
        self.assertEqual(merged.caption_track.cues[0].texto, "hola planeta")
        self.assertEqual(merged.caption_track.cues[0].palabras[1].texto, "planeta")


if __name__ == "__main__":
    unittest.main()
