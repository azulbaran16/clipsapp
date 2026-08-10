from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from edit_fixtures import documento  # noqa: E402
from clipperkick.application.edit import ServicioEdicion  # noqa: E402
from clipperkick.domain.edit.errors import ErrorHistorialEdicion  # noqa: E402
from clipperkick.domain.edit.history import EditRevision  # noqa: E402
from clipperkick.domain.edit.patches import (  # noqa: E402
    CorregirTextoCue, EstablecerIntensidad,
)
from clipperkick.domain.edit.schema import serializar  # noqa: E402
from clipperkick.infrastructure.edit import RepositorioSqliteEdicion  # noqa: E402
from clipperkick.infrastructure.project import crear_proyecto  # noqa: E402


class Ids:
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 1
        return f"id-{self.value:03d}"


class Clock:
    def __init__(self):
        self.value = 0

    def __call__(self):
        self.value += 1
        return f"2026-08-10T00:00:{self.value:02d}+00:00"


class BaseHistory(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.open_project = crear_proyecto(Path(self.temp.name) / "edit.clipsapp", "Edit")
        with self.open_project.repositorio.transaccion() as connection:
            connection.execute(
                "INSERT INTO Moment (id, source_id, start_seconds, end_seconds, score)"
                " VALUES ('moment-1', NULL, 0, 10, 1)")
        self.repository = RepositorioSqliteEdicion(self.open_project.repositorio)
        self.service = ServicioEdicion(self.repository, Ids(), Clock())
        self.created = self.service.crear_borrador("moment-1", documento())

    def tearDown(self):
        self.open_project.close()
        self.temp.cleanup()


class HistoryTests(BaseHistory):
    def test_cada_confirmacion_es_atomica_y_las_revisiones_anteriores_no_cambian(self):
        root_text = serializar(self.created.revision.document)
        revision = self.service.confirmar(
            self.created.variant.id, (CorregirTextoCue("cue-1", "humano"),))
        self.assertEqual(self.repository.obtener_variant(self.created.variant.id).current_revision_id,
                         revision.id)
        self.assertEqual(revision.document.revision_id, revision.id)
        self.assertEqual(serializar(self.repository.obtener_revision(
            self.created.revision.id).document), root_text)
        self.assertEqual(len(self.service.historial(self.created.variant.id)), 2)

    def test_undo_redo_mueven_punteros_sin_mutar_revisiones(self):
        changed = self.service.confirmar(
            self.created.variant.id, (CorregirTextoCue("cue-1", "humano"),))
        undone = self.service.deshacer(self.created.variant.id)
        self.assertEqual(undone.id, self.created.revision.id)
        self.assertEqual(self.repository.obtener_revision(changed.id).document
                         .caption_track.cues[0].texto, "humano")
        redone = self.service.rehacer(self.created.variant.id)
        self.assertEqual(redone.id, changed.id)

    def test_editar_tras_undo_crea_rama_y_redo_prefiere_la_mas_reciente(self):
        old = self.service.confirmar(
            self.created.variant.id, (CorregirTextoCue("cue-1", "rama vieja"),))
        self.service.deshacer(self.created.variant.id)
        new = self.service.confirmar(
            self.created.variant.id, (CorregirTextoCue("cue-1", "rama nueva"),))
        self.assertNotEqual(old.id, new.id)
        self.assertEqual(self.repository.obtener_revision(old.id).document
                         .caption_track.cues[0].texto, "rama vieja")
        self.service.deshacer(self.created.variant.id)
        self.assertEqual(self.service.rehacer(self.created.variant.id).id, new.id)

    def test_duplicar_variante_comparte_snapshot_y_luego_es_independiente(self):
        original = self.created.variant
        duplicate = self.service.duplicar_variant(original.id, "Alternativa")
        self.assertEqual(duplicate.current_revision_id, original.current_revision_id)
        duplicate_revision = self.service.confirmar(
            duplicate.id, (EstablecerIntensidad(80),))
        self.assertEqual(duplicate_revision.document.intensidad, 80)
        self.assertEqual(self.repository.obtener_revision(
            self.repository.obtener_variant(original.id).current_revision_id
        ).document.intensidad, 50)
        count = self.open_project.repositorio.conexion.execute(
            "SELECT COUNT(*) FROM EditRevision").fetchone()[0]
        self.assertEqual(count, 2)

    def test_duplicar_desde_revision_no_raiz_tiene_undo_y_redo_propios(self):
        original_head = self.service.confirmar(
            self.created.variant.id, (EstablecerIntensidad(65),))
        duplicate = self.service.duplicar_variant(self.created.variant.id, "Rama")
        fork = self.repository.obtener_revision(duplicate.current_revision_id)
        self.assertNotEqual(fork.id, original_head.id)
        self.assertEqual(fork.document.con(revision_id=None),
                         original_head.document.con(revision_id=None))
        self.assertEqual(self.service.deshacer(duplicate.id).id, self.created.revision.id)
        self.assertEqual(self.service.rehacer(duplicate.id).id, fork.id)

        branch = self.service.confirmar(duplicate.id, (EstablecerIntensidad(80),))
        self.assertEqual(self.service.deshacer(duplicate.id).id, fork.id)
        self.assertEqual(self.service.rehacer(duplicate.id).id, branch.id)

    def test_restaurar_automatico_crea_revision_reversible(self):
        changed = self.service.confirmar(
            self.created.variant.id, (CorregirTextoCue("cue-1", "humano"),))
        restored = self.service.restaurar_automatico(self.created.variant.id)
        self.assertEqual(restored.document.con(revision_id=None),
                         self.created.revision.document.con(revision_id=None))
        self.assertEqual(restored.document.revision_id, restored.id)
        self.assertEqual(self.service.deshacer(self.created.variant.id).id, changed.id)

    def test_autosave_agrupa_rafaga_y_cierra_al_cambiar_de_control(self):
        autosave = self.service.autoguardado(self.created.variant.id)
        self.assertIsNone(autosave.add(CorregirTextoCue("cue-1", "h")))
        self.assertIsNone(autosave.add(CorregirTextoCue("cue-1", "hola")))
        saved = autosave.add(EstablecerIntensidad(70))
        self.assertIsNotNone(saved)
        self.assertEqual(saved.document.caption_track.cues[0].texto, "hola")
        final = autosave.flush()
        self.assertEqual(final.document.intensidad, 70)
        self.assertEqual(len(self.service.historial(self.created.variant.id)), 3)

    def test_noop_y_limites_de_undo_redo_son_errores_tipados(self):
        with self.assertRaises(ErrorHistorialEdicion):
            self.service.confirmar(self.created.variant.id, (EstablecerIntensidad(50),))
        with self.assertRaises(ErrorHistorialEdicion):
            self.service.deshacer(self.created.variant.id)
        with self.assertRaises(ErrorHistorialEdicion):
            self.service.rehacer(self.created.variant.id)

    def test_compare_and_swap_no_deja_revision_huerfana(self):
        first = self.service.confirmar(
            self.created.variant.id, (EstablecerIntensidad(60),))
        stale = EditRevision(
            id="stale", draft_id=self.created.draft.id,
            variant_id=self.created.variant.id,
            parent_revision_id=self.created.revision.id,
            document=documento().con(intensidad=70, revision_id="stale"),
            created_at="2026-08-10T01:00:00+00:00",
        )
        with self.assertRaises(ErrorHistorialEdicion):
            self.repository.confirmar_revision(
                self.created.variant.id, self.created.revision.id, stale)
        self.assertEqual(self.repository.obtener_variant(
            self.created.variant.id).current_revision_id, first.id)
        self.assertEqual(self.open_project.repositorio.conexion.execute(
            "SELECT COUNT(*) FROM EditRevision WHERE id='stale'").fetchone()[0], 0)

    def test_fallo_sql_hace_rollback_del_alta_completa(self):
        before = self.open_project.repositorio.conexion.execute(
            "SELECT COUNT(*) FROM EditRevision").fetchone()[0]
        with self.assertRaises((ErrorHistorialEdicion, sqlite3.Error)):
            self.service.crear_borrador("moment-inexistente", documento(document_id="doc-2"))
        after = self.open_project.repositorio.conexion.execute(
            "SELECT COUNT(*) FROM EditRevision").fetchone()[0]
        self.assertEqual(after, before)

    def test_lee_fila_v6_temprana_que_guardaba_el_documento_sin_sobre(self):
        raw_id = "raw-revision"
        with self.open_project.repositorio.transaccion() as connection:
            connection.execute(
                "INSERT INTO Draft (id, moment_id, current_revision_id)"
                " VALUES ('raw-draft', 'moment-1', NULL)")
            connection.execute(
                "INSERT INTO EditRevision (id, draft_id, document_json, created_at)"
                " VALUES (?, 'raw-draft', ?, '2026-01-01')",
                (raw_id, serializar(documento(document_id="raw-doc"))),
            )
            connection.execute(
                "UPDATE Draft SET current_revision_id=? WHERE id='raw-draft'", (raw_id,))
            connection.execute(
                "INSERT INTO Variant (id, draft_id, name, current_revision_id)"
                " VALUES ('raw-variant', 'raw-draft', 'Anterior', ?)", (raw_id,))
        recovered = self.repository.obtener_revision(raw_id)
        self.assertEqual(recovered.document.document_id, "raw-doc")
        self.assertEqual(recovered.reason, "migrada")


if __name__ == "__main__":
    unittest.main()
