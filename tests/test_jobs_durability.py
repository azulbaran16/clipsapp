"""T03-F01: durabilidad de la publicacion, migracion versionada y bordes tipados.

Cada clase corresponde a un hallazgo de la revision adversarial. Donde el fallo
solo existe con un proceso de verdad —el huerfano que sobrevive a su padre— se
usa un proceso de verdad; donde lo que falla es un orden de barreras o una regla
de migracion, se observa ese orden o esa regla directamente.
"""

from pathlib import Path
from unittest import mock
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# `discover` anade tests/ al path, pero `unittest tests.modulo` no: se anade
# aqui para que el helper compartido se resuelva con ambas invocaciones.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from clipperkick.domain.jobs import (  # noqa: E402
    CODIGO_CANCELADO, ArchivoSalida, ErrorJob, ErrorValidacionSalida, EstadoJob,
    EventoProgreso, Job,
    ManifiestoSalida, PoliticaCancelacion, PoliticaRetry, Recursos, StageDefinition, TipoEvento,
    clave_canonica, es_componente_interno, exigir_componente_interno,
)
from clipperkick.domain.project import ErrorMigracionProyecto, ErrorProyecto  # noqa: E402
from clipperkick.infrastructure.jobs import (  # noqa: E402
    AlmacenArtefactosProyecto, RepositorioSqliteJobs, crear_coordinador,
)
from clipperkick.infrastructure.jobs.artifacts import NOMBRE_PUBLICACION  # noqa: E402
from clipperkick.infrastructure.project import abrir_proyecto, crear_proyecto  # noqa: E402
from clipperkick.infrastructure.project import persistence  # noqa: E402
from clipperkick.infrastructure.project.persistence import (  # noqa: E402
    DIRECTORIOS_PROYECTO, NOMBRE_BASE, NOMBRE_MANIFIESTO, VERSION_ESQUEMA, _conexion, _sql_v1,
    _sql_v2, _sql_v3, _sql_v4,
)

from test_jobs_lifecycle import proceso_vivo  # noqa: E402


SRC = str(Path(__file__).resolve().parents[1] / "src")
ENTORNO = dict(os.environ, PYTHONPATH=SRC, PYTHONDONTWRITEBYTECODE="1")
LIMITE = 90.0
DUENIO = "coordinador-a"

STAGE = StageDefinition(
    nombre="prueba", version_contrato="1", salidas=("salida.txt",), recursos=Recursos(cpu=1),
    soporta_checkpoint=True, politica_retry=PoliticaRetry(max_intentos=2),
    politica_cancelacion=PoliticaCancelacion(timeout_cooperativo=0.4, timeout_arranque=30.0))


# El caso exacto del hallazgo: el worker recibe la cancelacion, es sordo, y su
# coordinador muere de golpe. Consumir el control no puede apagar la vigilancia.
HIJO_CANCELA_Y_MUERE = """
import os, sys, time
from clipperkick.domain.jobs import Job, PoliticaCancelacion, PoliticaRetry, Recursos, StageDefinition
from clipperkick.infrastructure.jobs import crear_coordinador
from clipperkick.infrastructure.project import abrir_proyecto

raiz, job_id = sys.argv[1], sys.argv[2]
stage = StageDefinition(nombre="prueba", version_contrato="1", recursos=Recursos(cpu=1),
                        politica_retry=PoliticaRetry(max_intentos=3),
                        politica_cancelacion=PoliticaCancelacion(timeout_cooperativo=600.0))
proyecto = abrir_proyecto(raiz)
coordinador = crear_coordinador(proyecto, catalogo={"prueba": stage})
coordinador.encolar(Job(id=job_id, stage="prueba", version_contrato="1", max_intentos=3,
                        payload={"pasos": 1, "retardo": 600.0, "respetar_cancelacion": False}))
limite = time.monotonic() + 60
pid = None
while pid is None and time.monotonic() < limite:
    coordinador.paso()
    intentos = coordinador.intentos(job_id)
    pid = intentos[0].pid if intentos else None
    time.sleep(0.01)
if pid is None:
    print("SIN_PID", flush=True); os._exit(1)
# INICIO ya llego (hay pid): se cancela y se deja que el listener lo consuma.
coordinador.solicitar_cancelacion(job_id)
coordinador.paso()
time.sleep(0.5)
print(pid, flush=True)
os._exit(17)
"""


def historia(job_id="j1", clave="prueba", tipo=None):
    """Evento minimo para los comandos que exigen historia.

    Las pruebas de este modulo no observan el contenido del evento salvo cuando
    lo dicen explicitamente; lo que el contrato exige es que exista.
    """
    return EventoProgreso(job_id=job_id, tipo=tipo or TipoEvento.FIN, instante=1.0, clave=clave)


def historias(job_id="j1", clave="prueba", tipo=None):
    return (historia(job_id, clave, tipo),)


class BaseDurabilidad(unittest.TestCase):
    def setUp(self):
        self.temporal = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporal.cleanup)
        self.base = Path(self.temporal.name)
        self.raiz = self.base / "demo.clipsapp"


# --------------------------------------------------------------------------- #
# (1) Consumir la cancelacion no apaga la vigilancia del padre
# --------------------------------------------------------------------------- #

class VigilanciaTrasCancelarTests(BaseDurabilidad):
    def setUp(self):
        super().setUp()
        crear_proyecto(self.raiz, "Demo").close()

    def test_un_worker_sordo_ya_cancelado_muere_con_su_coordinador(self):
        """El agujero estaba justo aqui: `cancelado.set()` y dejar de leer.

        Sin lector no hay EOF, y sin EOF el worker no se entera de que su
        coordinador murio. El stage es sordo a proposito —ignora la cancelacion—
        para que lo unico que pueda retirarlo sea la vigilancia del canal.
        """
        proceso = subprocess.run(
            [sys.executable, "-c", HIJO_CANCELA_Y_MUERE, str(self.raiz), "j1"],
            stdout=subprocess.PIPE, text=True, env=ENTORNO, timeout=LIMITE)
        self.assertEqual(proceso.returncode, 17, proceso.stdout)
        pid = int(proceso.stdout.strip())
        self.addCleanup(self._asegurar_muerto, pid)

        limite = time.monotonic() + 30
        while proceso_vivo(pid) and time.monotonic() < limite:
            time.sleep(0.02)
        self.assertFalse(proceso_vivo(pid),
                         f"el worker {pid} sobrevivio tras consumir la cancelacion")

    def _asegurar_muerto(self, pid):
        """Red de seguridad: solo alcanza al PID que esta prueba engendro."""
        if proceso_vivo(pid):
            try:
                os.kill(pid, 9)
            except OSError:
                pass


# --------------------------------------------------------------------------- #
# (2) La publicacion ordena sus barreras
# --------------------------------------------------------------------------- #

