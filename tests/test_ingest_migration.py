"""Migracion v5 -> v6: identidad de fuentes y materializacion de artefactos.

v6 no amplia v5 en el sitio: es una migracion versionada mas, con el mismo
respaldo, el mismo marcador y el mismo rollback que v2..v5. Lo que se prueba
aqui es que una imagen v5 real —construida desde el DDL de cada version, no
degradando un proyecto actual— sobrevive intacta, que una imagen que la
migracion no puede representar se detiene con diagnostico y devuelve el
proyecto anterior, y que una base v6 incompleta no llega a devolver repositorio.
"""

from pathlib import Path
from unittest import mock
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipperkick.application.ingest import SolicitudIngesta  # noqa: E402
from clipperkick.domain.ingest import ErrorFuenteIngesta  # noqa: E402
from clipperkick.domain.project import ErrorMigracionProyecto, ErrorProyecto  # noqa: E402
from clipperkick.infrastructure.ingest import RepositorioSqliteFuentes  # noqa: E402
from clipperkick.infrastructure.project import abrir_proyecto, crear_proyecto  # noqa: E402
from clipperkick.infrastructure.project import persistence  # noqa: E402
from clipperkick.infrastructure.project.persistence import (  # noqa: E402
    DIRECTORIOS_PROYECTO, NOMBRE_BASE, NOMBRE_MANIFIESTO, VERSION_ESQUEMA, _conexion, _sql_v1,
    _sql_v2, _sql_v3, _sql_v4, _sql_v5,
)

from test_ingest_use_case import VIDEO, DescargadorFalso, SondaFalsa  # noqa: E402


DDL_POR_VERSION = (_sql_v1, _sql_v2, _sql_v3, _sql_v4, _sql_v5)


