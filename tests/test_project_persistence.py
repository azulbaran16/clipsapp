from pathlib import Path
from unittest import mock
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipperkick.domain.project import ErrorFuenteProyecto, ErrorMigracionProyecto, ErrorProyecto  # noqa: E402
from clipperkick.infrastructure.project import persistence  # noqa: E402
from clipperkick.infrastructure.project.persistence import (  # noqa: E402
    DIRECTORIOS_PROYECTO, NOMBRE_BASE, NOMBRE_LOCK, NOMBRE_MANIFIESTO, NOMBRE_MARCADOR_MIGRACION,
    NOMBRE_RESPALDO_BASE, NOMBRE_RESPALDO_MANIFIESTO, VERSION_ESQUEMA, _DDL_V2,
    _adquirir_lock, _conexion, _migrar, _recuperar_migracion, _sql_v1, abrir_proyecto, crear_proyecto,
)

SRC = str(Path(__file__).resolve().parents[1] / "src")
ENTORNO = dict(os.environ, PYTHONPATH=SRC)

# Cada hijo pelea por el lock varias veces y marca su exclusividad con un
# centinela O_EXCL: dos escritores solapados lo delatan sin depender de timing.
HIJO_LOCK_EXCLUSIVO = """
import os, sys, time
from pathlib import Path
from clipperkick.infrastructure.project.persistence import _adquirir_lock
carpeta, centinela = Path(sys.argv[1]), Path(sys.argv[2])
for _ in range(5):
    lock = None
    while lock is None:
        lock = _adquirir_lock(carpeta)
        if lock is None:
            time.sleep(0.005)
    try:
        try:
            os.close(os.open(centinela, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        except FileExistsError:
            print("DOBLE"); sys.exit(0)
        time.sleep(0.02)
        centinela.unlink()
    finally:
        lock.close()
print("OK")
"""

HIJO_RETIENE_LOCK = """
import sys, time
from pathlib import Path
from clipperkick.infrastructure.project.persistence import _adquirir_lock
carpeta, listo, alto = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
lock = _adquirir_lock(carpeta)
if lock is None:
    print("SIN_LOCK"); sys.exit(1)
listo.write_text("1")
while not alto.exists():
    time.sleep(0.02)
lock.close()
print("OK")
"""

HIJO_CREA = """
from clipperkick.infrastructure.project import crear_proyecto
import sys
try:
    p = crear_proyecto(sys.argv[1], sys.argv[2]); print('WIN'); p.close()
except Exception as e:
    print('LOSE', type(e).__name__)
"""

# Falla dentro de crear_proyecto con el lock en mano, avisa, y solo entonces
# libera: el padre puede observar el destino mientras el fallo esta en curso.
HIJO_CREA_FALLANDO = """
import sys, time
from pathlib import Path
from unittest import mock
from clipperkick.infrastructure.project import persistence
destino, listo, alto = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])

def publicar_imposible(*a, **k):
    listo.write_text("1")
    while not alto.exists():
        time.sleep(0.01)
    raise OSError(28, "No space left on device")

with mock.patch.object(persistence, "_escribir_manifest", publicar_imposible):
    try:
        persistence.crear_proyecto(destino, "Fallido")
        print("INESPERADO")
    except Exception as e:
        print("FALLO", type(e).__name__)
"""

# Mezcla creadores sanos y creadores que fallan al publicar, todos reintentando:
# el resultado admitido es un proyecto valido o un destino reintentable, nunca
# un destino perdido ni un proyecto a medio publicar.
HIJO_CREA_REINTENTANDO = """
import sys, time
from pathlib import Path
from unittest import mock
from clipperkick.infrastructure.project import persistence
from clipperkick.domain.project import ErrorProyecto
destino, modo = sys.argv[1], sys.argv[2]

def publicar_imposible(*a, **k):
    raise OSError(28, "No space left on device")

def intentar():
    if modo == "fallar":
        with mock.patch.object(persistence, "_escribir_manifest", publicar_imposible):
            persistence.crear_proyecto(destino, "Concurrente")
    else:
        persistence.crear_proyecto(destino, "Concurrente").close()

for _ in range(200):
    try:
        intentar()
        print("WIN"); break
    except ErrorProyecto as e:
        if "ya contiene archivos" in str(e):
            print("YA_EXISTE"); break
        time.sleep(0.01)
    except Exception as e:
        print("PERDIDA", type(e).__name__); break
else:
    print("AGOTADO")
if not Path(destino).exists():
    print("PERDIDA destino-desaparecido")
"""