class BarrerasDePublicacionTests(BaseDurabilidad):
    def setUp(self):
        super().setUp()
        crear_proyecto(self.raiz, "Demo").close()
        self.trabajo = self.raiz / "cache" / "jobs" / "j1" / "intento"
        self.trabajo.mkdir(parents=True)
        (self.trabajo / "salida.txt").write_bytes(b"contenido")

    def publicar_observando(self):
        """Registra *que* se sincroniza y cuando, no cuantas veces.

        Contar `fsync` no distingue una barrera de otra: basta con que sobre
        alguna para que el numero cuadre aunque falte la que importa. Se observa
        cada primitiva por separado y sobre que ruta actua, de modo que retirar
        cualquiera de ellas deja de cumplir el orden que se afirma.
        """
        eventos = []
        from clipperkick.infrastructure.jobs import artifacts

        def envolver(nombre, etiqueta, indice=0):
            real = getattr(artifacts, nombre)

            def espia(*argumentos, **claves):
                eventos.append((etiqueta, Path(argumentos[indice]).name))
                return real(*argumentos, **claves)
            return mock.patch.object(artifacts, nombre, espia)

        replace_real = os.replace

        def espiar_replace(origen, destino):
            eventos.append(("replace", Path(destino).name))
            return replace_real(origen, destino)

        almacen = AlmacenArtefactosProyecto(self.raiz)
        manifiesto = ManifiestoSalida((ArchivoSalida("salida.txt", 9),), {"kind": "prueba"})
        with (envolver("sincronizar_archivo", "archivo"),
              envolver("escribir_bytes_durable", "sidecar"),
              envolver("sincronizar_arbol", "arbol"),
              envolver("sincronizar_directorio", "directorio"),
              envolver("copiar_sellado", "copia", indice=1),
              mock.patch("os.replace", espiar_replace)):
            publicados = almacen.publicar("j1", str(self.trabajo), manifiesto,
                                          attempt_id="i1", clave="k1")
        return eventos, publicados

    def antes_de_publicar(self, eventos):
        publicacion = eventos.index(("replace", "j1"))
        return eventos[:publicacion], eventos[publicacion:]

    def test_los_bytes_llegan_a_disco_antes_de_publicarse(self):
        """Un rename atomico no es un rename durable.

        Sin esta barrera, un corte de energia justo despues del rename dejaria un
        directorio publicado cuyos archivos son ceros: la entrada de directorio
        sobrevivio y los datos no.
        """
        eventos, publicados = self.publicar_observando()
        self.assertEqual(publicados, ("artifacts/j1/salida.txt",))
        antes, _ = self.antes_de_publicar(eventos)
        self.assertIn(("archivo", "salida.txt"), antes,
                      f"la salida se publico sin haberse sincronizado: {eventos}")

    def test_el_sidecar_se_escribe_de_forma_durable_antes_de_publicarse(self):
        eventos, _ = self.publicar_observando()
        antes, _ = self.antes_de_publicar(eventos)
        self.assertIn(("sidecar", NOMBRE_PUBLICACION), antes,
                      f"la identidad de la publicacion no es durable: {eventos}")

    def test_el_staging_se_confirma_entero_antes_del_rename(self):
        """El staging lleva sufijo aleatorio: se reconoce por prefijo, no por nombre."""
        eventos, _ = self.publicar_observando()
        antes, _ = self.antes_de_publicar(eventos)
        arboles = [nombre for etiqueta, nombre in antes
                   if etiqueta == "arbol" and nombre.startswith("j1.publicando")]
        self.assertTrue(arboles,
                        f"el directorio publicado no se confirmo antes de publicarlo: {eventos}")

    def test_la_entrada_del_directorio_padre_se_confirma_despues(self):
        eventos, _ = self.publicar_observando()
        _, despues = self.antes_de_publicar(eventos)
        self.assertIn(("directorio", "artifacts"), despues,
                      f"falta la barrera posterior al rename: {eventos}")

    def test_un_solo_rename_publica_el_conjunto(self):
        eventos, _ = self.publicar_observando()
        self.assertEqual([evento for evento in eventos if evento[0] == "replace"].count(
            ("replace", "j1")), 1)
        antes, _ = self.antes_de_publicar(eventos)
        self.assertIn(("copia", "salida.txt"), antes,
                      "los archivos entran al staging antes de que el staging se publique")
        publicado = self.raiz / "artifacts" / "j1"
        self.assertEqual((publicado / "salida.txt").read_bytes(), b"contenido")
        self.assertTrue((publicado / NOMBRE_PUBLICACION).is_file())

    def test_la_salida_publicada_estrena_inode_y_no_el_del_productor(self):
        """El sellado copia: un descriptor retenido por el worker no la alcanza."""
        producido = (self.trabajo / "salida.txt").stat()
        self.publicar_observando()
        publicado = (self.raiz / "artifacts" / "j1" / "salida.txt").stat()
        self.assertNotEqual((publicado.st_dev, publicado.st_ino),
                            (producido.st_dev, producido.st_ino),
                            "publicar el mismo inode deja los bytes al alcance del productor")

    def test_el_digest_es_la_ultima_lectura_del_conjunto_sellado(self):
        """Entre digerir y renombrar solo cabe escribir el sidecar y su directorio."""
        eventos, _ = self.publicar_observando()
        antes, _ = self.antes_de_publicar(eventos)
        arbol = max(indice for indice, (etiqueta, _n) in enumerate(antes) if etiqueta == "arbol")
        sidecar = max(indice for indice, (etiqueta, _n) in enumerate(antes)
                      if etiqueta == "sidecar")
        self.assertLess(arbol, sidecar,
                        f"el recorrido del arbol tiene que preceder al digest: {eventos}")
        posteriores = {etiqueta for etiqueta, _n in antes[sidecar + 1:]}
        self.assertLessEqual(posteriores, {"directorio"},
                             f"nada vuelve a tocar los bytes certificados: {eventos}")


class PrimitivasDurablesTests(unittest.TestCase):
    """Las barreras tienen que *hacer* algo, no solo llamarse.

    Las pruebas de orden observan los puntos de llamada; estas observan que cada
    primitiva llega de verdad al sistema. Sin ambas, vaciar el cuerpo de una
    funcion dejaria el protocolo intacto sobre el papel y sin efecto en disco.
    """

    def setUp(self):
        self.temporal = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporal.cleanup)
        self.base = Path(self.temporal.name)

    def test_sincronizar_archivo_llega_al_sistema(self):
        from clipperkick.infrastructure import durability

        objetivo = self.base / "dato.bin"
        objetivo.write_bytes(b"x" * 32)
        with mock.patch("os.fsync") as espia:
            durability.sincronizar_archivo(objetivo)
        self.assertEqual(espia.call_count, 1)

    def test_escribir_bytes_durable_escribe_y_sincroniza(self):
        from clipperkick.infrastructure import durability

        objetivo = self.base / "sidecar.json"
        with mock.patch("os.fsync") as espia:
            durability.escribir_bytes_durable(objetivo, b"{}")
        self.assertEqual(objetivo.read_bytes(), b"{}")
        self.assertEqual(espia.call_count, 1, "escribir sin sincronizar no es durable")

    def test_sincronizar_arbol_alcanza_a_cada_archivo(self):
        from clipperkick.infrastructure import durability

        raiz = self.base / "arbol"
        (raiz / "sub").mkdir(parents=True)
        (raiz / "a.bin").write_bytes(b"a")
        (raiz / "sub" / "b.bin").write_bytes(b"b")
        vistos = []
        real = durability.sincronizar_archivo
        with mock.patch.object(durability, "sincronizar_archivo",
                               lambda ruta: (vistos.append(ruta.name), real(ruta))[1]):
            durability.sincronizar_arbol(raiz)
        self.assertEqual(sorted(vistos), ["a.bin", "b.bin"])

    def test_en_windows_el_directorio_no_se_abre_y_no_falla(self):
        from clipperkick.infrastructure import durability

        raiz = self.base / "carpeta"
        raiz.mkdir()
        with mock.patch("os.fsync") as espia:
            durability.sincronizar_directorio(raiz)
        if os.name == "nt":
            self.assertEqual(espia.call_count, 0,
                             "en Windows no hay descriptor de directorio que sincronizar")
        else:
            self.assertEqual(espia.call_count, 1)


# --------------------------------------------------------------------------- #
# (3) La huella identifica los bytes, no solo su longitud
# --------------------------------------------------------------------------- #

class HuellaDeContenidoTests(BaseDurabilidad):
    def setUp(self):
        super().setUp()
        crear_proyecto(self.raiz, "Demo").close()
        self.almacen = AlmacenArtefactosProyecto(self.raiz)

    def publicar(self, contenido, attempt_id, metadata=None):
        trabajo = self.raiz / "cache" / "jobs" / "j1" / attempt_id
        trabajo.mkdir(parents=True, exist_ok=True)
        (trabajo / "x.bin").write_bytes(contenido)
        manifiesto = ManifiestoSalida((ArchivoSalida("x.bin", len(contenido)),),
                                      metadata if metadata is not None else {"kind": "prueba"})
        return self.almacen.publicar("j1", str(trabajo), manifiesto, attempt_id=attempt_id,
                                     clave="k1", stage="prueba", version_contrato="1")

    def test_igual_tamano_y_distinto_contenido_no_se_adopta(self):
        """Cuatro bytes son cuatro bytes: sin digest, `BBBB` adoptaria `AAAA`."""
        self.assertEqual(self.publicar(b"AAAA", "primero"), ("artifacts/j1/x.bin",))
        with self.assertRaises(ErrorValidacionSalida) as capturado:
            self.publicar(b"BBBB", "segundo")
        self.assertIn("archivos", str(capturado.exception),
                      "el diagnostico debe senalar que difiere el contenido declarado")
        self.assertEqual((self.raiz / "artifacts" / "j1" / "x.bin").read_bytes(), b"AAAA",
                         "la publicacion previa se conserva intacta")
        self.assertEqual((self.raiz / "cache" / "jobs" / "j1" / "segundo" / "x.bin").read_bytes(),
                         b"BBBB", "el intento discordante tambien se conserva para diagnostico")

    def test_los_mismos_bytes_si_se_adoptan(self):
        self.publicar(b"AAAA", "primero")
        self.assertEqual(self.publicar(b"AAAA", "segundo"), ("artifacts/j1/x.bin",))
        self.assertEqual((self.raiz / "artifacts" / "j1" / "x.bin").read_bytes(), b"AAAA")

    def test_un_linaje_distinto_no_adopta_aunque_los_bytes_coincidan(self):
        self.publicar(b"AAAA", "primero")
        trabajo = self.raiz / "cache" / "jobs" / "j1" / "otra-etapa"
        trabajo.mkdir(parents=True)
        (trabajo / "x.bin").write_bytes(b"AAAA")
        manifiesto = ManifiestoSalida((ArchivoSalida("x.bin", 4),), {"kind": "prueba"})
        with self.assertRaises(ErrorValidacionSalida):
            self.almacen.publicar("j1", str(trabajo), manifiesto, attempt_id="otra",
                                  clave="k1", stage="otra_etapa", version_contrato="1")
        with self.assertRaises(ErrorValidacionSalida):
            self.almacen.publicar("j1", str(trabajo), manifiesto, attempt_id="otra",
                                  clave="k1", stage="prueba", version_contrato="9")

    def test_unos_bytes_alterados_tras_publicar_no_se_adoptan(self):
        """El sidecar intacto sobre archivos manipulados no basta."""
        self.publicar(b"AAAA", "primero")
        (self.raiz / "artifacts" / "j1" / "x.bin").write_bytes(b"XXXX")
        with self.assertRaises(ErrorValidacionSalida):
            self.publicar(b"AAAA", "segundo")