class MigracionV6Tests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.base = Path(self.tempdir.name)
        self.raiz = self.base / "antiguo.clipsapp"

    # -- fixtures -------------------------------------------------------- #

    def semilla(self, fuentes=(), artefactos=(), version=5):
        """Imagen construida desde el DDL de cada version, como un proyecto real."""
        self.raiz.mkdir()
        for directorio in DIRECTORIOS_PROYECTO:
            (self.raiz / directorio).mkdir()
        (self.raiz / NOMBRE_BASE).touch()
        conexion = _conexion(self.raiz / NOMBRE_BASE)
        try:
            for aplicar in DDL_POR_VERSION[:version]:
                aplicar(conexion)
            conexion.execute(f"PRAGMA user_version={int(version)}")
            conexion.execute("INSERT INTO Project VALUES ('p', 'Antiguo', '{}')")
            for identificador, externa, huella in fuentes:
                conexion.execute(
                    "INSERT INTO SourceAsset (id, project_id, mode, relative_path, external_path,"
                    " fingerprint, state) VALUES (?, 'p', 'referenced', NULL, ?, ?, 'available')",
                    (identificador, externa, huella))
            for identificador, fuente, ruta in artefactos:
                conexion.execute(
                    "INSERT INTO AnalysisArtifact (id, source_id, relative_path, kind, state)"
                    " VALUES (?, ?, ?, 'probe', 'available')", (identificador, fuente, ruta))
            conexion.commit()
        finally:
            conexion.close()
        (self.raiz / NOMBRE_MANIFIESTO).write_text(
            json.dumps({"project_id": "p", "name": "Antiguo", "schema_version": version}),
            encoding="utf-8")
        return self.raiz

    def conexion(self, solo_lectura=False):
        return _conexion(self.raiz / NOMBRE_BASE, solo_lectura)

    def columnas(self, conexion, tabla):
        return {fila[1] for fila in conexion.execute(f"PRAGMA table_info({tabla})")}

    def indices(self, conexion, tabla):
        return {fila[1] for fila in conexion.execute(f"PRAGMA index_list({tabla})")}

    # -- migracion feliz -------------------------------------------------- #

    def test_una_v5_migra_conservando_fuentes_artefactos_y_su_historia(self):
        self.semilla(fuentes=(("s1", "D:/media/uno.mp4", "opaca-1"),),
                     artefactos=(("a1", "s1", "artifacts/viejo/probe.json"),))
        with abrir_proyecto(self.raiz) as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().schema_version, VERSION_ESQUEMA)
            conexion = proyecto.repositorio.conexion
            fila = conexion.execute(
                "SELECT fingerprint, fingerprint_version, source_uri, origin, size_bytes,"
                " supersedes FROM SourceAsset WHERE id='s1'").fetchone()
            self.assertEqual(fila[0], "opaca-1", "la huella heredada no se reescribe")
            self.assertEqual(fila[1], "legacy/0",
                             "una huella que esta version no sabe reproducir se marca como tal")
            self.assertEqual(fila[2], "D:/media/uno.mp4")
            self.assertEqual((fila[3], fila[4], fila[5]), ("local", None, None))
            artefacto = conexion.execute(
                "SELECT artifact_path, materialization_key, checksum, lineage_json"
                " FROM AnalysisArtifact WHERE id='a1'").fetchone()
            self.assertEqual(artefacto[0], "artifacts/viejo/probe.json")
            self.assertEqual((artefacto[1], artefacto[2], artefacto[3]), ("", "", "{}"))
            self.assertEqual(conexion.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conexion.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_la_migracion_declara_los_indices_que_hacen_unica_la_identidad(self):
        self.semilla()
        with abrir_proyecto(self.raiz) as proyecto:
            conexion = proyecto.repositorio.conexion
            self.assertIn("SourceAsset_identidad", self.indices(conexion, "SourceAsset"))
            self.assertIn("AnalysisArtifact_materializacion",
                          self.indices(conexion, "AnalysisArtifact"))
            self.assertLessEqual({"source_uri", "origin", "size_bytes", "mtime_ns",
                                  "fingerprint_partial", "fingerprint_version", "supersedes"},
                                 self.columnas(conexion, "SourceAsset"))
            self.assertLessEqual({"materialization_key", "artifact_path", "checksum",
                                  "lineage_json"},
                                 self.columnas(conexion, "AnalysisArtifact"))

    def test_dos_artefactos_sin_clave_conviven_y_dos_con_la_misma_no(self):
        """El indice es parcial: solo exige unicidad donde hay clave que exigir."""
        self.semilla(fuentes=(("s1", "D:/uno.mp4", "opaca-1"),),
                     artefactos=(("a1", "s1", "artifacts/x/probe.json"),
                                 ("a2", "s1", "artifacts/y/probe.json")))
        with abrir_proyecto(self.raiz) as proyecto:
            with proyecto.repositorio.transaccion() as tx:
                tx.execute("UPDATE AnalysisArtifact SET materialization_key='mk1-x',"
                           " artifact_path='probe.json' WHERE id='a1'")
            with self.assertRaises(ErrorProyecto):
                with proyecto.repositorio.transaccion() as tx:
                    tx.execute("UPDATE AnalysisArtifact SET materialization_key='mk1-x',"
                               " artifact_path='probe.json' WHERE id='a2'")

    def test_tras_migrar_se_puede_ingerir_con_normalidad(self):
        self.semilla()
        entrada = self.base / "clip.mp4"
        entrada.write_bytes(VIDEO)
        with abrir_proyecto(self.raiz) as proyecto:
            from clipperkick.infrastructure.ingest import crear_caso_de_uso_ingesta

            caso = crear_caso_de_uso_ingesta(proyecto, sonda=SondaFalsa(),
                                             descargador=DescargadorFalso(VIDEO))
            resultado = caso.ingerir(SolicitudIngesta(str(entrada)))
            self.assertTrue((self.raiz / resultado.artefactos[0]).is_file())

    # -- abortos con diagnostico ------------------------------------------ #

    def test_dos_fuentes_con_la_misma_identidad_detienen_la_migracion(self):
        self.semilla(fuentes=(("s1", "D:/uno.mp4", "misma"), ("s2", "D:/dos.mp4", "misma")))
        with self.assertRaises(ErrorMigracionProyecto) as capturado:
            abrir_proyecto(self.raiz)
        self.assertIn("identidad de contenido", str(capturado.exception.__cause__),
                      "el diagnostico debe nombrar el conflicto, no solo la restriccion")

        conexion = self.conexion()
        try:
            self.assertEqual(conexion.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(conexion.execute("SELECT COUNT(*) FROM SourceAsset").fetchone()[0], 2)
            self.assertNotIn("source_uri", self.columnas(conexion, "SourceAsset"))
        finally:
            conexion.close()
        self.assertEqual(json.loads((self.raiz / NOMBRE_MANIFIESTO).read_text("utf-8"))
                         ["schema_version"], 5)

    def test_un_corte_a_mitad_de_v6_deja_la_v5_reabrible(self):
        self.semilla(fuentes=(("s1", "D:/uno.mp4", "opaca-1"),))

        def cortar(version):
            if version == 6:
                raise OSError(28, "No space left on device")

        with self.assertRaises(ErrorMigracionProyecto):
            abrir_proyecto(self.raiz, cortar)
        conexion = self.conexion()
        try:
            self.assertEqual(conexion.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertNotIn("materialization_key", self.columnas(conexion, "AnalysisArtifact"))
        finally:
            conexion.close()
        # Y la siguiente apertura, sin el corte, la completa sin ayuda.
        with abrir_proyecto(self.raiz) as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().schema_version, VERSION_ESQUEMA)

    def test_una_v6_sin_su_indice_de_identidad_no_devuelve_repositorio(self):
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            proyecto.repositorio.conexion.execute("DROP INDEX SourceAsset_identidad")
            proyecto.repositorio.conexion.commit()
        with self.assertRaises(ErrorProyecto):
            abrir_proyecto(self.raiz)

    def test_una_v6_sin_las_columnas_de_su_version_no_devuelve_repositorio(self):
        self.semilla(version=5)
        conexion = self.conexion()
        try:
            conexion.execute("PRAGMA user_version=6")  # dice v6 y trae esquema v5
            conexion.commit()
        finally:
            conexion.close()
        manifiesto = json.loads((self.raiz / NOMBRE_MANIFIESTO).read_text("utf-8"))
        manifiesto["schema_version"] = 6
        (self.raiz / NOMBRE_MANIFIESTO).write_text(json.dumps(manifiesto), encoding="utf-8")
        with self.assertRaises(ErrorProyecto):
            abrir_proyecto(self.raiz)

    # -- solo lectura ----------------------------------------------------- #

    def test_una_instancia_sin_lock_no_migra_ni_escribe(self):
        self.semilla(fuentes=(("s1", "D:/uno.mp4", "opaca-1"),))
        with mock.patch.object(persistence, "_adquirir_lock", lambda _raiz: None):
            with abrir_proyecto(self.raiz) as proyecto:
                self.assertTrue(proyecto.solo_lectura)
                self.assertEqual(proyecto.repositorio.informacion().schema_version, 5)
        conexion = self.conexion()
        try:
            self.assertEqual(conexion.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertNotIn("source_uri", self.columnas(conexion, "SourceAsset"))
        finally:
            conexion.close()


class FuentesHeredadasTests(unittest.TestCase):
    """Una fila migrada de v5 no tiene con que reconciliarse: hay que reingerirla."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.base = Path(self.tempdir.name)

    def test_una_fuente_sin_huella_reproducible_se_rechaza_con_diagnostico(self):
        raiz = self.base / "proyecto.clipsapp"
        with crear_proyecto(raiz, "Demo") as proyecto:
            with proyecto.repositorio.transaccion() as tx:
                tx.execute(
                    "INSERT INTO SourceAsset (id, project_id, mode, relative_path, external_path,"
                    " fingerprint, state, source_uri, fingerprint_version)"
                    " VALUES ('vieja', ?, 'referenced', NULL, 'D:/uno.mp4', 'opaca', 'available',"
                    " 'D:/uno.mp4', 'legacy/0')",
                    (proyecto.repositorio.informacion().project_id,))
            repositorio = RepositorioSqliteFuentes(proyecto.repositorio)
            with self.assertRaises(ErrorFuenteIngesta) as capturado:
                repositorio.buscar_por_identidad("opaca")
            self.assertIn("vuelve a ingerirla", str(capturado.exception))


if __name__ == "__main__":
    unittest.main()