class BaseProyecto(unittest.TestCase):
    def setUp(self):
        self.temporal = tempfile.TemporaryDirectory()
        self.base = Path(self.temporal.name)
        self.raiz = self.base / "demo.clipsapp"

    def tearDown(self):
        self.temporal.cleanup()

    def fixture_v1(self, nombre="Old", project_id="p1"):
        """Proyecto en la version inmediatamente anterior, listo para migrar."""
        self.raiz.mkdir()
        for directorio in DIRECTORIOS_PROYECTO:
            (self.raiz / directorio).mkdir()
        (self.raiz / NOMBRE_MANIFIESTO).write_text(
            json.dumps({"project_id": project_id, "name": nombre, "schema_version": 1}), encoding="utf-8")
        (self.raiz / NOMBRE_BASE).touch()
        conexion = _conexion(self.raiz / NOMBRE_BASE)
        _sql_v1(conexion)
        conexion.execute("PRAGMA user_version=1")
        conexion.execute("INSERT INTO Project VALUES (?, ?, '{}')", (project_id, nombre))
        conexion.commit()
        conexion.close()

    def version_y_tablas(self):
        conexion = _conexion(self.raiz / NOMBRE_BASE)
        try:
            version = conexion.execute("PRAGMA user_version").fetchone()[0]
            tablas = {fila[0] for fila in conexion.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            return version, tablas
        finally:
            conexion.close()

    def contenido(self):
        return sorted(hijo.name for hijo in self.raiz.iterdir())

    def hijo(self, codigo, *argumentos, espera=30):
        return subprocess.run([sys.executable, "-c", codigo, *map(str, argumentos)],
                              stdout=subprocess.PIPE, text=True, env=ENTORNO, timeout=espera)


# --------------------------------------------------------------------------- #
# Camino nominal
# --------------------------------------------------------------------------- #

class CicloDeVidaTests(BaseProyecto):
    def test_crea_reabre_y_valida_integridad(self):
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            identidad = proyecto.repositorio.informacion()
            self.assertEqual(identidad.nombre, "Demo")
            self.assertEqual(identidad.schema_version, VERSION_ESQUEMA)
            self.assertEqual(proyecto.repositorio.conexion.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            tablas = {fila[0] for fila in proyecto.repositorio.conexion.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"Project", "SourceAsset", "AnalysisArtifact", "Moment", "Draft", "Variant",
                             "EditRevision", "Job", "JobAttempt", "Output", "CapabilityBinding"} <= tablas)
        with abrir_proyecto(self.raiz) as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().project_id, identidad.project_id)
            self.assertFalse(proyecto.solo_lectura)
            conexion = proyecto.repositorio.conexion
            self.assertEqual(conexion.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conexion.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertTrue((self.raiz / NOMBRE_MANIFIESTO).is_file())
        self.assertTrue((self.raiz / NOMBRE_BASE).is_file())
        self.assertTrue(all((self.raiz / nombre).is_dir() for nombre in DIRECTORIOS_PROYECTO))

    def test_identidad_es_independiente_del_directorio_de_ejecucion(self):
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            identidad = proyecto.repositorio.informacion()
        anterior = os.getcwd()
        os.chdir(self.base)
        try:
            with abrir_proyecto(os.path.relpath(self.raiz, self.base)) as proyecto:
                self.assertEqual(proyecto.repositorio.informacion(), identidad)
        finally:
            os.chdir(anterior)

    def test_carpeta_sin_manifiesto_no_se_abre_ni_deja_rastro(self):
        vacia = self.base / "no-es-proyecto"
        vacia.mkdir()
        with self.assertRaises(ErrorProyecto):
            abrir_proyecto(vacia)
        self.assertEqual(sorted(hijo.name for hijo in vacia.iterdir()), [],
                         "una apertura fallida no puede escribir en una carpeta ajena")


# --------------------------------------------------------------------------- #
# Creacion: guardia por destino, lock sin ventana, publicacion reintentable
# --------------------------------------------------------------------------- #

class CreacionTests(BaseProyecto):
    def test_creacion_devuelve_el_proyecto_con_su_lock_ya_retenido(self):
        with crear_proyecto(self.raiz, "Demo"):
            self.assertIsNone(_adquirir_lock(self.raiz),
                              "no puede existir ventana entre publicar y poseer el lock")
            segundo = abrir_proyecto(self.raiz)
            self.assertTrue(segundo.solo_lectura)
            segundo.close()

    def test_fallo_al_publicar_manifiesto_deja_el_destino_reintentable(self):
        def replace_fallido(_origen, _destino):
            raise OSError("disco lleno")

        with self.assertRaises(ErrorProyecto):
            crear_proyecto(self.raiz, "Demo", replace=replace_fallido)
        self.assertEqual(self.contenido(), [NOMBRE_LOCK],
                         "el residuo reintentable es la carpeta con su lock, sin nada mas")
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().nombre, "Demo")

    def test_un_archivo_ajeno_aparecido_tras_el_precheck_no_se_borra(self):
        """Ventana precheck→lock: el recheck rechaza, y el cleanup no debe barrerlo."""
        intruso = self.raiz / "documento-del-usuario.txt"
        adquirir_real = persistence._adquirir_lock

        def aparece_el_intruso(carpeta):
            intruso.write_text("no tocar", encoding="utf-8")
            return adquirir_real(carpeta)

        with mock.patch.object(persistence, "_adquirir_lock", aparece_el_intruso):
            with self.assertRaises(ErrorProyecto) as capturado:
                crear_proyecto(self.raiz, "Demo")
        self.assertIn("ya contiene archivos", str(capturado.exception))
        self.assertTrue(self.raiz.is_dir(), "el destino no puede desaparecer")
        self.assertTrue(intruso.is_file(), "el cleanup borro el archivo que el recheck acababa de proteger")
        self.assertEqual(intruso.read_text(encoding="utf-8"), "no tocar")

    def test_un_intruso_anidado_en_un_directorio_permitido_no_se_borra(self):
        """El nombre `sources` esta permitido; su contenido no lo esta.

        Un allowlist que solo mira los hijos de primer nivel deja que un archivo
        del usuario viaje de polizon dentro de un nombre autorizado y sea
        consumido por un borrado recursivo.
        """
        for directorio in DIRECTORIOS_PROYECTO:
            with self.subTest(directorio=directorio):
                temporal = tempfile.TemporaryDirectory()
                self.addCleanup(temporal.cleanup)
                raiz = Path(temporal.name) / "demo.clipsapp"
                intruso = raiz / directorio / "user.txt"
                adquirir_real = persistence._adquirir_lock

                def aparece_el_intruso(carpeta, _intruso=intruso):
                    _intruso.parent.mkdir(parents=True, exist_ok=True)
                    _intruso.write_text("no tocar", encoding="utf-8")
                    return adquirir_real(carpeta)

                with mock.patch.object(persistence, "_adquirir_lock", aparece_el_intruso):
                    with self.assertRaises(ErrorProyecto) as capturado:
                        crear_proyecto(raiz, "Demo")
                self.assertIn("ya contiene archivos", str(capturado.exception))
                self.assertTrue(intruso.is_file(), f"{directorio}/user.txt fue borrado")
                self.assertEqual(intruso.read_text(encoding="utf-8"), "no tocar")
                self.assertFalse((raiz / NOMBRE_MANIFIESTO).exists(),
                                 "no puede publicarse un proyecto sobre contenido ajeno")

    def test_un_intruso_profundamente_anidado_tambien_cancela_la_limpieza(self):
        intruso = self.raiz / "cache" / "sub" / "otro" / "user.bin"
        adquirir_real = persistence._adquirir_lock

        def aparece_el_intruso(carpeta):
            intruso.parent.mkdir(parents=True, exist_ok=True)
            intruso.write_bytes(b"no tocar")
            return adquirir_real(carpeta)

        with mock.patch.object(persistence, "_adquirir_lock", aparece_el_intruso):
            with self.assertRaises(ErrorProyecto):
                crear_proyecto(self.raiz, "Demo")
        self.assertTrue(intruso.is_file())
        self.assertFalse((self.raiz / NOMBRE_MANIFIESTO).exists())

    def test_un_directorio_permitido_pero_vacio_si_es_residuo_retirable(self):
        """El contrapeso: exigir vacio no puede volver irreclamable un residuo real."""
        self.raiz.mkdir()
        for directorio in DIRECTORIOS_PROYECTO:
            (self.raiz / directorio).mkdir()
        (self.raiz / (NOMBRE_BASE + ".tmp")).write_bytes(b"basura")
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().nombre, "Demo")

    def test_un_enlace_simbolico_con_nombre_permitido_no_se_reclama(self):
        externa = self.base / "carpeta-del-usuario"
        externa.mkdir()
        (externa / "user.txt").write_text("no tocar", encoding="utf-8")
        self.raiz.mkdir()
        try:
            (self.raiz / "sources").symlink_to(externa, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"este sistema no permite crear enlaces simbolicos: {error}")
        with self.assertRaises(ErrorProyecto):
            crear_proyecto(self.raiz, "Demo")
        self.assertTrue((externa / "user.txt").is_file(), "se siguio el enlace fuera del destino")

    def test_un_archivo_ajeno_junto_a_residuos_propios_cancela_todo_el_cleanup(self):
        intruso = self.raiz / "documento-del-usuario.txt"
        adquirir_real = persistence._adquirir_lock

        def aparece_el_intruso(carpeta):
            lock = adquirir_real(carpeta)
            intruso.write_text("no tocar", encoding="utf-8")
            (carpeta / "sources").mkdir(exist_ok=True)
            return lock

        with mock.patch.object(persistence, "_adquirir_lock", aparece_el_intruso):
            with self.assertRaises(ErrorProyecto):
                crear_proyecto(self.raiz, "Demo")
        self.assertTrue(intruso.is_file())
        self.assertTrue((self.raiz / "sources").is_dir(),
                        "un residuo propio no se retira si comparte carpeta con contenido ajeno")

    def test_todo_archivo_transitorio_de_la_creacion_esta_en_el_allowlist(self):
        """Si la creacion produce un nombre no reconocido, un observador
        concurrente lo confunde con contenido ajeno y el destino deja de ser
        reclamable. Se comprueba en el instante de mayor estado transitorio:
        con la base temporal abierta y su transaccion en curso."""
        vistos: set[str] = set()
        sql_real = persistence._sql_v2

        def espiar(conexion):
            vistos.update(hijo.name for hijo in self.raiz.iterdir())
            return sql_real(conexion)

        with mock.patch.object(persistence, "_sql_v2", espiar):
            with crear_proyecto(self.raiz, "Demo"):
                vistos.update(hijo.name for hijo in self.raiz.iterdir())
        vistos.update(hijo.name for hijo in self.raiz.iterdir())
        self.assertTrue(vistos, "el espia no llego a observar la creacion")
        ajenos = vistos - set(persistence._RESIDUOS_DE_CREACION) - {NOMBRE_MANIFIESTO}
        self.assertEqual(ajenos, set(), f"nombres de creacion fuera del allowlist: {sorted(ajenos)}")

    def test_el_diario_de_rollback_es_residuo_reconocido_y_se_neutraliza(self):
        """SQLite usa `-journal` antes de que WAL tome efecto.

        Si ese nombre no se reconoce pasan dos cosas: un creador concurrente lo
        toma por contenido ajeno y el destino deja de ser reclamable, y un
        diario huerfano sobrevive a `_restaurar`, con lo que SQLite podria
        revertir la imagen recien restaurada usando el diario de la anterior.
        """
        base = self.base / NOMBRE_BASE
        diario = base.with_name(base.name + "-journal")
        conexion = sqlite3.connect(base)  # journal_mode por defecto: rollback journal
        try:
            conexion.execute("CREATE TABLE t (id TEXT)")
            conexion.execute("BEGIN IMMEDIATE")
            conexion.execute("INSERT INTO t VALUES ('x')")
            self.assertTrue(diario.is_file(), "el fixture no produjo un diario de rollback real")
            conexion.rollback()
        finally:
            conexion.close()
        for nombre in (NOMBRE_BASE, NOMBRE_BASE + ".tmp"):
            self.assertIn(nombre + "-journal", persistence._RESIDUOS_DE_CREACION)
        diario.write_bytes(b"diario huerfano")
        persistence._neutralizar_sidecars(base)
        self.assertFalse(diario.exists(), "_restaurar dejaria vivo el diario de la imagen descartada")

    def test_un_creador_fallido_no_destruye_el_destino_ni_el_lock_de_otro(self):
        """A falla y limpia; B, que ya reclamo el residuo, debe seguir vivo."""
        listo, alto = self.base / "listo", self.base / "alto"
        fallando = subprocess.Popen(
            [sys.executable, "-c", HIJO_CREA_FALLANDO, str(self.raiz), str(listo), str(alto)],
            stdout=subprocess.PIPE, text=True, env=ENTORNO)
        try:
            limite = time.monotonic() + 30
            while not listo.exists() and time.monotonic() < limite:
                time.sleep(0.01)
            self.assertTrue(listo.exists(), "el hijo no llego a fallar dentro de crear_proyecto")
        finally:
            alto.write_text("1")
        self.assertEqual(fallando.communicate(timeout=30)[0].strip(), "FALLO ErrorProyecto")
        self.assertTrue(self.raiz.is_dir(), "el destino no puede desaparecer tras un fallo")
        with crear_proyecto(self.raiz, "Segundo") as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().nombre, "Segundo")

    def test_creadores_fallidos_y_sanos_en_paralelo_nunca_pierden_el_proyecto(self):
        for _ in range(3):
            destino = self.base / ("mixto-" + str(uuid.uuid4()) + ".clipsapp")
            procesos = [subprocess.Popen([sys.executable, "-c", HIJO_CREA_REINTENTANDO, str(destino), modo],
                                         stdout=subprocess.PIPE, text=True, env=ENTORNO)
                        for modo in ("fallar", "crear", "fallar", "crear")]
            salidas = [proceso.communicate(timeout=60)[0].strip() for proceso in procesos]
            self.assertTrue(any(salida == "WIN" for salida in salidas), salidas)
            self.assertNotIn("PERDIDA", salidas, salidas)
            with abrir_proyecto(destino) as proyecto:
                self.assertEqual(proyecto.repositorio.informacion().nombre, "Concurrente")

    def test_creacion_abortada_por_crash_se_reclama_en_el_siguiente_intento(self):
        self.raiz.mkdir()
        (self.raiz / NOMBRE_LOCK).write_text(" ")
        (self.raiz / "sources").mkdir()
        (self.raiz / (NOMBRE_BASE + ".tmp")).write_bytes(b"basura")
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().nombre, "Demo")
        self.assertFalse((self.raiz / (NOMBRE_BASE + ".tmp")).exists())

    def test_carpeta_con_contenido_ajeno_nunca_se_reclama(self):
        self.raiz.mkdir()
        (self.raiz / "documento-del-usuario.txt").write_text("no tocar", encoding="utf-8")
        with self.assertRaises(ErrorProyecto) as capturado:
            crear_proyecto(self.raiz, "Demo")
        self.assertIn("ya contiene archivos", str(capturado.exception))
        self.assertTrue((self.raiz / "documento-del-usuario.txt").is_file())

    def test_crear_sobre_un_proyecto_publicado_nunca_lo_destruye(self):
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            identidad = proyecto.repositorio.informacion()
        with self.assertRaises(ErrorProyecto):
            crear_proyecto(self.raiz, "Otro")
        # Y tampoco si el reclamo llegase a pasar: el guardia bajo lock lo frena.
        with mock.patch.object(persistence, "_reclamar_destino", lambda _destino: None):
            with self.assertRaises(ErrorProyecto):
                crear_proyecto(self.raiz, "Otro")
        with abrir_proyecto(self.raiz) as proyecto:
            self.assertEqual(proyecto.repositorio.informacion(), identidad)

    def test_dos_creadores_del_mismo_destino_publican_exactamente_un_proyecto(self):
        for _ in range(3):
            destino = self.base / ("mismo-" + str(uuid.uuid4()) + ".clipsapp")
            procesos = [subprocess.Popen([sys.executable, "-c", HIJO_CREA, str(destino), "Concurrente"],
                                         stdout=subprocess.PIPE, text=True, env=ENTORNO) for _ in range(2)]
            salidas = [proceso.communicate(timeout=30)[0].strip() for proceso in procesos]
            self.assertEqual(sum(salida == "WIN" for salida in salidas), 1, salidas)
            self.assertTrue(all(salida == "WIN" or salida.startswith("LOSE ErrorProyecto") for salida in salidas), salidas)
            with abrir_proyecto(destino) as proyecto:
                self.assertEqual(proyecto.repositorio.informacion().nombre, "Concurrente")

    def test_creadores_de_destinos_distintos_en_la_misma_carpeta_no_se_excluyen(self):
        """El guardia es el destino, no el directorio contenedor."""
        for _ in range(3):
            marca = str(uuid.uuid4())
            destinos = [self.base / f"{nombre}-{marca}.clipsapp" for nombre in ("alpha", "beta", "gamma", "delta")]
            procesos = [subprocess.Popen([sys.executable, "-c", HIJO_CREA, str(destino), destino.stem],
                                         stdout=subprocess.PIPE, text=True, env=ENTORNO) for destino in destinos]
            salidas = [proceso.communicate(timeout=30)[0].strip() for proceso in procesos]
            self.assertEqual(salidas, ["WIN"] * len(destinos), salidas)
            for destino in destinos:
                with abrir_proyecto(destino) as proyecto:
                    self.assertEqual(proyecto.repositorio.informacion().nombre, destino.stem)
            self.assertEqual(sorted(hijo.name for hijo in self.base.iterdir() if marca in hijo.name),
                             sorted(destino.name for destino in destinos),
                             "la creacion no puede dejar residuos en la carpeta del usuario")