# --------------------------------------------------------------------------- #
# (4) Migracion versionada: canonicalizacion, renumeracion e indices
# --------------------------------------------------------------------------- #

class MigracionV4Tests(BaseDurabilidad):
    def semilla(self, version, jobs=(), intentos=()):
        """Fixture en la version indicada, con historia que migrar."""
        self.raiz.mkdir()
        for directorio in DIRECTORIOS_PROYECTO:
            (self.raiz / directorio).mkdir()
        (self.raiz / NOMBRE_MANIFIESTO).write_text(
            json.dumps({"project_id": "p", "name": "Antiguo", "schema_version": version}),
            encoding="utf-8")
        (self.raiz / NOMBRE_BASE).touch()
        conexion = _conexion(self.raiz / NOMBRE_BASE)
        try:
            _sql_v1(conexion)
            _sql_v2(conexion)
            if version >= 3:
                _sql_v3(conexion)
            conexion.execute(f"PRAGMA user_version={version}")
            conexion.execute("INSERT INTO Project VALUES ('p', 'Antiguo', '{}')")
            for identificador in jobs:
                conexion.execute("INSERT INTO Job (id, kind, state) VALUES (?, 'ingest', 'queued')",
                                 (identificador,))
            for attempt_id, job_id, instante in intentos:
                conexion.execute(
                    "INSERT INTO JobAttempt (id, job_id, state) VALUES (?, ?, 'interrupted')",
                    (attempt_id, job_id))
                if version >= 3 and instante is not None:
                    conexion.execute("UPDATE JobAttempt SET started_at=? WHERE id=?",
                                     (instante, attempt_id))
            conexion.commit()
        finally:
            conexion.close()

    def conexion_base(self):
        return _conexion(self.raiz / NOMBRE_BASE)

    # -- canonicalizacion --------------------------------------------------- #

    def test_el_backfill_usa_la_regla_del_dominio_y_no_lower_de_sqlite(self):
        """`lower()` de SQLite es ASCII: `Straße` sobreviviria junto a `STRASSE`."""
        self.semilla(2, jobs=("Straße",))
        with abrir_proyecto(self.raiz) as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().schema_version, VERSION_ESQUEMA)
            canonico = proyecto.repositorio.conexion.execute(
                "SELECT canonical_id FROM Job WHERE id='Straße'").fetchone()[0]
        self.assertEqual(canonico, clave_canonica("Straße"))
        self.assertEqual(canonico, "strasse")

    def test_una_colision_canonica_detiene_la_migracion_y_restaura(self):
        self.semilla(2, jobs=("Straße", "STRASSE"))
        with self.assertRaises(ErrorMigracionProyecto) as capturado:
            abrir_proyecto(self.raiz)
        self.assertIn("comparten identidad", str(capturado.exception.__cause__),
                      "la causa debe nombrar los dos trabajos que colisionan")

        conexion = self.conexion_base()
        try:
            self.assertEqual(conexion.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual(
                sorted(fila[0] for fila in conexion.execute("SELECT id FROM Job")),
                ["STRASSE", "Straße"], "no se pierde historia al restaurar")
            self.assertEqual({fila[1] for fila in conexion.execute("PRAGMA table_info(Job)")},
                             {"id", "kind", "state", "payload_json"})
        finally:
            conexion.close()
        self.assertEqual(json.loads((self.raiz / NOMBRE_MANIFIESTO).read_text("utf-8"))
                         ["schema_version"], 2)

    def test_una_colision_ascii_tambien_se_detiene(self):
        self.semilla(2, jobs=("Alpha", "ALPHA"))
        with self.assertRaises(ErrorMigracionProyecto):
            abrir_proyecto(self.raiz)

    # -- renumeracion ------------------------------------------------------- #

    def test_los_intentos_heredados_reciben_numeros_propios_y_deterministas(self):
        self.semilla(2, jobs=("j1",),
                     intentos=(("a1", "j1", None), ("a2", "j1", None), ("a3", "j1", None)))
        with abrir_proyecto(self.raiz) as proyecto:
            filas = proyecto.repositorio.conexion.execute(
                "SELECT id, attempt_number FROM JobAttempt ORDER BY attempt_number").fetchall()
        self.assertEqual(filas, [("a1", 1), ("a2", 2), ("a3", 3)])

    def test_la_renumeracion_respeta_el_orden_cronologico(self):
        self.semilla(3, jobs=("j1",),
                     intentos=(("tarde", "j1", 300.0), ("pronto", "j1", 100.0),
                               ("medio", "j1", 200.0)))
        with abrir_proyecto(self.raiz) as proyecto:
            filas = proyecto.repositorio.conexion.execute(
                "SELECT id FROM JobAttempt ORDER BY attempt_number").fetchall()
        self.assertEqual([fila[0] for fila in filas], ["pronto", "medio", "tarde"])

    def test_cada_job_numera_sus_intentos_por_separado(self):
        self.semilla(2, jobs=("j1", "j2"),
                     intentos=(("a1", "j1", None), ("b1", "j2", None), ("a2", "j1", None)))
        with abrir_proyecto(self.raiz) as proyecto:
            filas = dict(proyecto.repositorio.conexion.execute(
                "SELECT id, attempt_number FROM JobAttempt").fetchall())
        self.assertEqual(filas, {"a1": 1, "a2": 2, "b1": 1})

    # -- indices ------------------------------------------------------------ #

    def test_el_esquema_exige_ambos_indices_unicos(self):
        with crear_proyecto(self.raiz, "Demo"):
            pass
        for indice in ("Job_identidad_canonica", "JobAttempt_secuencia"):
            with self.subTest(indice=indice):
                conexion = self.conexion_base()
                try:
                    conexion.execute(f"DROP INDEX {indice}")
                    conexion.commit()
                finally:
                    conexion.close()
                with self.assertRaises(ErrorProyecto) as capturado:
                    abrir_proyecto(self.raiz)
                self.assertIn("indices", str(capturado.exception))
                conexion = self.conexion_base()
                try:
                    if indice == "Job_identidad_canonica":
                        conexion.execute("CREATE UNIQUE INDEX Job_identidad_canonica"
                                         " ON Job (canonical_id)")
                    else:
                        conexion.execute("CREATE UNIQUE INDEX JobAttempt_secuencia"
                                         " ON JobAttempt (job_id, attempt_number)")
                    conexion.commit()
                finally:
                    conexion.close()

    def test_la_secuencia_de_intentos_es_unica_por_job(self):
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            repositorio = RepositorioSqliteJobs(proyecto.repositorio)
            repositorio.encolar(Job(id="j1", stage="prueba", version_contrato="1"), evento=historia())
            conexion = proyecto.repositorio.conexion
            conexion.execute("INSERT INTO JobAttempt (id, job_id, state, attempt_number)"
                             " VALUES ('a1', 'j1', 'failed', 1)")
            conexion.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                conexion.execute("INSERT INTO JobAttempt (id, job_id, state, attempt_number)"
                                 " VALUES ('a2', 'j1', 'failed', 1)")

    # -- rutas de version --------------------------------------------------- #

    def test_una_imagen_v3_intermedia_se_actualiza_sin_perder_historia(self):
        """v3 dejo de ser inmutable, asi que necesita su propia ruta de salida."""
        self.semilla(3, jobs=("j1",), intentos=(("a1", "j1", 10.0), ("a2", "j1", 20.0)))
        with abrir_proyecto(self.raiz) as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().schema_version, VERSION_ESQUEMA)
            conexion = proyecto.repositorio.conexion
            self.assertEqual(conexion.execute("SELECT COUNT(*) FROM Job").fetchone()[0], 1)
            self.assertEqual(
                conexion.execute("SELECT id FROM JobAttempt ORDER BY attempt_number").fetchall(),
                [("a1",), ("a2",)])
            self.assertEqual(conexion.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_un_crash_a_mitad_de_la_migracion_restaura_la_version_anterior(self):
        self.semilla(3, jobs=("j1",))
        with self.assertRaises(ErrorMigracionProyecto):
            abrir_proyecto(self.raiz,
                           fallo_migracion=lambda _v: (_ for _ in ()).throw(RuntimeError("corte")))
        conexion = self.conexion_base()
        try:
            self.assertEqual(conexion.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(conexion.execute("SELECT id FROM Job").fetchall(), [("j1",)])
        finally:
            conexion.close()
        with abrir_proyecto(self.raiz) as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().schema_version, VERSION_ESQUEMA)

    def test_un_proyecto_migrado_queda_operativo(self):
        self.semilla(2)
        with abrir_proyecto(self.raiz) as proyecto:
            coordinador = crear_coordinador(proyecto, catalogo={"prueba": STAGE})
            try:
                coordinador.encolar(Job(id="nuevo", stage="prueba", version_contrato="1",
                                        payload={"pasos": 1}))
                self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE))
                self.assertEqual(coordinador.job("nuevo").estado, EstadoJob.SUCCEEDED)
            finally:
                coordinador.cerrar()


# --------------------------------------------------------------------------- #
# (5) Ningun terminal ni intencion sin su evento
# --------------------------------------------------------------------------- #

class AtomicidadTerminalTests(BaseDurabilidad):
    def setUp(self):
        super().setUp()
        crear_proyecto(self.raiz, "Demo").close()

    def sesion(self):
        proyecto = abrir_proyecto(self.raiz)
        self.addCleanup(proyecto.close)
        self.abierto = proyecto
        coordinador = crear_coordinador(proyecto, catalogo={"prueba": STAGE})
        self.addCleanup(coordinador.cerrar)
        return coordinador

    def bloquear_eventos(self, tipo):
        conexion = self.abierto.repositorio.conexion
        conexion.execute(f"CREATE TRIGGER sin_{tipo} BEFORE INSERT ON JobEvent"
                         f" WHEN NEW.kind='{tipo}' BEGIN SELECT RAISE(ABORT, 'fault'); END")
        conexion.commit()
        self.addCleanup(self.desbloquear, tipo)

    def desbloquear(self, tipo):
        try:
            conexion = self.abierto.repositorio.conexion
            conexion.execute(f"DROP TRIGGER IF EXISTS sin_{tipo}")
            conexion.commit()
        except Exception:
            pass

    def test_un_fallo_de_lanzamiento_no_confirma_estado_sin_historia(self):
        """Era la ultima ventana abierta entre un terminal y su evento."""
        coordinador = self.sesion()
        coordinador.encolar(Job(id="j1", stage="prueba", version_contrato="1",
                                payload={"pasos": 1}))
        self.bloquear_eventos("error")
        with mock.patch.object(type(coordinador._ejecutor), "lanzar",
                               side_effect=OSError("no hay proceso")):
            with self.assertRaises(ErrorJob):
                coordinador.paso()
        job = coordinador.job("j1")
        self.assertEqual(job.estado, EstadoJob.RUNNING,
                         "sin evento terminal no puede confirmarse el terminal")
        # `intento.preparado` si esta: describe lo unico que quedo confirmado
        # —el lease y el intento—, que es precisamente lo que explica el
        # `running`. Lo que no puede aparecer es un terminal sin su historia.
        claves = [evento.clave for evento in coordinador.eventos("j1")]
        self.assertEqual(claves, ["job.encolado", "intento.preparado"])
        self.assertNotIn("job.fallido", claves)

    def test_la_intencion_de_cancelar_tampoco_se_confirma_sin_su_evento(self):
        coordinador = self.sesion()
        coordinador.encolar(Job(id="j1", stage="prueba", version_contrato="1",
                                payload={"pasos": 1}))
        self.bloquear_eventos("cancelacion")
        with self.assertRaises(ErrorJob):
            coordinador.solicitar_cancelacion("j1")
        self.assertFalse(coordinador.job("j1").cancelacion_solicitada,
                         "la intencion sin historia no queda registrada")

    def test_la_recuperacion_se_retoma_si_muere_entre_sus_dos_mitades(self):
        """Marcar interrumpido y decidir el desenlace son dos transacciones."""
        coordinador = self.sesion()
        repositorio = RepositorioSqliteJobs(self.abierto.repositorio)
        repositorio.encolar(Job(id="j1", stage="prueba", version_contrato="1", max_intentos=3), evento=historia())
        repositorio.adquirir_lease("j1", "sesion-vieja", 60.0, 1000.0, evento=historia())
        # Primera mitad tal y como la ejecuta produccion: el cambio a
        # `interrupted` y su evento, juntos. La muerte se simula no llegando a
        # la segunda mitad.
        repositorio.recuperar_huerfanos(
            "sesion-nueva", 2000.0,
            evento=lambda job_id, attempt_id: EventoProgreso(
                job_id=job_id, tipo=TipoEvento.INTERRUPCION, instante=2000.0,
                attempt_id=attempt_id, clave="intento.interrumpido"))
        self.assertEqual(coordinador.job("j1").estado, EstadoJob.INTERRUPTED)

        # Una apertura posterior encuentra el `interrupted` a medio procesar.
        self.assertEqual(coordinador.recuperar(), ("j1",))
        job = coordinador.job("j1")
        self.assertEqual(job.estado, EstadoJob.QUEUED)
        claves = [evento.clave for evento in coordinador.eventos("j1")]
        self.assertIn("intento.interrumpido", claves)
        self.assertIn("job.reprogramado", claves)


# --------------------------------------------------------------------------- #
# (6) Corrupcion numerica tambien en los comandos
# --------------------------------------------------------------------------- #

class CorrupcionEnComandosTests(BaseDurabilidad):
    def setUp(self):
        super().setUp()
        self.proyecto = crear_proyecto(self.raiz, "Demo")
        self.addCleanup(self.proyecto.close)
        self.repositorio = RepositorioSqliteJobs(self.proyecto.repositorio)
        self.repositorio.encolar(Job(id="j1", stage="prueba", version_contrato="1",
                                     max_intentos=3), evento=historia())

    def corromper(self, sql):
        conexion = self.proyecto.repositorio.conexion
        conexion.execute(sql)
        conexion.commit()

    def test_max_attempts_corrupto_no_filtra_valueerror_al_adquirir(self):
        self.corromper("UPDATE Job SET max_attempts='bad' WHERE id='j1'")
        with self.assertRaises(ErrorJob) as capturado:
            self.repositorio.adquirir_lease("j1", "duenio", 60.0, 1000.0, evento=historia())
        self.assertNotIsInstance(capturado.exception, ValueError)

    def test_attempts_used_corrupto_tampoco(self):
        self.corromper("UPDATE Job SET attempts_used='bad' WHERE id='j1'")
        with self.assertRaises(ErrorJob):
            self.repositorio.adquirir_lease("j1", "duenio", 60.0, 1000.0, evento=historia())

    def test_attempt_number_corrupto_no_filtra_al_numerar_el_siguiente(self):
        self.repositorio.adquirir_lease("j1", "duenio", 60.0, 1000.0, evento=historia())
        self.corromper("UPDATE JobAttempt SET attempt_number='bad'")
        self.corromper("UPDATE Job SET state='queued'")
        with self.assertRaises(ErrorJob) as capturado:
            self.repositorio.adquirir_lease("j1", "duenio", 60.0, 1001.0, evento=historia())
        self.assertNotIsInstance(capturado.exception, ValueError)

    def test_interruptions_used_corrupto_no_filtra_al_reprogramar(self):
        intento = self.repositorio.adquirir_lease("j1", "duenio", 60.0, 1000.0, evento=historia())
        self.repositorio.finalizar(intento.id, "duenio", EstadoJob.FAILED, 1001.0,
                                   error_codigo="x", eventos=historias())
        self.corromper("UPDATE Job SET interruptions_used='bad' WHERE id='j1'")
        with self.assertRaises(ErrorJob) as capturado:
            self.repositorio.reprogramar("j1", 1002.0, eventos=historias())
        self.assertNotIsInstance(capturado.exception, ValueError)

    def test_las_filas_sanas_siguen_funcionando(self):
        intento = self.repositorio.adquirir_lease("j1", "duenio", 60.0, 1000.0, evento=historia())
        self.assertEqual(intento.numero, 1)
        self.repositorio.finalizar(intento.id, "duenio", EstadoJob.FAILED, 1001.0,
                                   error_codigo="x", eventos=historias())
        self.assertTrue(self.repositorio.reprogramar("j1", 1002.0, eventos=historias()))
        self.assertEqual(self.repositorio.adquirir_lease("j1", "duenio", 60.0, 1003.0, evento=historia()).numero, 2)


# --------------------------------------------------------------------------- #
# (7) Gramatica portable de nombres de dispositivo
# --------------------------------------------------------------------------- #

class GramaticaPortableTests(unittest.TestCase):
    #: Cada uno es un nombre que Windows resuelve como dispositivo aunque no lo
    #: parezca. La lista se comprueba igual en Windows y en POSIX: la gramatica
    #: no puede depender de donde corra la prueba.
    RESERVADOS_DISFRAZADOS = (
        "COM¹", "COM².txt", "LPT³.log", "CON .txt", "NUL .txt", "AUX .bin",
        "PRN  .dat", "lpt².LOG", "CON.txt.bak", "NUL",
    )

    def test_ningun_nombre_reservado_disfrazado_atraviesa_la_frontera(self):
        for nombre in self.RESERVADOS_DISFRAZADOS:
            with self.subTest(nombre=nombre):
                self.assertFalse(es_componente_interno(nombre))
                with self.assertRaises(Exception):
                    exigir_componente_interno(nombre)

    def test_un_job_no_puede_llamarse_como_un_dispositivo_disfrazado(self):
        for nombre in ("COM¹", "CON .txt"):
            with self.subTest(nombre=nombre):
                with self.assertRaises(Exception):
                    Job(id=nombre, stage="prueba", version_contrato="1")

    def test_los_nombres_que_solo_se_parecen_siguen_pasando(self):
        for nombre in ("COM10", "CONtenido", "NULO", "AUXILIAR", "com", "lpt", "PRNta.txt",
                       "COM¹¹", "con¹", "COM0"):
            with self.subTest(nombre=nombre):
                self.assertTrue(es_componente_interno(nombre), nombre)


# --------------------------------------------------------------------------- #
# T03-F02 (1) La primera transicion tambien lleva su evento dentro
# --------------------------------------------------------------------------- #