# --------------------------------------------------------------------------- #
# Lock de escritor
# --------------------------------------------------------------------------- #

class LockEscritorTests(BaseProyecto):
    def test_segunda_apertura_es_solo_lectura_y_no_libera_el_lock_ajeno(self):
        primero = crear_proyecto(self.raiz, "Demo")
        segundo = abrir_proyecto(self.raiz)
        self.assertFalse(primero.solo_lectura)
        self.assertTrue(segundo.solo_lectura)
        segundo.close()
        self.assertIsNone(_adquirir_lock(self.raiz), "cerrar la sesion read-only no puede soltar el lock del escritor")
        primero.close()
        liberado = _adquirir_lock(self.raiz)
        self.assertIsNotNone(liberado)
        liberado.close()

    def test_el_archivo_de_lock_nunca_se_desvincula(self):
        """Desvincularlo permitiria bloquear inodos distintos sobre la misma ruta."""
        with crear_proyecto(self.raiz, "Demo"):
            pass
        ruta = self.raiz / NOMBRE_LOCK
        self.assertTrue(ruta.is_file())
        identidad = ruta.stat().st_ino
        for _ in range(5):
            lock = _adquirir_lock(self.raiz)
            self.assertIsNotNone(lock)
            lock.close()
            self.assertTrue(ruta.is_file(), "el lock file debe sobrevivir a su sesion")
            self.assertEqual(ruta.stat().st_ino, identidad, "el inodo del lock no puede cambiar entre sesiones")

    def test_procesos_en_disputa_nunca_escriben_a_la_vez(self):
        """Reproduce la carrera de release/acquire en ambos adaptadores de lock."""
        with crear_proyecto(self.raiz, "Demo"):
            pass
        centinela = self.base / "escritor.activo"
        for _ in range(2):
            procesos = [subprocess.Popen([sys.executable, "-c", HIJO_LOCK_EXCLUSIVO, str(self.raiz), str(centinela)],
                                         stdout=subprocess.PIPE, text=True, env=ENTORNO) for _ in range(4)]
            salidas = [proceso.communicate(timeout=60)[0].strip() for proceso in procesos]
            self.assertEqual(salidas, ["OK"] * 4, salidas)
            self.assertFalse(centinela.exists())

    def test_lock_se_libera_despues_de_crash_de_proceso(self):
        codigo = "from clipperkick.infrastructure.project import crear_proyecto; import os,sys; crear_proyecto(sys.argv[1], 'Crash'); os._exit(17)"
        proceso = subprocess.run([sys.executable, "-c", codigo, str(self.raiz)], env=ENTORNO, timeout=30)
        self.assertEqual(proceso.returncode, 17)
        self.assertTrue((self.raiz / NOMBRE_LOCK).is_file(), "el archivo remanente no debe impedir reabrir")
        with abrir_proyecto(self.raiz) as proyecto:
            self.assertFalse(proyecto.solo_lectura)


# --------------------------------------------------------------------------- #
# Migracion, marcador y recuperacion
# --------------------------------------------------------------------------- #

class MigracionTests(BaseProyecto):
    def test_migra_fixture_anterior_y_restaura_ante_fallo_inyectado(self):
        self.fixture_v1(nombre="Anterior")
        with self.assertRaises(ErrorMigracionProyecto):
            abrir_proyecto(self.raiz, fallo_migracion=lambda _v: (_ for _ in ()).throw(RuntimeError("fallo")))
        self.assertEqual(json.loads((self.raiz / NOMBRE_MANIFIESTO).read_text("utf-8"))["schema_version"], 1)
        version, tablas = self.version_y_tablas()
        self.assertEqual(version, 1)
        self.assertNotIn("Draft", tablas)
        self.assertNotIn(NOMBRE_MARCADOR_MIGRACION, self.contenido())
        self.assertNotIn(NOMBRE_RESPALDO_BASE, self.contenido())
        with abrir_proyecto(self.raiz) as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().schema_version, VERSION_ESQUEMA)

    def test_rollback_de_migracion_conserva_commit_confirmado_en_wal(self):
        self.fixture_v1()
        codigo = ("import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"PRAGMA journal_mode=WAL\");"
                  " c.execute(\"UPDATE Project SET name='CommittedInWal'\"); c.commit(); os._exit(0)")
        self.assertEqual(subprocess.run([sys.executable, "-c", codigo, str(self.raiz / NOMBRE_BASE)], timeout=30).returncode, 0)
        wal = self.raiz / (NOMBRE_BASE + "-wal")
        self.assertTrue(wal.is_file() and wal.stat().st_size > 0, "el commit debe permanecer en WAL antes del rollback")
        with self.assertRaises(ErrorMigracionProyecto):
            abrir_proyecto(self.raiz, fallo_migracion=lambda _v: (_ for _ in ()).throw(RuntimeError("fallo")))
        conexion = _conexion(self.raiz / NOMBRE_BASE)
        self.assertEqual(conexion.execute("SELECT name FROM Project").fetchone()[0], "CommittedInWal")
        self.assertEqual(conexion.execute("PRAGMA user_version").fetchone()[0], 1)
        conexion.close()
        with abrir_proyecto(self.raiz) as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().nombre, "CommittedInWal")

    def test_crash_real_en_migracion_no_deja_ddl_parcial_y_se_recupera(self):
        """El estado inmediatamente posterior a la recuperacion es el que importa."""
        self.fixture_v1()
        codigo = ("from pathlib import Path; import json,os,sys;"
                  " from clipperkick.infrastructure.project.persistence import _migrar;"
                  " p=Path(sys.argv[1]); _migrar(p, json.loads((p/'project.json').read_text()), lambda _: os._exit(17))")
        self.assertEqual(subprocess.run([sys.executable, "-c", codigo, str(self.raiz)], env=ENTORNO, timeout=30).returncode, 17)
        self.assertTrue((self.raiz / NOMBRE_MARCADOR_MIGRACION).is_file())
        version, tablas = self.version_y_tablas()
        self.assertEqual(version, 1, "el DDL vive en una unica transaccion: un crash no puede confirmarlo")
        self.assertNotIn("Draft", tablas)

        _recuperar_migracion(self.raiz)
        version, tablas = self.version_y_tablas()
        self.assertEqual(version, 1)
        self.assertNotIn("Draft", tablas)
        for residuo in (NOMBRE_MARCADOR_MIGRACION, NOMBRE_RESPALDO_BASE, NOMBRE_RESPALDO_MANIFIESTO,
                        NOMBRE_BASE + "-wal", NOMBRE_BASE + "-shm"):
            self.assertNotIn(residuo, self.contenido())
        with abrir_proyecto(self.raiz) as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().nombre, "Old")
            self.assertEqual(proyecto.repositorio.informacion().schema_version, VERSION_ESQUEMA)

    def test_restaurar_neutraliza_el_wal_huerfano_y_no_resucita_la_imagen_descartada(self):
        """Un `-wal` sobreviviente reaplicaria el trabajo que la restauracion deshace."""
        with crear_proyecto(self.raiz, "Demo"):
            pass
        shutil.copy2(self.raiz / NOMBRE_BASE, self.raiz / NOMBRE_RESPALDO_BASE)
        shutil.copy2(self.raiz / NOMBRE_MANIFIESTO, self.raiz / NOMBRE_RESPALDO_MANIFIESTO)
        codigo = ("import os,sqlite3,sys; c=sqlite3.connect(sys.argv[1]); c.execute(\"PRAGMA journal_mode=WAL\");"
                  " c.execute(\"UPDATE Project SET name='Descartado'\");"
                  " c.execute(\"PRAGMA user_version=99\"); c.commit(); os._exit(0)")
        self.assertEqual(subprocess.run([sys.executable, "-c", codigo, str(self.raiz / NOMBRE_BASE)], timeout=30).returncode, 0)
        wal = self.raiz / (NOMBRE_BASE + "-wal")
        self.assertTrue(wal.is_file() and wal.stat().st_size > 0)
        (self.raiz / NOMBRE_MARCADOR_MIGRACION).write_text(
            json.dumps({"formato": "clipsapp-migration/1", "desde": 1, "hasta": 2}), encoding="utf-8")

        _recuperar_migracion(self.raiz)

        self.assertFalse(wal.exists(), "el WAL de la imagen descartada no puede sobrevivir a la restauracion")
        self.assertFalse((self.raiz / (NOMBRE_BASE + "-shm")).exists())
        conexion = _conexion(self.raiz / NOMBRE_BASE)
        try:
            self.assertEqual(conexion.execute("SELECT name FROM Project").fetchone()[0], "Demo")
            self.assertEqual(conexion.execute("PRAGMA user_version").fetchone()[0], VERSION_ESQUEMA)
            self.assertEqual(conexion.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            conexion.close()
        with abrir_proyecto(self.raiz) as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().nombre, "Demo")

    def test_marcador_huerfano_sin_respaldos_no_bloquea_el_proyecto(self):
        with crear_proyecto(self.raiz, "Demo"):
            pass
        (self.raiz / NOMBRE_MARCADOR_MIGRACION).write_text(json.dumps({"formato": "clipsapp-migration/1"}), encoding="utf-8")
        for _ in range(3):
            with abrir_proyecto(self.raiz) as proyecto:
                self.assertEqual(proyecto.repositorio.informacion().nombre, "Demo")
        self.assertFalse((self.raiz / NOMBRE_MARCADOR_MIGRACION).exists())

    def test_marcador_malformado_se_trata_como_pendiente_sin_excepcion_cruda(self):
        with crear_proyecto(self.raiz, "Demo"):
            pass
        shutil.copy2(self.raiz / NOMBRE_BASE, self.raiz / NOMBRE_RESPALDO_BASE)
        shutil.copy2(self.raiz / NOMBRE_MANIFIESTO, self.raiz / NOMBRE_RESPALDO_MANIFIESTO)
        (self.raiz / NOMBRE_MANIFIESTO).write_text('{"project_id":"roto","name":"Roto","schema_version":2}', encoding="utf-8")
        (self.raiz / NOMBRE_MARCADOR_MIGRACION).write_bytes(b"\xff\xfe no es json {{{")
        with abrir_proyecto(self.raiz) as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().nombre, "Demo")
        self.assertFalse((self.raiz / NOMBRE_MARCADOR_MIGRACION).exists())
        self.assertEqual(json.loads((self.raiz / NOMBRE_MANIFIESTO).read_text("utf-8"))["name"], "Demo")

    def test_marcador_manipulado_no_alcanza_archivos_fuera_del_proyecto(self):
        with crear_proyecto(self.raiz, "Demo"):
            pass
        victima_db = self.base / "otra.sqlite3"
        victima_json = self.base / "otro.json"
        victima_db.write_bytes(b"contenido ajeno")
        victima_json.write_text('{"project_id":"ajeno","name":"Ajeno","schema_version":2}', encoding="utf-8")
        shutil.copy2(self.raiz / NOMBRE_BASE, self.raiz / NOMBRE_RESPALDO_BASE)
        shutil.copy2(self.raiz / NOMBRE_MANIFIESTO, self.raiz / NOMBRE_RESPALDO_MANIFIESTO)
        for backup_db, backup_manifest in (("../otra.sqlite3", "../otro.json"),
                                           (str(victima_db), str(victima_json))):
            (self.raiz / NOMBRE_MARCADOR_MIGRACION).write_text(
                json.dumps({"backup_db": backup_db, "backup_manifest": backup_manifest}), encoding="utf-8")
            with abrir_proyecto(self.raiz) as proyecto:
                self.assertEqual(proyecto.repositorio.informacion().nombre, "Demo")
            self.assertTrue(victima_db.is_file(), f"{backup_db} borro un archivo externo")
            self.assertTrue(victima_json.is_file(), f"{backup_manifest} borro un archivo externo")
            self.assertEqual(victima_db.read_bytes(), b"contenido ajeno")
            shutil.copy2(self.raiz / NOMBRE_BASE, self.raiz / NOMBRE_RESPALDO_BASE)
            shutil.copy2(self.raiz / NOMBRE_MANIFIESTO, self.raiz / NOMBRE_RESPALDO_MANIFIESTO)

    def test_respaldo_fallido_no_deja_bak_huerfano_ni_error_crudo(self):
        """El `.bak` de la base ya existe cuando falla el del manifiesto: hay que compensarlo."""
        self.fixture_v1()
        manifest = json.loads((self.raiz / NOMBRE_MANIFIESTO).read_text("utf-8"))
        with mock.patch("shutil.copyfile", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(ErrorMigracionProyecto) as capturado:
                _migrar(self.raiz, manifest)
        self.assertNotIsInstance(capturado.exception, OSError)
        for residuo in (NOMBRE_RESPALDO_BASE, NOMBRE_RESPALDO_BASE + ".tmp",
                        NOMBRE_RESPALDO_MANIFIESTO, NOMBRE_RESPALDO_MANIFIESTO + ".tmp",
                        NOMBRE_MARCADOR_MIGRACION):
            self.assertNotIn(residuo, self.contenido())
        with abrir_proyecto(self.raiz) as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().schema_version, VERSION_ESQUEMA)

    def test_backup_de_sqlite_fallido_se_reporta_tipado_y_sin_residuos(self):
        self.fixture_v1()
        real = sqlite3.connect

        def solo_falla_el_respaldo(objetivo, *argumentos, **claves):
            if isinstance(objetivo, Path):  # solo `_respaldar` conecta por Path.
                raise sqlite3.OperationalError("disk I/O error")
            return real(objetivo, *argumentos, **claves)

        with mock.patch("sqlite3.connect", side_effect=solo_falla_el_respaldo):
            with self.assertRaises(ErrorMigracionProyecto) as capturado:
                _migrar(self.raiz, json.loads((self.raiz / NOMBRE_MANIFIESTO).read_text("utf-8")))
        self.assertNotIsInstance(capturado.exception, sqlite3.Error)
        for residuo in (NOMBRE_RESPALDO_BASE, NOMBRE_RESPALDO_BASE + ".tmp", NOMBRE_MARCADOR_MIGRACION):
            self.assertNotIn(residuo, self.contenido())
        with abrir_proyecto(self.raiz) as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().schema_version, VERSION_ESQUEMA)

    def test_el_marcador_se_escribe_con_fsync_antes_del_replace(self):
        """El marcador es el pivote del protocolo: no puede quedar vacio tras un corte."""
        eventos = []
        destino = self.base / "pivote.json"
        with mock.patch("os.fsync", side_effect=lambda fd: eventos.append("fsync")):
            persistence._escribir_bytes_durable(
                destino, b"contenido",
                replace=lambda origen, final: (eventos.append("replace"), os.replace(origen, final))[1])
        self.assertEqual(eventos[:2], ["fsync", "replace"])
        self.assertEqual(destino.read_bytes(), b"contenido")