class PrimeraTransicionAtomicaTests(AtomicidadTerminalTests):
    """Adquirir el lease es la transicion que mas caro sale dejar a medias.

    Confirmar `running` por su cuenta y escribir el evento despues deja un job en
    marcha —con su worker ya lanzado— del que la historia no dice nada, y esa
    contradiccion no se cura sola: el worker termina y publica, pero su arranque
    no aparece en ninguna consulta.
    """

    def test_si_falla_el_evento_inicial_no_hay_lease_ni_intento_ni_worker(self):
        coordinador = self.sesion()
        coordinador.encolar(Job(id="j1", stage="prueba", version_contrato="1",
                                payload={"pasos": 2, "retardo": 0.05}))
        self.bloquear_eventos("inicio")
        with self.assertRaises(ErrorJob):
            coordinador.paso()
        job = coordinador.job("j1")
        self.assertEqual(job.estado, EstadoJob.QUEUED, "el `running` no puede confirmarse solo")
        self.assertEqual(job.intentos_usados, 0, "el presupuesto no se gasta en algo que no paso")
        self.assertEqual(coordinador.intentos("j1"), ())
        self.assertEqual(coordinador.resumen().en_curso, (),
                         "no se lanza worker si la transaccion no se confirmo")
        self.assertEqual([evento.clave for evento in coordinador.eventos("j1")], ["job.encolado"])

    def test_retirado_el_fallo_el_arranque_deja_estado_e_historia_coherentes(self):
        coordinador = self.sesion()
        coordinador.encolar(Job(id="j1", stage="prueba", version_contrato="1",
                                payload={"pasos": 1}))
        self.bloquear_eventos("inicio")
        with self.assertRaises(ErrorJob):
            coordinador.paso()
        self.desbloquear("inicio")
        self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE))
        self.assertEqual(coordinador.job("j1").estado, EstadoJob.SUCCEEDED)
        claves = [evento.clave for evento in coordinador.eventos("j1")]
        self.assertEqual(claves[:3], ["job.encolado", "intento.preparado", "intento.iniciado"])

    def test_el_evento_de_preparacion_no_afirma_que_el_proceso_ya_arranco(self):
        """Son dos hechos distintos y se registran por separado."""
        coordinador = self.sesion()
        coordinador.encolar(Job(id="j1", stage="prueba", version_contrato="1",
                                payload={"pasos": 1}))
        coordinador.paso()
        claves = [evento.clave for evento in coordinador.eventos("j1")]
        self.assertIn("intento.preparado", claves)
        self.assertNotIn("intento.iniciado", claves,
                         "el worker aun no se ha presentado: afirmarlo seria falso")

        preparado = next(evento for evento in coordinador.eventos("j1")
                         if evento.clave == "intento.preparado")
        self.assertEqual(preparado.attempt_id, coordinador.intentos("j1")[0].id,
                         "el evento debe quedar ligado al intento que la transaccion creo")

        self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE))
        iniciado = next(evento for evento in coordinador.eventos("j1")
                        if evento.clave == "intento.iniciado")
        self.assertGreater(iniciado.secuencia, preparado.secuencia)
        self.assertEqual(iniciado.datos.get("pid"), coordinador.intentos("j1")[0].pid)

    def test_si_falla_el_evento_de_interrupcion_el_intento_sigue_activo(self):
        coordinador = self.sesion()
        repositorio = RepositorioSqliteJobs(self.abierto.repositorio)
        repositorio.encolar(Job(id="j1", stage="prueba", version_contrato="1", max_intentos=3), evento=historia())
        repositorio.adquirir_lease("j1", "sesion-vieja", 60.0, 1000.0, evento=historia())
        self.bloquear_eventos("interrupcion")
        with self.assertRaises(ErrorJob):
            coordinador.recuperar()
        job = coordinador.job("j1")
        self.assertEqual(job.estado, EstadoJob.RUNNING,
                         "no puede quedar `interrupted` sin historia que lo explique")
        self.assertEqual(coordinador.intentos("j1")[0].estado, EstadoJob.RUNNING)
        self.assertEqual(coordinador.intentos("j1")[0].lease_owner, "sesion-vieja",
                         "un rollback parcial habria soltado el lease")
        self.assertEqual(job.intentos_usados, 1, "la devolucion de credito tambien se revierte")
        self.assertNotIn("intento.interrumpido",
                         [evento.clave for evento in coordinador.eventos("j1")],
                         "la interrupcion se deshizo entera, historia incluida")

    def test_retirado_el_fallo_la_recuperacion_escribe_estado_e_historia(self):
        coordinador = self.sesion()
        repositorio = RepositorioSqliteJobs(self.abierto.repositorio)
        repositorio.encolar(Job(id="j1", stage="prueba", version_contrato="1", max_intentos=3), evento=historia())
        repositorio.adquirir_lease("j1", "sesion-vieja", 60.0, 1000.0, evento=historia())
        self.bloquear_eventos("interrupcion")
        with self.assertRaises(ErrorJob):
            coordinador.recuperar()
        self.desbloquear("interrupcion")
        self.assertEqual(coordinador.recuperar(), ("j1",))
        self.assertEqual(coordinador.job("j1").estado, EstadoJob.QUEUED)
        claves = [evento.clave for evento in coordinador.eventos("j1")]
        self.assertEqual(claves[-2:], ["intento.interrumpido", "job.reprogramado"],
                         "la recuperacion cierra con su historia completa")

    def test_cancel_requested_no_se_confirma_sin_su_evento(self):
        """`marcar_estado` es la otra puerta por la que se cambia de estado."""
        coordinador = self.sesion()
        coordinador.encolar(Job(id="j1", stage="prueba", version_contrato="1",
                                payload={"pasos": 400, "retardo": 0.05}))
        limite = time.monotonic() + LIMITE
        while coordinador.job("j1").estado is not EstadoJob.RUNNING:
            coordinador.paso()
            self.assertLess(time.monotonic(), limite, "el worker no llego a correr")
            time.sleep(0.01)
        self.assertTrue(coordinador.solicitar_cancelacion("j1"))

        # Se bloquea *despues* de registrar la intencion: lo que se prueba es la
        # transicion del intento a `cancel_requested`, no la intencion del job.
        self.bloquear_eventos("cancelacion")
        with self.assertRaises(ErrorJob):
            coordinador.paso()
        self.assertEqual(coordinador.intentos("j1")[0].estado, EstadoJob.RUNNING,
                         "sin evento no puede confirmarse `cancel_requested`")
        self.assertNotIn("cancelacion.cooperativa",
                         [evento.clave for evento in coordinador.eventos("j1")])

        self.desbloquear("cancelacion")
        self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE))
        job = coordinador.job("j1")
        self.assertEqual((job.estado, job.error_codigo), (EstadoJob.FAILED, CODIGO_CANCELADO))
        self.assertIn("cancelacion.cooperativa",
                      [evento.clave for evento in coordinador.eventos("j1")])

    def test_validating_no_se_confirma_sin_su_evento(self):
        coordinador = self.sesion()
        coordinador.encolar(Job(id="j1", stage="prueba", version_contrato="1",
                                payload={"pasos": 1}))
        self.bloquear_eventos("validacion")
        with self.assertRaises(ErrorJob):
            coordinador.ejecutar(limite_segundos=LIMITE)
        self.assertEqual(coordinador.job("j1").estado, EstadoJob.RUNNING,
                         "`validating` tampoco puede quedar sin historia")
        self.assertFalse((self.raiz / "artifacts" / "j1").exists(),
                         "nada se publica sobre una transicion que no se confirmo")

    def test_ninguna_transicion_de_estado_queda_sin_evento(self):
        """Barrido sobre las transiciones que el coordinador escribe.

        Cada una debe morir con su evento bloqueado y dejar el estado anterior
        exactamente como estaba.
        """
        coordinador = self.sesion()
        for indice, tipo in enumerate(("inicio", "cancelacion")):
            with self.subTest(evento=tipo):
                identificador = f"j{indice}"
                coordinador.encolar(Job(id=identificador, stage="prueba", version_contrato="1",
                                        payload={"pasos": 1}))
                antes = coordinador.job(identificador)
                self.bloquear_eventos(tipo)
                with self.assertRaises(ErrorJob):
                    if tipo == "cancelacion":
                        coordinador.solicitar_cancelacion(identificador)
                    else:
                        coordinador.paso()
                despues = coordinador.job(identificador)
                self.assertEqual((despues.estado, despues.cancelacion_solicitada,
                                  despues.intentos_usados),
                                 (antes.estado, antes.cancelacion_solicitada,
                                  antes.intentos_usados))
                self.desbloquear(tipo)


# --------------------------------------------------------------------------- #
# T03-F02 (2) La migracion no acepta un ID que el dominio no sabe representar
# --------------------------------------------------------------------------- #