# --------------------------------------------------------------------------- #
# Aislamiento de la instancia read-only
# --------------------------------------------------------------------------- #

class SoloLecturaTests(BaseProyecto):
    def test_instancia_sin_lock_no_recupera_ni_altera_el_proyecto(self):
        """La recuperacion es privilegio del escritor: quien no tiene lock no toca nada."""
        with crear_proyecto(self.raiz, "Demo"):
            pass
        shutil.copy2(self.raiz / NOMBRE_BASE, self.raiz / NOMBRE_RESPALDO_BASE)
        shutil.copy2(self.raiz / NOMBRE_MANIFIESTO, self.raiz / NOMBRE_RESPALDO_MANIFIESTO)
        (self.raiz / NOMBRE_MARCADOR_MIGRACION).write_text(json.dumps({"formato": "clipsapp-migration/1"}), encoding="utf-8")
        listo, alto = self.base / "listo", self.base / "alto"
        hijo = subprocess.Popen([sys.executable, "-c", HIJO_RETIENE_LOCK, str(self.raiz), str(listo), str(alto)],
                                stdout=subprocess.PIPE, text=True, env=ENTORNO)
        try:
            limite = time.monotonic() + 30
            while not listo.exists() and time.monotonic() < limite:
                time.sleep(0.01)
            self.assertTrue(listo.exists(), "el hijo no llego a retener el lock")
            antes = self.contenido()
            with self.assertRaises(ErrorProyecto):
                abrir_proyecto(self.raiz)
            self.assertEqual(self.contenido(), antes,
                             "una instancia read-only no puede borrar respaldos ni marcador")
        finally:
            alto.write_text("1")
            self.assertEqual(hijo.communicate(timeout=30)[0].strip(), "OK")
        with abrir_proyecto(self.raiz) as proyecto:
            self.assertFalse(proyecto.solo_lectura)
            self.assertEqual(proyecto.repositorio.informacion().nombre, "Demo")

    def test_read_only_no_escribe_al_agregar_una_fuente(self):
        origen = self.base / "origen.mp4"
        origen.write_bytes(b"video")
        with crear_proyecto(self.raiz, "Demo"):
            with abrir_proyecto(self.raiz) as segundo:
                self.assertTrue(segundo.solo_lectura)
                with self.assertRaises(ErrorProyecto):
                    segundo.repositorio.agregar_fuente(str(origen), "fp-1", copiar=True)
                with self.assertRaises(ErrorProyecto):
                    segundo.repositorio.relocalizar_fuente("cualquiera", str(origen), lambda _r: "fp-1")
                self.assertEqual(sorted(hijo.name for hijo in (self.raiz / "sources").iterdir()), [])

    def test_reconciliacion_read_only_reporta_discrepancias_sin_mutar(self):
        externa = self.base / "externa.mp4"
        externa.write_bytes(b"video")
        with crear_proyecto(self.raiz, "Demo") as primero:
            fuente = primero.repositorio.agregar_fuente(str(externa), "fp-1")
            with primero.repositorio.transaccion() as tx:
                tx.execute("INSERT INTO AnalysisArtifact (id, source_id, relative_path, kind, state) VALUES ('a', ?, 'cache/ausente.json', 'proxy', 'available')", (fuente,))
            externa.unlink()
            with abrir_proyecto(self.raiz) as segundo:
                resultado = segundo.repositorio.reconciliar()
                self.assertEqual((resultado.fuentes_faltantes, resultado.artefactos_reconstruibles), (1, 1))
                estados = segundo.repositorio.conexion.execute(
                    "SELECT (SELECT state FROM SourceAsset), (SELECT state FROM AnalysisArtifact)").fetchone()
                self.assertEqual(estados, ("available", "available"), "read-only detecta, no persiste")

    def test_reconciliacion_de_un_proyecto_v1_no_filtra_errores_de_sqlite(self):
        self.fixture_v1()
        hijo = subprocess.Popen([sys.executable, "-c", HIJO_RETIENE_LOCK, str(self.raiz),
                                 str(self.base / "listo"), str(self.base / "alto")],
                                stdout=subprocess.PIPE, text=True, env=ENTORNO)
        try:
            limite = time.monotonic() + 30
            while not (self.base / "listo").exists() and time.monotonic() < limite:
                time.sleep(0.01)
            with abrir_proyecto(self.raiz) as segundo:
                self.assertTrue(segundo.solo_lectura)
                self.assertEqual(segundo.repositorio.informacion().schema_version, 1)
                resultado = segundo.repositorio.reconciliar()
                self.assertEqual((resultado.fuentes_faltantes, resultado.artefactos_reconstruibles), (0, 0))
        finally:
            (self.base / "alto").write_text("1")
            hijo.communicate(timeout=30)