class IdsHeredadosTests(BaseDurabilidad):
    def fixture_v2(self, identificador):
        """Proyecto en la version aceptada por T02 con un unico Job heredado."""
        raiz = self.base / f"legado-{uuid.uuid4().hex}.clipsapp"
        raiz.mkdir()
        for directorio in DIRECTORIOS_PROYECTO:
            (raiz / directorio).mkdir()
        (raiz / NOMBRE_MANIFIESTO).write_text(
            json.dumps({"project_id": "p2", "name": "Antiguo", "schema_version": 2}),
            encoding="utf-8")
        (raiz / NOMBRE_BASE).touch()
        conexion = _conexion(raiz / NOMBRE_BASE)
        try:
            _sql_v1(conexion)
            _sql_v2(conexion)
            conexion.execute("PRAGMA user_version=2")
            conexion.execute("INSERT INTO Project VALUES ('p2', 'Antiguo', '{}')")
            conexion.execute("INSERT INTO Job VALUES (?, 'ingest', 'queued', '{}')",
                             (identificador,))
            conexion.execute("INSERT INTO JobAttempt VALUES ('i1', ?, 'queued', NULL)",
                             (identificador,))
            conexion.commit()
        finally:
            conexion.close()
        return raiz

    def assertPreservadoEnV2(self, raiz, identificador):
        """La imagen anterior debe quedar byte a byte utilizable, no solo presente."""
        self.assertEqual(json.loads((raiz / NOMBRE_MANIFIESTO).read_text("utf-8"))
                         ["schema_version"], 2)
        conexion = _conexion(raiz / NOMBRE_BASE)
        try:
            self.assertEqual(conexion.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual({fila[1] for fila in conexion.execute("PRAGMA table_info(Job)")},
                             {"id", "kind", "state", "payload_json"},
                             "una v4 a medias habria dejado columnas nuevas")
            self.assertEqual(conexion.execute("SELECT id FROM Job").fetchall(),
                             [(identificador,)])
            self.assertEqual(conexion.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conexion.close()
        residuos = sorted(hijo.name for hijo in raiz.iterdir()
                          if ".bak" in hijo.name or "pending" in hijo.name)
        self.assertEqual(residuos, [], "la restauracion no puede dejar respaldos ni marcador")

    """Canonicalizar un id irrepresentable produce una v4 que abre y no se lee.

    Una sola fila heredada —un NFD, un `..`, un `CON`— bastaba para bloquear
    `listar()`, y con el la recuperacion y la planificacion enteras. Y ya sin
    vuelta atras, porque la migracion se habia confirmado.
    """

    IDS_INVALIDOS = (
        ("nfd", "cafe\u0301"),
        ("traversal", "../escape"),
        ("separador_posix", "a/b"),
        ("separador_windows", "a\\b"),
        ("reservado", "CON"),
        ("reservado_con_espacio", "CON .txt"),
        ("unidad", "C:x"),
        ("punto_final", "job."),
    )

    def test_un_id_heredado_irrepresentable_detiene_la_migracion(self):
        for etiqueta, identificador in self.IDS_INVALIDOS:
            with self.subTest(caso=etiqueta):
                raiz = self.fixture_v2(identificador)
                with self.assertRaises(ErrorMigracionProyecto) as capturado:
                    abrir_proyecto(raiz)
                self.assertIn(repr(identificador), str(capturado.exception),
                              "el diagnostico debe nombrar el trabajo que hay que resolver")
                self.assertPreservadoEnV2(raiz, identificador)

    def test_una_migracion_detenida_se_puede_reintentar_indefinidamente(self):
        raiz = self.fixture_v2("../escape")
        for _ in range(3):
            with self.assertRaises(ErrorMigracionProyecto):
                abrir_proyecto(raiz)
            self.assertPreservadoEnV2(raiz, "../escape")

    def test_los_ids_validos_siguen_migrando_y_leyendose(self):
        raiz = self.fixture_v2("trabajo-valido")
        with abrir_proyecto(raiz) as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().schema_version, VERSION_ESQUEMA)
            jobs = RepositorioSqliteJobs(proyecto.repositorio).listar()
            self.assertEqual([job.id for job in jobs], ["trabajo-valido"])

    def test_un_id_valido_no_ascii_tambien_migra(self):
        raiz = self.fixture_v2("cancion-ñ")
        with abrir_proyecto(raiz) as proyecto:
            jobs = RepositorioSqliteJobs(proyecto.repositorio).listar()
            self.assertEqual([job.id for job in jobs], ["cancion-ñ"])


# --------------------------------------------------------------------------- #
# T03-F04 La historia describe la entidad que el comando muto
# --------------------------------------------------------------------------- #

class IdentidadDeLaHistoriaTests(BaseDurabilidad):
    """El llamador dice *que* paso; el comando decide *de que* lo cuenta.

    Un evento colgado de otro job produce dos mentiras a la vez: el job que si
    cambio no puede explicar su estado, y uno intacto afirma algo que no le
    ocurrio. Por eso la identidad no se respeta, se impone.
    """

    def setUp(self):
        super().setUp()
        crear_proyecto(self.raiz, "Demo").close()
        self.proyecto = abrir_proyecto(self.raiz)
        self.addCleanup(self.proyecto.close)
        self.repositorio = RepositorioSqliteJobs(self.proyecto.repositorio)

    # -- utilidades ----------------------------------------------------- #

    def mentira(self, clave, job_id="job-ajeno", attempt_id="attempt-ajeno"):
        """Evento que miente sobre ambos extremos de su identidad."""
        return EventoProgreso(job_id=job_id, tipo=TipoEvento.FIN, instante=1.0,
                              clave=clave, attempt_id=attempt_id)

    def alta(self, job_id, **claves):
        job = Job(id=job_id, stage="prueba", version_contrato="1", **claves)
        return self.repositorio.encolar(job, self.mentira(f"alta-{job_id}"))

    def historia(self, job_id):
        return [(evento.job_id, evento.attempt_id, evento.clave)
                for evento in self.repositorio.eventos(job_id)]

    def ultimo(self, job_id):
        return self.historia(job_id)[-1]

    # -- comandos de nivel job: attempt_id siempre NULL ------------------ #

    def test_encolar_descarta_el_intento_que_el_llamador_invento(self):
        self.alta("j1")
        self.assertEqual(self.historia("j1"), [("j1", None, "alta-j1")])

    def test_cancelar_reprogramar_y_fallo_directo_no_cuelgan_de_ningun_intento(self):
        self.alta("j1", max_intentos=2)
        self.repositorio.solicitar_cancelacion("j1", self.mentira("cancelar"))
        self.assertEqual(self.ultimo("j1"), ("j1", None, "cancelar"))

        self.repositorio.marcar_fallo_directo("j1", 1.0, "x", eventos=(self.mentira("fallo"),))
        self.assertEqual(self.ultimo("j1"), ("j1", None, "fallo"))

        self.repositorio.reprogramar("j1", 2.0, eventos=(self.mentira("reprograma"),))
        self.assertEqual(self.ultimo("j1"), ("j1", None, "reprograma"))

    # -- comandos de nivel intento: el intento real ---------------------- #

    def test_los_comandos_de_intento_ligan_al_intento_que_tocaron(self):
        self.alta("j1", max_intentos=2)
        intento = self.repositorio.adquirir_lease("j1", DUENIO, 60.0, 1000.0,
                                                  evento=self.mentira("preparado"))
        self.assertEqual(self.ultimo("j1"), ("j1", intento.id, "preparado"))

        self.repositorio.registrar_checkpoint("j1" and intento.id, DUENIO, {"paso": 1}, 1001.0,
                                              eventos=(self.mentira("checkpoint"),))
        self.assertEqual(self.ultimo("j1"), ("j1", intento.id, "checkpoint"))

        self.repositorio.marcar_estado(intento.id, DUENIO, EstadoJob.VALIDATING, 1002.0,
                                       eventos=(self.mentira("validando"),))
        self.assertEqual(self.ultimo("j1"), ("j1", intento.id, "validando"))

        self.repositorio.finalizar(intento.id, DUENIO, EstadoJob.SUCCEEDED, 1003.0,
                                   eventos=(self.mentira("fin"),))
        self.assertEqual(self.ultimo("j1"), ("j1", intento.id, "fin"))

    def test_la_recuperacion_liga_a_la_pareja_que_de_verdad_interrumpio(self):
        """El caso del hallazgo: la fabrica miente sobre job *y* sobre intento."""
        self.alta("huerfano", max_intentos=3)
        self.alta("victima")
        intento = self.repositorio.adquirir_lease("huerfano", "sesion-vieja", 60.0, 1000.0,
                                                  evento=self.mentira("preparado"))
        afectados = self.repositorio.recuperar_huerfanos(
            "sesion-nueva", 2000.0,
            evento=lambda job_id, attempt_id: self.mentira("interrupcion", job_id="victima",
                                                           attempt_id="attempt-falso"))
        self.assertEqual(afectados, ("huerfano",))
        self.assertEqual(self.repositorio.obtener("huerfano").estado, EstadoJob.INTERRUPTED)
        self.assertEqual(self.ultimo("huerfano"), ("huerfano", intento.id, "interrupcion"),
                         "el job que cambio de estado debe poder explicarlo")
        self.assertEqual(self.historia("victima"), [("victima", None, "alta-victima")],
                         "un job intacto no puede heredar la historia de otro")

    def test_la_matriz_completa_de_los_ocho_comandos_ignora_la_identidad_ajena(self):
        """Barrido: ningun comando conserva nada de lo que el llamador invento."""
        self.alta("j1", max_intentos=3)
        intento = self.repositorio.adquirir_lease("j1", DUENIO, 60.0, 1000.0,
                                                  evento=self.mentira("c2-lease"))
        pasos = (
            ("c3-checkpoint", intento.id, lambda: self.repositorio.registrar_checkpoint(
                intento.id, DUENIO, {"p": 1}, 1001.0, eventos=(self.mentira("c3-checkpoint"),))),
            ("c4-estado", intento.id, lambda: self.repositorio.marcar_estado(
                intento.id, DUENIO, EstadoJob.CANCEL_REQUESTED, 1002.0,
                eventos=(self.mentira("c4-estado"),))),
            ("c5-finalizar", intento.id, lambda: self.repositorio.finalizar(
                intento.id, DUENIO, EstadoJob.FAILED, 1003.0,
                eventos=(self.mentira("c5-finalizar"),))),
            ("c6-reprogramar", None, lambda: self.repositorio.reprogramar(
                "j1", 1004.0, eventos=(self.mentira("c6-reprogramar"),))),
            ("c7-cancelar", None, lambda: self.repositorio.solicitar_cancelacion(
                "j1", self.mentira("c7-cancelar"))),
            ("c8-fallo", None, lambda: self.repositorio.marcar_fallo_directo(
                "j1", 1005.0, "x", eventos=(self.mentira("c8-fallo"),))),
        )
        esperado = [("j1", None, "alta-j1"), ("j1", intento.id, "c2-lease")]
        for clave, attempt_esperado, ejecutar in pasos:
            with self.subTest(comando=clave):
                ejecutar()
                esperado.append(("j1", attempt_esperado, clave))
                self.assertEqual(self.ultimo("j1"), esperado[-1])
        self.assertEqual(self.historia("j1"), esperado)
        self.assertEqual(
            self.proyecto.repositorio.conexion.execute("PRAGMA foreign_key_check").fetchall(), [])

    # -- la base tambien lo impide ---------------------------------------- #

    def test_la_base_rechaza_una_pareja_incoherente_escrita_a_mano(self):
        self.alta("j1", max_intentos=2)
        self.alta("j2")
        intento = self.repositorio.adquirir_lease("j1", DUENIO, 60.0, 1000.0,
                                                  evento=self.mentira("preparado"))
        conexion = self.proyecto.repositorio.conexion
        casos = (
            ("intento inexistente", "j1", "no-existe"),
            ("intento de otro job", "j2", intento.id),
        )
        for motivo, job_id, attempt_id in casos:
            with self.subTest(motivo=motivo):
                with self.assertRaises(sqlite3.IntegrityError):
                    conexion.execute(
                        "INSERT INTO JobEvent (job_id, attempt_id, kind, at) VALUES (?, ?, ?, ?)",
                        (job_id, attempt_id, "error", 1.0))
                conexion.rollback()
        # La pareja correcta si entra, y el evento de nivel job tambien.
        conexion.execute("INSERT INTO JobEvent (job_id, attempt_id, kind, at) VALUES (?, ?, ?, ?)",
                         ("j1", intento.id, "error", 1.0))
        conexion.execute("INSERT INTO JobEvent (job_id, attempt_id, kind, at) VALUES (?, ?, ?, ?)",
                         ("j2", None, "error", 1.0))
        conexion.commit()
        self.assertEqual(conexion.execute("PRAGMA foreign_key_check").fetchall(), [])


# --------------------------------------------------------------------------- #
# T03-F04 Migracion v4 -> v5
# --------------------------------------------------------------------------- #

class MigracionV5Tests(BaseDurabilidad):
    def fixture_v4(self, eventos):
        """Proyecto detenido en v4 con la historia que la prueba decida.

        La imagen se construye desde el DDL de cada version —como hace
        `MigracionV4Tests.semilla`— y no degradando un proyecto actual. Degradar
        obligaria a deshacer a mano todo lo que aporta cada version futura, y
        una omision produciria un "v4" que en realidad ya trae columnas nuevas:
        la migracion se probaria contra una imagen que ningun usuario tiene.
        """
        raiz = self.base / f"v4-{uuid.uuid4().hex}.clipsapp"
        raiz.mkdir()
        for directorio in DIRECTORIOS_PROYECTO:
            (raiz / directorio).mkdir()
        (raiz / NOMBRE_BASE).touch()
        conexion = _conexion(raiz / NOMBRE_BASE)
        try:
            _sql_v1(conexion)
            _sql_v2(conexion)
            _sql_v3(conexion)
            _sql_v4(conexion)
            conexion.execute("INSERT INTO Project VALUES ('p', 'Antiguo', '{}')")
            conexion.execute("INSERT INTO Job (id, kind, state, stage, contract_version,"
                             " canonical_id, resources_json, max_attempts)"
                             " VALUES ('j1', 'prueba', 'queued', 'prueba', '1', 'j1',"
                             " '{\"cpu\": 1, \"disk\": 0, \"gpu\": 0, \"network\": 0}', 1)")
            conexion.execute("INSERT INTO JobAttempt (id, job_id, state, attempt_number)"
                             " VALUES ('a1', 'j1', 'running', 1)")
            for seq, job_id, attempt_id, clave in eventos:
                conexion.execute(
                    "INSERT INTO JobEvent (seq, job_id, attempt_id, kind, at, message_key)"
                    " VALUES (?, ?, ?, 'fin', 1.0, ?)", (seq, job_id, attempt_id, clave))
            conexion.execute("PRAGMA user_version=4")
            conexion.commit()
        finally:
            conexion.close()
        (raiz / NOMBRE_MANIFIESTO).write_text(
            json.dumps({"project_id": "p", "name": "Antiguo", "schema_version": 4}),
            encoding="utf-8")
        return raiz

    def test_una_historia_coherente_migra_preservando_seq_y_orden(self):
        raiz = self.fixture_v4([(5, "j1", None, "job-level"),
                                (9, "j1", "a1", "intento-level"),
                                (11, "j1", None, "otro-job-level")])
        with abrir_proyecto(raiz) as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().schema_version, VERSION_ESQUEMA)
            eventos = RepositorioSqliteJobs(proyecto.repositorio).eventos("j1")
            self.assertEqual([(evento.secuencia, evento.attempt_id, evento.clave)
                              for evento in eventos],
                             [(5, None, "job-level"), (9, "a1", "intento-level"),
                              (11, None, "otro-job-level")],
                             "reconstruir la tabla no puede renumerar la historia")
            conexion = proyecto.repositorio.conexion
            self.assertEqual(conexion.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conexion.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_un_seq_nuevo_continua_la_secuencia_heredada(self):
        raiz = self.fixture_v4([(41, "j1", None, "heredado")])
        with abrir_proyecto(raiz) as proyecto:
            repositorio = RepositorioSqliteJobs(proyecto.repositorio)
            nuevo = repositorio.registrar_evento(EventoProgreso(
                job_id="j1", tipo=TipoEvento.FIN, instante=1.0, clave="nuevo"))
            self.assertGreater(nuevo.secuencia, 41,
                               "la secuencia debe seguir donde la dejo la migracion")

    def test_una_historia_heredada_incoherente_aborta_y_restaura(self):
        casos = (("intento inexistente", "j1", "fantasma"),
                 ("intento de otro job", "otro", "a1"))
        for motivo, job_id, attempt_id in casos:
            with self.subTest(motivo=motivo):
                raiz = self.fixture_v4([(3, "j1", None, "sano")])
                conexion = _conexion(raiz / NOMBRE_BASE)
                try:
                    if job_id == "otro":
                        conexion.execute("INSERT INTO Job (id, kind, state, stage,"
                                         " contract_version, canonical_id, resources_json,"
                                         " max_attempts) VALUES ('otro', 'p', 'queued', 'p', '1',"
                                         " 'otro', '{\"cpu\": 1, \"disk\": 0, \"gpu\": 0,"
                                         " \"network\": 0}', 1)")
                    conexion.execute(
                        "INSERT INTO JobEvent (seq, job_id, attempt_id, kind, at, message_key)"
                        " VALUES (7, ?, ?, 'fin', 1.0, 'mal-ligado')", (job_id, attempt_id))
                    conexion.commit()
                finally:
                    conexion.close()

                with self.assertRaises(ErrorMigracionProyecto) as capturado:
                    abrir_proyecto(raiz)
                diagnostico = str(capturado.exception)
                # El mensaje tiene que servir para ir a arreglar la fila.
                self.assertIn("seq=7", diagnostico)
                self.assertIn(repr(attempt_id), diagnostico)
                self.assertIn("restauro", diagnostico)
                self.assertPreservadoEnV4(raiz)

    def assertPreservadoEnV4(self, raiz):
        self.assertEqual(json.loads((raiz / NOMBRE_MANIFIESTO).read_text("utf-8"))
                         ["schema_version"], 4)
        conexion = _conexion(raiz / NOMBRE_BASE)
        try:
            self.assertEqual(conexion.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(conexion.execute("PRAGMA foreign_key_list(JobEvent)").fetchall(),
                             [(0, 0, "Job", "job_id", "id", "NO ACTION", "NO ACTION", "NONE")],
                             "una v5 a medias habria dejado la clave compuesta")
            self.assertNotIn("JobAttempt_identidad",
                             [fila[1] for fila in conexion.execute("PRAGMA index_list(JobAttempt)")])
            self.assertEqual(conexion.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conexion.close()
        residuos = sorted(hijo.name for hijo in raiz.iterdir()
                          if ".bak" in hijo.name or "pending" in hijo.name)
        self.assertEqual(residuos, [])

    def test_una_v5_sin_su_clave_compuesta_no_devuelve_repositorio(self):
        raiz = self.fixture_v4([(3, "j1", None, "sano")])
        with abrir_proyecto(raiz):
            pass
        conexion = _conexion(raiz / NOMBRE_BASE)
        try:
            conexion.execute("ALTER TABLE JobEvent RENAME TO JobEvent_roto")
            conexion.execute(
                "CREATE TABLE JobEvent (seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                " job_id TEXT NOT NULL REFERENCES Job(id), attempt_id TEXT, kind TEXT NOT NULL,"
                " at REAL NOT NULL, message_key TEXT NOT NULL DEFAULT '', units_done REAL,"
                " units_total REAL, payload_json TEXT NOT NULL DEFAULT '{}')")
            conexion.execute("DROP TABLE JobEvent_roto")
            conexion.commit()
        finally:
            conexion.close()
        with self.assertRaises(ErrorProyecto) as capturado:
            abrir_proyecto(raiz)
        self.assertIn("validaciones", str(capturado.exception))

    def test_una_v5_sin_el_indice_padre_tampoco_se_acepta(self):
        raiz = self.fixture_v4([(3, "j1", None, "sano")])
        with abrir_proyecto(raiz):
            pass
        conexion = _conexion(raiz / NOMBRE_BASE)
        try:
            conexion.execute("DROP INDEX JobAttempt_identidad")
            conexion.commit()
        finally:
            conexion.close()
        with self.assertRaises(ErrorProyecto) as capturado:
            abrir_proyecto(raiz)
        self.assertIn("indices", str(capturado.exception))

    def test_una_migracion_v5_interrumpida_deja_v4_reabrible(self):
        raiz = self.fixture_v4([(3, "j1", None, "sano")])
        with self.assertRaises(ErrorMigracionProyecto):
            abrir_proyecto(raiz, fallo_migracion=lambda version: (_ for _ in ()).throw(
                RuntimeError("corte")) if version == 5 else None)
        self.assertPreservadoEnV4(raiz)
        with abrir_proyecto(raiz) as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().schema_version, VERSION_ESQUEMA)

    def test_un_proyecto_v5_abre_en_solo_lectura_sin_migrar_nada(self):
        raiz = self.fixture_v4([(3, "j1", None, "sano")])
        primero = abrir_proyecto(raiz)
        self.addCleanup(primero.close)
        segundo = abrir_proyecto(raiz)
        self.addCleanup(segundo.close)
        self.assertTrue(segundo.solo_lectura)
        self.assertEqual(segundo.repositorio.informacion().schema_version, VERSION_ESQUEMA)
        self.assertEqual([evento.clave for evento
                          in RepositorioSqliteJobs(segundo.repositorio).eventos("j1")], ["sano"])


# --------------------------------------------------------------------------- #
# T03-F05 El high-water de `JobEvent` sobrevive a la reconstruccion
# --------------------------------------------------------------------------- #

class AltoDeSecuenciaTests(MigracionV5Tests):
    """`sqlite_sequence` guarda el mayor `seq` jamas entregado, no el mayor vivo.

    Si se borraron eventos, el contador queda por encima de `MAX(seq)` y es lo
    unico que impide volver a entregar un numero que alguien ya vio. Reconstruir
    la tabla lo reduce —y si quedo vacia lo pierde entero, reemitiendo la
    historia desde 1—, asi que capturarlo y reponerlo es parte de la migracion.
    """

    def fijar_alto(self, raiz, alto):
        """Deja el contador de `JobEvent` en un valor concreto, o sin fila."""
        conexion = _conexion(raiz / NOMBRE_BASE)
        try:
            conexion.execute("DELETE FROM sqlite_sequence WHERE name='JobEvent'")
            if alto is not None:
                conexion.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('JobEvent', ?)",
                                 (alto,))
            conexion.commit()
        finally:
            conexion.close()
        return raiz

    def alto_actual(self, raiz):
        conexion = _conexion(raiz / NOMBRE_BASE, solo_lectura=True)
        try:
            fila = conexion.execute(
                "SELECT seq FROM sqlite_sequence WHERE name='JobEvent'").fetchone()
            return None if fila is None else fila[0]
        finally:
            conexion.close()

    def migrar_y_anotar(self, raiz):
        """Migra y devuelve la secuencia que recibe el primer evento nuevo."""
        with abrir_proyecto(raiz) as proyecto:
            repositorio = RepositorioSqliteJobs(proyecto.repositorio)
            nuevo = repositorio.registrar_evento(EventoProgreso(
                job_id="j1", tipo=TipoEvento.FIN, instante=1.0, clave="nuevo"))
            return nuevo.secuencia

    # -- preservacion exacta --------------------------------------------- #

    def test_una_historia_con_hueco_conserva_su_contador(self):
        """El caso del hallazgo: el 42 ya se entrego y no puede repetirse."""
        raiz = self.fijar_alto(self.fixture_v4([(41, "j1", None, "heredado")]), 100)
        self.assertEqual(self.migrar_y_anotar(raiz), 101)
        self.assertEqual(self.alto_actual(raiz), 101)

    def test_una_tabla_vacia_con_contador_no_reemite_la_historia(self):
        """Sin esto el contador cae a cero y se reparte otra vez desde 1."""
        raiz = self.fijar_alto(self.fixture_v4([]), 100)
        self.assertEqual(self.migrar_y_anotar(raiz), 101)

    def test_un_contador_que_coincide_con_el_maximo_sigue_donde_estaba(self):
        raiz = self.fijar_alto(self.fixture_v4([(41, "j1", None, "heredado")]), 41)
        self.assertEqual(self.migrar_y_anotar(raiz), 42)

    def test_sin_fila_de_contador_no_se_inventa_ninguno(self):
        """Ausencia legitima: en SQLite equivale a continuar desde `MAX(seq)`."""
        raiz = self.fijar_alto(self.fixture_v4([(41, "j1", None, "heredado")]), None)
        with abrir_proyecto(raiz):
            pass
        self.assertEqual(self.alto_actual(raiz), 41,
                         "el contador solo debe aparecer por la copia, no inventado")
        self.assertEqual(self.migrar_y_anotar(raiz), 42)

    def test_un_proyecto_sin_eventos_ni_contador_empieza_en_uno(self):
        """Se comprueba la semantica, no la representacion.

        SQLite puede dejar la fila del contador a cero al crear una tabla
        `AUTOINCREMENT`, y "sin fila" y "fila en cero" significan exactamente lo
        mismo: el siguiente `seq` es 1. Afirmar que no hay fila probaria un
        detalle del motor, no la garantia que importa.
        """
        raiz = self.fijar_alto(self.fixture_v4([]), None)
        self.assertIn(self.alto_actual(raiz), (None, 0))
        self.assertEqual(self.migrar_y_anotar(raiz), 1,
                         "sin historia previa no hay nada que preservar")

    # -- el cursor de la UI no ve numeros reutilizados -------------------- #

    def test_el_cursor_de_eventos_nunca_retrocede_sobre_lo_ya_entregado(self):
        raiz = self.fijar_alto(self.fixture_v4([(41, "j1", None, "heredado")]), 100)
        with abrir_proyecto(raiz) as proyecto:
            repositorio = RepositorioSqliteJobs(proyecto.repositorio)
            # Un lector que ya iba por 100 no puede encontrarse eventos "nuevos"
            # con secuencias que el creia pasadas.
            self.assertEqual(repositorio.eventos("j1", desde=100), ())
            nuevo = repositorio.registrar_evento(EventoProgreso(
                job_id="j1", tipo=TipoEvento.FIN, instante=1.0, clave="nuevo"))
            self.assertEqual([evento.clave for evento in repositorio.eventos("j1", desde=100)],
                             ["nuevo"])
            self.assertEqual(repositorio.eventos("j1", desde=nuevo.secuencia), ())
            self.assertGreater(nuevo.secuencia, 100)

    # -- contadores incoherentes ------------------------------------------ #

    def test_un_contador_incoherente_aborta_y_conserva_la_v4(self):
        casos = (("menor que el maximo", 7), ("negativo", -1), ("texto", "muchos"))
        for motivo, alto in casos:
            with self.subTest(motivo=motivo):
                raiz = self.fijar_alto(self.fixture_v4([(41, "j1", None, "heredado")]), alto)
                with self.assertRaises(ErrorMigracionProyecto) as capturado:
                    abrir_proyecto(raiz)
                diagnostico = str(capturado.exception)
                self.assertIn("contador de eventos", diagnostico)
                self.assertIn("restauro", diagnostico)
                self.assertPreservadoEnV4(raiz)
                self.assertEqual(self.alto_actual(raiz), alto,
                                 "la imagen anterior se conserva tal cual estaba")

    def test_un_fallo_al_reponer_el_contador_deja_la_v4_intacta(self):
        raiz = self.fijar_alto(self.fixture_v4([(41, "j1", None, "heredado")]), 100)
        with mock.patch.object(persistence, "_restaurar_alto_de_eventos",
                               side_effect=sqlite3.OperationalError("corte")):
            with self.assertRaises(ErrorMigracionProyecto):
                abrir_proyecto(raiz)
        self.assertPreservadoEnV4(raiz)
        self.assertEqual(self.alto_actual(raiz), 100)
        # Y una vez retirado el fallo, la migracion se completa sin secuelas.
        self.assertEqual(self.migrar_y_anotar(raiz), 101)


if __name__ == "__main__":
    unittest.main()