# --------------------------------------------------------------------------- #
# Coherencia del esquema y cierre de handles
# --------------------------------------------------------------------------- #

class CoherenciaAperturaTests(BaseProyecto):
    def base_declarada_v2_sin_tablas_v2(self):
        self.raiz.mkdir()
        for directorio in DIRECTORIOS_PROYECTO:
            (self.raiz / directorio).mkdir()
        (self.raiz / NOMBRE_MANIFIESTO).write_text(
            json.dumps({"project_id": "p1", "name": "X", "schema_version": 2}), encoding="utf-8")
        (self.raiz / NOMBRE_BASE).touch()
        conexion = _conexion(self.raiz / NOMBRE_BASE)
        _sql_v1(conexion)
        conexion.execute("PRAGMA user_version=2")
        conexion.execute("INSERT INTO Project VALUES ('p1', 'X', '{}')")
        conexion.commit()
        conexion.close()

    def test_version_declarada_sin_sus_tablas_no_devuelve_repositorio(self):
        self.base_declarada_v2_sin_tablas_v2()
        with self.assertRaises(ErrorProyecto) as capturado:
            abrir_proyecto(self.raiz)
        self.assertIn("tablas", str(capturado.exception))

    def test_fallo_posterior_a_conectar_no_deja_handle_ni_lock(self):
        self.base_declarada_v2_sin_tablas_v2()
        with self.assertRaises(ErrorProyecto):
            abrir_proyecto(self.raiz)
        # En Windows un handle vivo impide borrar; es la prueba directa de la fuga.
        (self.raiz / NOMBRE_BASE).unlink()
        self.assertFalse((self.raiz / NOMBRE_BASE).exists())
        lock = _adquirir_lock(self.raiz)
        self.assertIsNotNone(lock, "el lock debe quedar liberado tras un fallo de apertura")
        lock.close()

    def test_base_sin_las_fks_de_pertenencia_de_su_version_no_devuelve_repositorio(self):
        """Todas las tablas presentes, pero Draft/Variant sin la FK compuesta."""
        self.raiz.mkdir()
        for directorio in DIRECTORIOS_PROYECTO:
            (self.raiz / directorio).mkdir()
        (self.raiz / NOMBRE_MANIFIESTO).write_text(
            json.dumps({"project_id": "p1", "name": "X", "schema_version": 2}), encoding="utf-8")
        (self.raiz / NOMBRE_BASE).touch()
        conexion = _conexion(self.raiz / NOMBRE_BASE)
        _sql_v1(conexion)
        for sentencia in _DDL_V2:
            if "CREATE TABLE Draft " in sentencia:
                sentencia = "CREATE TABLE Draft (id TEXT PRIMARY KEY, moment_id TEXT, current_revision_id TEXT)"
            elif "CREATE TABLE Variant " in sentencia:
                sentencia = ("CREATE TABLE Variant (id TEXT PRIMARY KEY, draft_id TEXT NOT NULL,"
                             " name TEXT NOT NULL, current_revision_id TEXT)")
            conexion.execute(sentencia)
        conexion.execute("PRAGMA user_version=2")
        conexion.execute("INSERT INTO Project VALUES ('p1', 'X', '{}')")
        conexion.commit()
        conexion.close()
        tablas_presentes = _conexion(self.raiz / NOMBRE_BASE)
        try:
            self.assertIn("Draft", {f[0] for f in tablas_presentes.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")})
        finally:
            tablas_presentes.close()
        with self.assertRaises(ErrorProyecto) as capturado:
            abrir_proyecto(self.raiz)
        self.assertIn("validaciones", str(capturado.exception))

    def test_identidad_discordante_entre_manifiesto_y_base_falla_de_forma_accionable(self):
        with crear_proyecto(self.raiz, "Demo"):
            pass
        manifest = json.loads((self.raiz / NOMBRE_MANIFIESTO).read_text("utf-8"))
        manifest["project_id"] = "otro"
        (self.raiz / NOMBRE_MANIFIESTO).write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ErrorProyecto) as capturado:
            abrir_proyecto(self.raiz)
        self.assertIn("coherentes", str(capturado.exception))
        (self.raiz / NOMBRE_BASE).unlink()

    def test_base_ausente_no_se_crea_silenciosamente(self):
        with crear_proyecto(self.raiz, "Demo"):
            pass
        (self.raiz / NOMBRE_BASE).unlink()
        with self.assertRaises(ErrorProyecto):
            abrir_proyecto(self.raiz)
        self.assertFalse((self.raiz / NOMBRE_BASE).exists(), "mode=rw nunca crea la base")

    def test_manifiesto_de_version_futura_se_rechaza(self):
        with crear_proyecto(self.raiz, "Demo"):
            pass
        manifest = json.loads((self.raiz / NOMBRE_MANIFIESTO).read_text("utf-8"))
        manifest["schema_version"] = VERSION_ESQUEMA + 5
        (self.raiz / NOMBRE_MANIFIESTO).write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(ErrorProyecto) as capturado:
            abrir_proyecto(self.raiz)
        self.assertIn("mas reciente", str(capturado.exception))


# --------------------------------------------------------------------------- #
# Integridad del modelo
# --------------------------------------------------------------------------- #

class IntegridadEsquemaTests(BaseProyecto):
    def semilla_revisiones(self, repositorio):
        with repositorio.transaccion() as tx:
            tx.execute("INSERT INTO Draft VALUES ('d1', NULL, NULL)")
            tx.execute("INSERT INTO Draft VALUES ('d2', NULL, NULL)")
            tx.execute("INSERT INTO EditRevision VALUES ('r1', 'd1', '{}', '2026-01-01')")
            tx.execute("INSERT INTO EditRevision VALUES ('r2', 'd2', '{}', '2026-01-01')")

    def test_revision_actual_invalida_o_ajena_se_rechaza_en_insert_y_update(self):
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            repositorio = proyecto.repositorio
            self.semilla_revisiones(repositorio)
            casos = (
                ("INSERT INTO Draft VALUES ('d3', NULL, 'revision-inexistente')", "insert de revision inexistente"),
                ("INSERT INTO Draft VALUES ('d4', NULL, 'r2')", "insert de revision de otro draft"),
                ("INSERT INTO Variant VALUES ('v1', 'd1', 'a', 'r2')", "insert de variante cruzada"),
                ("INSERT INTO Variant VALUES ('v2', 'd1', 'b', 'revision-inexistente')", "insert de variante invalida"),
                ("UPDATE Draft SET current_revision_id='r2' WHERE id='d1'", "update de revision de otro draft"),
            )
            for sentencia, motivo in casos:
                with self.subTest(motivo=motivo):
                    with self.assertRaises(ErrorProyecto, msg=motivo):
                        with repositorio.transaccion() as tx:
                            tx.execute(sentencia)
            # La relacion valida sigue siendo posible por ambos caminos.
            with repositorio.transaccion() as tx:
                tx.execute("INSERT INTO Variant VALUES ('v3', 'd1', 'ok', 'r1')")
                tx.execute("UPDATE Draft SET current_revision_id='r1' WHERE id='d1'")
            conexion = repositorio.conexion
            self.assertEqual(conexion.execute("SELECT COUNT(*) FROM Draft").fetchone()[0], 2)
            self.assertEqual(conexion.execute("SELECT COUNT(*) FROM Variant").fetchone()[0], 1)
            self.assertEqual(conexion.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conexion.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_la_pertenencia_no_puede_romperse_mutando_el_otro_lado_de_la_relacion(self):
        """La invariante es relacional: no basta con vigilar `current_revision_id`."""
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            repositorio = proyecto.repositorio
            self.semilla_revisiones(repositorio)
            with repositorio.transaccion() as tx:
                tx.execute("UPDATE Draft SET current_revision_id='r1' WHERE id='d1'")
                tx.execute("INSERT INTO Variant VALUES ('v1', 'd1', 'a', 'r1')")
            casos = (
                ("UPDATE Variant SET draft_id='d2' WHERE id='v1'", "mover la variante conservando la revision anterior"),
                ("UPDATE Draft SET id='dX' WHERE id='d1'", "renombrar el draft separandolo de su revision"),
                ("UPDATE EditRevision SET draft_id='d2' WHERE id='r1'", "mover la revision apuntada a otro draft"),
                ("UPDATE EditRevision SET id='rX' WHERE id='r1'", "renombrar la revision apuntada"),
                ("DELETE FROM EditRevision WHERE id='r1'", "borrar la revision apuntada"),
            )
            for sentencia, motivo in casos:
                with self.subTest(motivo=motivo):
                    with self.assertRaises(ErrorProyecto, msg=motivo):
                        with repositorio.transaccion() as tx:
                            tx.execute(sentencia)
            conexion = repositorio.conexion
            self.assertEqual(
                conexion.execute("SELECT id, draft_id, current_revision_id FROM Variant").fetchall(),
                [("v1", "d1", "r1")])
            self.assertEqual(conexion.execute("SELECT current_revision_id FROM Draft WHERE id='d1'").fetchone()[0], "r1")
            self.assertEqual(conexion.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conexion.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_las_mutaciones_legitimas_de_la_relacion_siguen_permitidas(self):
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            repositorio = proyecto.repositorio
            self.semilla_revisiones(repositorio)
            with repositorio.transaccion() as tx:
                tx.execute("INSERT INTO Variant VALUES ('v1', 'd1', 'a', NULL)")
                tx.execute("UPDATE Variant SET draft_id='d2' WHERE id='v1'")   # sin revision apuntada
                tx.execute("UPDATE Draft SET current_revision_id='r1' WHERE id='d1'")
                tx.execute("DELETE FROM EditRevision WHERE id='r2'")           # revision no apuntada
                tx.execute("UPDATE Draft SET current_revision_id=NULL WHERE id='d1'")
                tx.execute("DELETE FROM EditRevision WHERE id='r1'")           # ya nadie la apunta
            self.assertEqual(proyecto.repositorio.conexion.execute("SELECT COUNT(*) FROM EditRevision").fetchone()[0], 0)

    def test_una_violacion_de_pertenencia_es_detectable_por_foreign_key_check(self):
        """Un trigger no deja rastro verificable; la FK compuesta si."""
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            self.semilla_revisiones(proyecto.repositorio)
            conexion = proyecto.repositorio.conexion
            conexion.execute("PRAGMA foreign_keys=OFF")
            conexion.execute("UPDATE Draft SET current_revision_id='r2' WHERE id='d1'")
            conexion.commit()
            self.assertNotEqual(conexion.execute("PRAGMA foreign_key_check").fetchall(), [],
                                "el estado prohibido debe ser diagnosticable, no solo irrepresentable")

    def test_la_transaccion_publica_no_filtra_excepciones_de_sqlite(self):
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            with self.assertRaises(ErrorProyecto) as capturado:
                with proyecto.repositorio.transaccion() as tx:
                    tx.execute("INSERT INTO SourceAsset (id, project_id, mode, relative_path, external_path,"
                               " fingerprint, state) VALUES ('s', 'proyecto-inexistente', 'referenced', NULL,"
                               " 'x', 'fp', 'available')")
            self.assertNotIsInstance(capturado.exception, sqlite3.Error)
            self.assertIsInstance(capturado.exception.__cause__, sqlite3.Error)
            self.assertEqual(proyecto.repositorio.conexion.execute("SELECT COUNT(*) FROM SourceAsset").fetchone()[0], 0)

    def test_una_excepcion_propia_del_llamador_atraviesa_la_transaccion(self):
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            with self.assertRaises(ValueError):
                with proyecto.repositorio.transaccion() as tx:
                    # Columnas explicitas: v3 amplia `Job` y un INSERT posicional
                    # dejaria de compilar por una razon ajena a lo que se prueba.
                    tx.execute("INSERT INTO Job (id, kind, state) VALUES ('j', 'k', 'queued')")
                    raise ValueError("decision del llamador")
            self.assertEqual(proyecto.repositorio.conexion.execute("SELECT COUNT(*) FROM Job").fetchone()[0], 0)

    def test_rutas_internas_escapadas_se_rechazan_y_no_se_siguen(self):
        fuera = self.base / "fuera.bin"
        fuera.write_bytes(b"externo")
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            project_id = proyecto.repositorio.informacion().project_id
            with proyecto.repositorio.transaccion() as tx:
                tx.execute("INSERT INTO SourceAsset (id, project_id, mode, relative_path, external_path,"
                           " fingerprint, state) VALUES ('s1', ?, 'copied', '../fuera.bin', NULL, 'fp',"
                           " 'available')", (project_id,))
                tx.execute("INSERT INTO AnalysisArtifact (id, source_id, relative_path, kind, state) VALUES ('a1', 's1', ?, 'proxy', 'available')", (str(fuera),))
            resultado = proyecto.repositorio.reconciliar()
            self.assertEqual((resultado.fuentes_faltantes, resultado.artefactos_reconstruibles), (1, 1),
                             "una ruta que escapa de la raiz no puede considerarse presente")
            self.assertTrue(fuera.is_file())


# --------------------------------------------------------------------------- #
# Fuentes
# --------------------------------------------------------------------------- #

class FuentesTests(BaseProyecto):
    def setUp(self):
        super().setUp()
        self.origen = self.base / "origen.mp4"
        self.origen.write_bytes(b"video")

    def sources(self):
        return sorted(hijo.name for hijo in (self.raiz / "sources").iterdir())

    def test_fuente_copiada_usa_ruta_relativa_del_proyecto(self):
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            fuente_id = proyecto.repositorio.agregar_fuente(str(self.origen), "fp-1", copiar=True)
            fila = proyecto.repositorio.conexion.execute(
                "SELECT mode, relative_path, external_path FROM SourceAsset WHERE id=?", (fuente_id,)).fetchone()
            self.assertEqual(fila[0], "copied")
            self.assertIsNone(fila[2])
            self.assertTrue((self.raiz / fila[1]).is_file())
            self.assertFalse(Path(fila[1]).is_absolute())

    def test_fuente_referenciada_movida_se_relocaliza_solo_con_fingerprint(self):
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            fuente_id = proyecto.repositorio.agregar_fuente(str(self.origen), "fp-1")
            self.origen.unlink()
            self.assertEqual(proyecto.repositorio.reconciliar().fuentes_faltantes, 1)
            nueva = self.base / "nueva.mp4"
            nueva.write_bytes(b"video")
            with self.assertRaises(ErrorFuenteProyecto):
                proyecto.repositorio.relocalizar_fuente(fuente_id, str(nueva), lambda _r: "otro")
            with self.assertRaises(ErrorFuenteProyecto):
                proyecto.repositorio.relocalizar_fuente(fuente_id, str(nueva),
                                                        lambda _r: (_ for _ in ()).throw(RuntimeError("sin probe")))
            proyecto.repositorio.relocalizar_fuente(fuente_id, str(nueva), lambda _r: "fp-1")
            estado = proyecto.repositorio.conexion.execute(
                "SELECT state FROM SourceAsset WHERE id=?", (fuente_id,)).fetchone()[0]
            self.assertEqual(estado, "available")

    def test_fallo_de_io_a_mitad_de_copia_no_deja_temporal_ni_error_crudo(self):
        """El ENOSPC llega *despues* de escribir un `.tmp` parcial: hay basura real que limpiar."""
        escritos = []

        def copia_parcial(_origen, destino, *_argumentos, **_claves):
            Path(destino).write_bytes(b"parcial")  # el temporal existe en disco...
            escritos.append(Path(destino))
            raise OSError(28, "No space left on device")  # ...y recien ahi falla.

        with crear_proyecto(self.raiz, "Demo") as proyecto:
            with mock.patch("shutil.copy2", copia_parcial):
                with self.assertRaises(ErrorFuenteProyecto) as capturado:
                    proyecto.repositorio.agregar_fuente(str(self.origen), "fp-1", copiar=True)
            self.assertEqual(len(escritos), 1)
            self.assertTrue(escritos[0].name.endswith(".tmp"))
            self.assertNotIsInstance(capturado.exception, OSError)
            self.assertEqual(self.sources(), [], "el temporal parcial debe quedar limpiado")
            self.assertEqual(proyecto.repositorio.conexion.execute("SELECT COUNT(*) FROM SourceAsset").fetchone()[0], 0)

    def test_fallo_sql_despues_de_copiar_limpia_el_archivo(self):
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            proyecto.repositorio.conexion.execute(
                "CREATE TRIGGER inyectado BEFORE INSERT ON SourceAsset BEGIN SELECT RAISE(ABORT, 'inyectado'); END")
            proyecto.repositorio.conexion.commit()
            with self.assertRaises(ErrorFuenteProyecto):
                proyecto.repositorio.agregar_fuente(str(self.origen), "fp-1", copiar=True)
            self.assertEqual(self.sources(), [])
            self.assertEqual(proyecto.repositorio.conexion.execute("SELECT COUNT(*) FROM SourceAsset").fetchone()[0], 0)

    def test_fallo_al_resolver_la_identidad_no_deja_copia_huerfana(self):
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            with proyecto.repositorio.transaccion() as tx:
                tx.execute("DELETE FROM Project")
            with self.assertRaises(ErrorProyecto):
                proyecto.repositorio.agregar_fuente(str(self.origen), "fp-1", copiar=True)
            self.assertEqual(self.sources(), [], "la identidad se resuelve antes de tocar el filesystem")

    def test_fuente_sin_fingerprint_o_inexistente_se_rechaza_antes_de_copiar(self):
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            with self.assertRaises(ErrorFuenteProyecto):
                proyecto.repositorio.agregar_fuente(str(self.origen), "", copiar=True)
            with self.assertRaises(ErrorFuenteProyecto):
                proyecto.repositorio.agregar_fuente(str(self.base / "no-existe.mp4"), "fp-1", copiar=True)
            self.assertEqual(self.sources(), [])

    def test_reconciliacion_hace_artefacto_reconstruible_y_conserva_draft(self):
        with crear_proyecto(self.raiz, "Demo") as proyecto:
            conexion = proyecto.repositorio.conexion
            project_id = proyecto.repositorio.informacion().project_id
            with proyecto.repositorio.transaccion() as tx:
                tx.execute("INSERT INTO SourceAsset (id, project_id, mode, relative_path, external_path,"
                           " fingerprint, state) VALUES ('s', ?, 'referenced', NULL, 'existente.mp4', 'fp',"
                           " 'available')", (project_id,))
                tx.execute("INSERT INTO AnalysisArtifact (id, source_id, relative_path, kind, state) VALUES ('a', 's', 'cache/perdido.mp4', 'proxy', 'available')")
                tx.execute("INSERT INTO Moment VALUES ('m', 's', 0, 1, 1)")
                tx.execute("INSERT INTO Draft VALUES ('d', 'm', NULL)")
            resultado = proyecto.repositorio.reconciliar()
            self.assertEqual(resultado.artefactos_reconstruibles, 1)
            self.assertEqual(conexion.execute("SELECT state FROM AnalysisArtifact WHERE id='a'").fetchone()[0], "rebuildable")
            self.assertEqual(conexion.execute("SELECT COUNT(*) FROM Draft").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
