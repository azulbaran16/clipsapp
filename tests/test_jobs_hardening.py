"""Bateria adversarial: identidad de rutas, contrato de etapa y publicacion.

Cada clase reproduce una forma concreta de romper el coordinador que la revision
logica identifico. Las victimas se comprueban byte a byte, y donde el ataque solo
existe de verdad con un proceso de por medio se usa un proceso de verdad.
"""

from contextlib import contextmanager
from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipperkick.application.jobs import Coordinador, RelojSistema  # noqa: E402
from clipperkick.domain.jobs import ArchivoSalida  # noqa: E402
from clipperkick.domain.jobs import (  # noqa: E402
    CODIGO_CONTRATO, ErrorContratoStage, ErrorIdentificadorJob, ErrorJob, ErrorProtocoloWorker,
    ErrorRecursoJob, ErrorValidacionSalida, EstadoJob, Job, ManifiestoSalida, PoliticaCancelacion,
    PoliticaRetry, Recursos, StageDefinition, conciliar_con_stage, es_componente_interno,
    exigir_componente_interno, exigir_ruta_interna, recursos_efectivos,
)
from clipperkick.infrastructure.jobs import (  # noqa: E402
    AlmacenArtefactosProyecto, crear_coordinador,
)
from clipperkick.infrastructure.jobs.artifacts import (  # noqa: E402
    FORMATO_PUBLICACION, NOMBRE_PUBLICACION,
)
from clipperkick.infrastructure.jobs.protocol import codificar, decodificar  # noqa: E402
from clipperkick.infrastructure.project import abrir_proyecto, crear_proyecto  # noqa: E402


SRC = str(Path(__file__).resolve().parents[1] / "src")
ENTORNO = dict(os.environ, PYTHONPATH=SRC, PYTHONDONTWRITEBYTECODE="1")
LIMITE = 90.0

STAGE = StageDefinition(
    nombre="prueba", version_contrato="1", recursos=Recursos(cpu=1), soporta_checkpoint=True,
    politica_retry=PoliticaRetry(max_intentos=2, codigos_transitorios=frozenset({"transitorio.io"})),
    politica_cancelacion=PoliticaCancelacion(timeout_cooperativo=0.4))
CATALOGO = {STAGE.nombre: STAGE}

#: Vectores que deben rechazarse *en cualquier sistema*: un proyecto viaja entre
#: Windows y POSIX, y lo que aqui es un nombre inocente alla es una ruta.
IDENTIFICADORES_HOSTILES = (
    "..", ".", "../victima", "..\\victima", "a/b", "a\\b", "/etc/passwd",
    "C:/Windows", "C:\\Windows", "\\\\servidor\\recurso", "unidad:flujo",
    "con", "CON", "NUL", "COM1", "LPT9.txt", "termina.", "termina ", "nulo\x00byte",
    "salto\nlinea", "", 42, None,
)


class BaseProyecto(unittest.TestCase):
    def setUp(self):
        self.temporal = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporal.cleanup)
        self.base = Path(self.temporal.name)
        self.raiz = self.base / "demo.clipsapp"
        crear_proyecto(self.raiz, "Demo").close()
        self.victima = self.base / "victima"
        self.victima.mkdir()
        self.documento = self.victima / "documento.txt"
        self.documento.write_bytes(b"no tocar")

    def victima_intacta(self):
        return self.documento.is_file() and self.documento.read_bytes() == b"no tocar"

    @contextmanager
    def sesion(self, catalogo=None, **claves):
        proyecto = abrir_proyecto(self.raiz)
        coordinador = crear_coordinador(proyecto, catalogo=catalogo or CATALOGO, **claves)
        self.abierto = proyecto
        try:
            yield coordinador
        finally:
            self.abierto = None
            coordinador.cerrar()
            proyecto.close()

    @property
    def conexion(self):
        return self.abierto.repositorio.conexion


# --------------------------------------------------------------------------- #
# Identidad: un id es un nombre, nunca una ruta
# --------------------------------------------------------------------------- #

class IdentificadoresTests(unittest.TestCase):
    def test_todo_vector_hostil_se_rechaza_con_error_tipado(self):
        for valor in IDENTIFICADORES_HOSTILES:
            with self.subTest(valor=valor):
                with self.assertRaises(ErrorIdentificadorJob):
                    exigir_componente_interno(valor)
                self.assertFalse(es_componente_interno(valor))

    def test_los_nombres_legitimos_siguen_pasando(self):
        for valor in ("j1", "ingest", "a-b_c.2", "trabajo 1", "ácentós", "CONtenido", "COM10"):
            with self.subTest(valor=valor):
                self.assertEqual(exigir_componente_interno(valor), valor)

    def test_una_ruta_de_manifiesto_se_juzga_con_la_gramatica_de_los_dos_sistemas(self):
        """En POSIX `Path` aceptaria la sintaxis de Windows como nombre relativo."""
        for valor in ("../fuera", "..\\fuera", "/absoluta", "C:/absoluta", "C:\\absoluta",
                      "\\\\servidor\\recurso", "a//b", "a\\\\b", "a/../b", "a/./b", ""):
            with self.subTest(valor=valor):
                with self.assertRaises(ErrorIdentificadorJob):
                    exigir_ruta_interna(valor)

    def test_una_ruta_interna_legitima_se_descompone_igual_en_ambos_sistemas(self):
        self.assertEqual(exigir_ruta_interna("a/b/c.txt"), ("a", "b", "c.txt"))
        self.assertEqual(exigir_ruta_interna("a\\b\\c.txt"), ("a", "b", "c.txt"))

    def test_un_job_no_puede_existir_con_un_id_que_sea_una_ruta(self):
        for valor in ("../victima", "C:\\Windows", "a/b", ".."):
            with self.subTest(valor=valor):
                with self.assertRaises(ErrorIdentificadorJob):
                    Job(id=valor, stage="prueba", version_contrato="1")


class AlmacenHostilTests(BaseProyecto):
    def almacen(self):
        return AlmacenArtefactosProyecto(self.raiz)

    def test_preparar_no_crea_ni_borra_nada_fuera_del_proyecto(self):
        almacen = self.almacen()
        for job_id in ("../../..", "../../victima", "C:/Windows/Temp/inyectado", "..\\..\\victima"):
            with self.subTest(job_id=job_id):
                with self.assertRaises(ErrorIdentificadorJob):
                    almacen.preparar(job_id, "intento")
                with self.assertRaises(ErrorIdentificadorJob):
                    almacen.preparar("job", job_id)
                self.assertTrue(self.victima_intacta(), f"{job_id} alcanzo a la victima")
                self.assertTrue(self.victima.is_dir())

    def test_el_destino_hostil_ni_siquiera_llega_a_crearse(self):
        with self.assertRaises(ErrorIdentificadorJob):
            self.almacen().preparar("../../colateral", "intento")
        self.assertFalse((self.base / "colateral").exists(),
                         "la validacion ocurre antes que cualquier efecto")

    def test_publicar_y_cuarentena_tampoco_aceptan_rutas(self):
        almacen = self.almacen()
        manifiesto = ManifiestoSalida()
        with self.assertRaises(ErrorIdentificadorJob):
            almacen.publicar("../../victima", str(self.raiz / "cache"), manifiesto)
        self.assertTrue(self.victima_intacta())

    def test_un_manifiesto_con_ruta_hostil_no_valida_ni_publica_lo_ajeno(self):
        """Sin esta frontera, publicar *moveria* el archivo de la victima.

        El worker esta aislado, pero el manifiesto es un documento que el
        coordinador interpreta en su propio proceso: si acepta `..` o una ruta
        con sintaxis del otro sistema, el que saca los bytes de su sitio es el
        coordinador, no el worker.
        """
        from clipperkick.domain.jobs import ArchivoSalida

        almacen = self.almacen()
        trabajo = self.raiz / "cache" / "jobs" / "j1" / "intento"
        trabajo.mkdir(parents=True)
        tamano = len(b"no tocar")
        hostiles = ("../../victima/documento.txt", "..\\..\\victima\\documento.txt",
                    str(self.documento), "/etc/passwd", "C:\\Windows\\win.ini")
        for ruta in hostiles:
            with self.subTest(ruta=ruta):
                manifiesto = ManifiestoSalida((ArchivoSalida(ruta, tamano),), {})
                with self.assertRaises(ErrorValidacionSalida):
                    almacen.validar(str(trabajo), manifiesto)
                with self.assertRaises(ErrorValidacionSalida):
                    almacen.publicar("j1", str(trabajo), manifiesto, clave="k")
                self.assertTrue(self.victima_intacta(), f"{ruta} movio o leyo lo ajeno")
                self.assertFalse((self.raiz / "artifacts" / "j1").exists())

    def test_un_enlace_que_saca_del_proyecto_se_rechaza(self):
        enlace = self.raiz / "cache" / "jobs"
        enlace.parent.mkdir(parents=True, exist_ok=True)
        try:
            enlace.symlink_to(self.victima, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest(f"este sistema no permite crear enlaces simbolicos: {error}")
        with self.assertRaises(ErrorIdentificadorJob):
            self.almacen().preparar("j1", "intento")
        self.assertTrue(self.victima_intacta())

    def test_el_coordinador_no_deja_encolar_un_id_hostil(self):
        with self.sesion() as coordinador:
            for valor in ("../victima", "C:\\Windows", "a/b"):
                with self.subTest(valor=valor):
                    with self.assertRaises(ErrorIdentificadorJob):
                        coordinador.encolar(Job(id=valor, stage="prueba", version_contrato="1"))
            self.assertEqual(coordinador.jobs(), ())
            self.assertTrue(self.victima_intacta())

    def test_un_id_hostil_persistido_no_llega_a_tocar_el_disco(self):
        """Una fila escrita por fuera tampoco puede convertirse en ruta.

        La fila se inserta a mano porque ninguna API del coordinador la
        aceptaria: es el escenario de una base manipulada o escrita por otra
        herramienta, y el modelo debe negarse a representarla.
        """
        with self.sesion() as coordinador:
            self.conexion.execute(
                "INSERT INTO Job (id, kind, state, stage, contract_version, resources_json,"
                " max_attempts) VALUES (?, 'prueba', 'queued', 'prueba', '1',"
                """ '{"cpu": 1, "gpu": 0, "disk": 0, "network": 0}', 1)""",
                ("../../victima",))
            self.conexion.commit()
            with self.assertRaises(ErrorIdentificadorJob):
                coordinador.paso()
            self.assertTrue(self.victima_intacta())
            self.assertFalse((self.raiz / "cache" / "jobs").exists(),
                             "ni siquiera se llego a preparar un staging")


# --------------------------------------------------------------------------- #
# Confinamiento del stage: el worker escribe donde se le dice y solo ahi
# --------------------------------------------------------------------------- #

class ConfinamientoStageTests(BaseProyecto):
    def correr(self, coordinador):
        self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE))

    def test_una_salida_con_ruta_hostil_no_escribe_fuera_del_proyecto(self):
        """El aislamiento por proceso no basta si el contexto deja componer `..`."""
        for nombre in ("../../victima/documento.txt", "..\\..\\victima\\documento.txt",
                       "/tmp/inyectado.txt", "C:/Windows/Temp/inyectado.txt"):
            with self.subTest(nombre=nombre), self.sesion() as coordinador:
                identificador = f"j{abs(hash(nombre))}"
                coordinador.encolar(Job(id=identificador, stage="prueba", version_contrato="1",
                                        payload={"pasos": 1, "nombre_salida": nombre}))
                self.correr(coordinador)
                job = coordinador.job(identificador)
                self.assertEqual(job.estado, EstadoJob.FAILED)
                self.assertTrue(self.victima_intacta(), f"{nombre} alcanzo a la victima")
                self.assertFalse((self.raiz / "artifacts" / identificador).exists())

    def test_el_marcador_entre_intentos_vive_dentro_del_proyecto(self):
        for nombre in ("../../victima/documento.txt", "/tmp/marcador", "C:/Windows/Temp/marcador"):
            with self.subTest(nombre=nombre), self.sesion() as coordinador:
                identificador = f"m{abs(hash(nombre))}"
                coordinador.encolar(Job(id=identificador, stage="prueba", version_contrato="1",
                                        payload={"pasos": 1, "marcador_transitorio": nombre}))
                self.correr(coordinador)
                self.assertEqual(coordinador.job(identificador).estado, EstadoJob.FAILED)
                self.assertTrue(self.victima_intacta())

    def test_un_marcador_legitimo_si_sobrevive_entre_intentos(self):
        with self.sesion() as coordinador:
            coordinador.encolar(Job(id="j1", stage="prueba", version_contrato="1", max_intentos=2,
                                    payload={"pasos": 1, "marcador_transitorio": "testigo"}))
            self.correr(coordinador)
            self.assertEqual(coordinador.job("j1").estado, EstadoJob.SUCCEEDED)
            self.assertTrue((self.raiz / "cache" / "jobs" / "j1" / "_estado" / "testigo").is_file())


# --------------------------------------------------------------------------- #
# La etapa gobierna al job
# --------------------------------------------------------------------------- #

class ContratoStageTests(BaseProyecto):
    def test_la_conciliacion_aplica_techo_de_retry_y_piso_de_recursos(self):
        stage = StageDefinition(nombre="prueba", version_contrato="1", recursos=Recursos(cpu=2),
                                politica_retry=PoliticaRetry(max_intentos=2))
        conciliado = conciliar_con_stage(
            Job(id="j", stage="prueba", version_contrato="1", max_intentos=100,
                recursos=Recursos(gpu=1)), stage)
        self.assertEqual(conciliado.max_intentos, 2, "el caller no compra reintentos")
        self.assertEqual(conciliado.recursos, Recursos(cpu=2, gpu=1),
                         "el contrato es piso por clase; declarar mas sigue permitido")
        self.assertEqual(recursos_efectivos(Recursos(), Recursos(cpu=1)), Recursos(cpu=1))
        self.assertEqual(conciliar_con_stage(
            Job(id="j", stage="prueba", version_contrato="1", max_intentos=1), stage).max_intentos,
            1, "pedir menos reintentos que la etapa es legitimo")

    def test_una_version_de_contrato_distinta_no_se_concilia(self):
        stage = StageDefinition(nombre="prueba", version_contrato="2")
        with self.assertRaises(ErrorContratoStage):
            conciliar_con_stage(Job(id="j", stage="prueba", version_contrato="1"), stage)

    def test_encolar_con_version_incompatible_se_rechaza(self):
        with self.sesion() as coordinador:
            with self.assertRaises(ErrorContratoStage):
                coordinador.encolar(Job(id="j1", stage="prueba", version_contrato="9"))
            self.assertEqual(coordinador.jobs(), ())

    def test_un_job_fijado_a_otra_version_ni_lanza_worker_ni_publica(self):
        """La etapa se actualizo entre sesiones; el job pinneado debe pararse."""
        with self.sesion() as coordinador:
            coordinador.encolar(Job(id="j1", stage="prueba", version_contrato="1",
                                    payload={"pasos": 1}))
        nueva = StageDefinition(nombre="prueba", version_contrato="2", recursos=Recursos(cpu=1))
        with self.sesion(catalogo={"prueba": nueva}) as coordinador:
            self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE))
            job = coordinador.job("j1")
            self.assertEqual((job.estado, job.error_codigo, job.intentos_usados),
                             (EstadoJob.FAILED, CODIGO_CONTRATO, 0))
            self.assertEqual(coordinador.intentos("j1"), (), "no se lanzo ningun worker")
            self.assertFalse((self.raiz / "artifacts" / "j1").exists())

    def test_un_max_intentos_persistido_por_encima_de_la_politica_no_compra_reintentos(self):
        with self.sesion() as coordinador:
            coordinador.encolar(Job(id="j1", stage="prueba", version_contrato="1",
                                    max_intentos=100,
                                    payload={"pasos": 1, "error_en": 0,
                                             "codigo": "transitorio.io"}))
            self.conexion.execute("UPDATE Job SET max_attempts=100 WHERE id='j1'")
            self.conexion.commit()
            self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE))
            job = coordinador.job("j1")
            self.assertEqual(job.estado, EstadoJob.FAILED)
            self.assertEqual(len(coordinador.intentos("j1")), STAGE.politica_retry.max_intentos,
                             "el techo de reintentos lo pone la etapa, no la fila")

    def test_un_peso_subdeclarado_en_disco_no_elude_la_capacidad(self):
        """Con `cpu=0` persistido, dos jobs correrian a la vez pese al limite."""
        with self.sesion(limites=Recursos(cpu=1)) as coordinador:
            for identificador in ("a", "b"):
                coordinador.encolar(Job(id=identificador, stage="prueba", version_contrato="1",
                                        payload={"pasos": 4, "retardo": 0.05}))
            self.conexion.execute(
                """UPDATE Job SET resources_json='{"cpu": 0, "gpu": 0, "disk": 0, "network": 0}'""")
            self.conexion.commit()
            maxima = 0
            limite = time.monotonic() + LIMITE
            while coordinador.paso():
                maxima = max(maxima, len(coordinador.resumen().en_curso))
                self.assertLess(time.monotonic(), limite)
                time.sleep(0.005)
            self.assertEqual(maxima, 1, "el peso del contrato manda sobre el de la fila")
            for identificador in ("a", "b"):
                self.assertEqual(coordinador.job(identificador).estado, EstadoJob.SUCCEEDED)


# --------------------------------------------------------------------------- #
# Ventana entre publicar y confirmar
# --------------------------------------------------------------------------- #

# Publica de verdad y muere justo antes de confirmar en SQLite: el rename ya
# ocurrio, la transaccion no. Es la ventana exacta que el protocolo debe cerrar.
HIJO_MUERE_TRAS_PUBLICAR = """
import os, sys, time
from clipperkick.domain.jobs import EstadoJob, Job, PoliticaCancelacion, PoliticaRetry, Recursos, StageDefinition
from clipperkick.infrastructure.jobs import crear_coordinador
from clipperkick.infrastructure.jobs.repository import RepositorioSqliteJobs
from clipperkick.infrastructure.project import abrir_proyecto

raiz, job_id = sys.argv[1], sys.argv[2]
stage = StageDefinition(nombre="prueba", version_contrato="1", recursos=Recursos(cpu=1),
                        politica_retry=PoliticaRetry(max_intentos=2))

original = RepositorioSqliteJobs.finalizar
def morir_antes_de_confirmar(self, attempt_id, propietario, destino, ahora, **claves):
    if destino is EstadoJob.SUCCEEDED:
        print("PUBLICADO", flush=True)
        os._exit(23)
    return original(self, attempt_id, propietario, destino, ahora, **claves)
RepositorioSqliteJobs.finalizar = morir_antes_de_confirmar

proyecto = abrir_proyecto(raiz)
coordinador = crear_coordinador(proyecto, catalogo={"prueba": stage})
coordinador.encolar(Job(id=job_id, stage="prueba", version_contrato="1", max_intentos=2,
                        clave_materializacion="clave-1", payload={"pasos": 2}))
coordinador.ejecutar(limite_segundos=60)
print("NO_MURIO", flush=True)
os._exit(1)
"""


class PublicacionInterrumpidaTests(BaseProyecto):
    def publicacion(self, job_id):
        return json.loads((self.raiz / "artifacts" / job_id / NOMBRE_PUBLICACION)
                          .read_text(encoding="utf-8"))

    def matar_tras_publicar(self, job_id):
        proceso = subprocess.run(
            [sys.executable, "-c", HIJO_MUERE_TRAS_PUBLICAR, str(self.raiz), job_id],
            stdout=subprocess.PIPE, text=True, env=ENTORNO, timeout=LIMITE)
        self.assertEqual((proceso.returncode, proceso.stdout.strip()), (23, "PUBLICADO"))

    def test_un_corte_entre_publicar_y_confirmar_se_adopta_al_reabrir(self):
        self.matar_tras_publicar("j1")
        artefacto = self.raiz / "artifacts" / "j1" / "salida.txt"
        self.assertTrue(artefacto.is_file(), "el fixture no llego a publicar")
        self.assertEqual(self.publicacion("j1")["formato"], FORMATO_PUBLICACION)
        contenido = artefacto.read_bytes()

        with self.sesion() as coordinador:
            self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE))
            job = coordinador.job("j1")
            self.assertEqual((job.estado, job.error_codigo), (EstadoJob.SUCCEEDED, None))
            self.assertEqual(artefacto.read_bytes(), contenido,
                             "adoptar no puede reescribir lo ya publicado")
            filas = self.conexion.execute(
                "SELECT relative_path FROM AnalysisArtifact").fetchall()
            self.assertEqual(filas, [("artifacts/j1/salida.txt",)],
                             "ni artefactos duplicados ni ausentes")

    def test_un_fallo_de_sql_al_confirmar_deja_el_trabajo_recuperable(self):
        """No se marca `failed` sobre un resultado que existe y esta identificado."""
        with self.sesion() as coordinador:
            coordinador.encolar(Job(id="j1", stage="prueba", version_contrato="1", max_intentos=2,
                                    clave_materializacion="clave-1", payload={"pasos": 1}))
            self.conexion.execute("CREATE TRIGGER inyectado BEFORE INSERT ON AnalysisArtifact"
                                  " BEGIN SELECT RAISE(ABORT, 'inyectado'); END")
            self.conexion.commit()
            coordinador.ejecutar(limite_segundos=LIMITE)
            job = coordinador.job("j1")
            self.assertEqual(job.estado, EstadoJob.VALIDATING)
            self.assertIn("publicacion.sin_confirmar",
                          [evento.clave for evento in coordinador.eventos("j1")])
            self.assertTrue((self.raiz / "artifacts" / "j1" / "salida.txt").is_file())
            self.conexion.execute("DROP TRIGGER inyectado")
            self.conexion.commit()

        with self.sesion() as coordinador:
            self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE))
            self.assertEqual(coordinador.job("j1").estado, EstadoJob.SUCCEEDED)
            self.assertEqual(self.conexion.execute(
                "SELECT COUNT(*) FROM AnalysisArtifact").fetchone()[0], 1)

    def test_una_publicacion_ajena_no_se_pisa_y_se_conserva(self):
        almacen = AlmacenArtefactosProyecto(self.raiz)
        destino = self.raiz / "artifacts" / "j1"
        destino.mkdir(parents=True)
        (destino / "salida.txt").write_bytes(b"contenido de otro")
        (destino / NOMBRE_PUBLICACION).write_text(json.dumps({
            "formato": FORMATO_PUBLICACION, "job_id": "j1", "attempt_id": "otro",
            "clave_materializacion": "otra-clave", "archivos": [["salida.txt", 17]],
            "metadata": {}}), encoding="utf-8")

        trabajo = self.raiz / "cache" / "jobs" / "j1" / "intento"
        trabajo.mkdir(parents=True)
        (trabajo / "salida.txt").write_bytes(b"lo mio")
        from clipperkick.domain.jobs import ArchivoSalida
        manifiesto = ManifiestoSalida((ArchivoSalida("salida.txt", 6),), {})
        with self.assertRaises(ErrorValidacionSalida):
            almacen.publicar("j1", str(trabajo), manifiesto, attempt_id="mio", clave="clave-1")
        self.assertEqual((destino / "salida.txt").read_bytes(), b"contenido de otro",
                         "la publicacion previa se conserva para diagnostico")

    def test_una_publicacion_con_otra_metadata_no_se_adopta(self):
        """Mismos bytes, otra descripcion: adoptar registraria una mentira.

        La metadata dice *que son* esos bytes —tipo de artefacto, desde que
        checkpoint se reanudo—. Aceptar una publicacion cuya identidad de archivos
        coincide pero cuya descripcion no, dejaria el proyecto afirmando algo que
        nadie produjo.
        """
        almacen = AlmacenArtefactosProyecto(self.raiz)
        destino = self.raiz / "artifacts" / "j1"
        # La publicacion previa se hace con el propio almacen: escribir el
        # sidecar a mano ataria la prueba al formato del momento en que se
        # escribio, y lo que interesa comprobar es la regla de adopcion.
        primero = self.raiz / "cache" / "jobs" / "j1" / "primero"
        primero.mkdir(parents=True)
        (primero / "salida.txt").write_bytes(b"seis!!")
        almacen.publicar("j1", str(primero),
                         ManifiestoSalida((ArchivoSalida("salida.txt", 6),),
                                          {"kind": "prueba", "reanudado_desde": 0}),
                         attempt_id="primero", clave="clave-1")

        trabajo = self.raiz / "cache" / "jobs" / "j1" / "segundo"
        trabajo.mkdir(parents=True)
        (trabajo / "salida.txt").write_bytes(b"seis!!")
        distinta = ManifiestoSalida((ArchivoSalida("salida.txt", 6),),
                                    {"kind": "prueba", "reanudado_desde": 3})
        with self.assertRaises(ErrorValidacionSalida):
            almacen.publicar("j1", str(trabajo), distinta, attempt_id="segundo", clave="clave-1")
        self.assertEqual(json.loads((destino / NOMBRE_PUBLICACION).read_text("utf-8"))["metadata"],
                         {"kind": "prueba", "reanudado_desde": 0},
                         "la identidad original se conserva intacta")

        # La misma metadata si se adopta: es el caso de la ventana publicar->confirmar.
        igual = ManifiestoSalida((ArchivoSalida("salida.txt", 6),),
                                 {"kind": "prueba", "reanudado_desde": 0})
        self.assertEqual(
            almacen.publicar("j1", str(trabajo), igual, attempt_id="segundo", clave="clave-1"),
            ("artifacts/j1/salida.txt",))

    def test_una_publicacion_sin_identidad_no_se_adopta(self):
        almacen = AlmacenArtefactosProyecto(self.raiz)
        destino = self.raiz / "artifacts" / "j1"
        destino.mkdir(parents=True)
        (destino / "salida.txt").write_bytes(b"huerfano")
        trabajo = self.raiz / "cache" / "jobs" / "j1" / "intento"
        trabajo.mkdir(parents=True)
        (trabajo / "salida.txt").write_bytes(b"huerfano")
        from clipperkick.domain.jobs import ArchivoSalida
        manifiesto = ManifiestoSalida((ArchivoSalida("salida.txt", 8),), {})
        with self.assertRaises(ErrorValidacionSalida):
            almacen.publicar("j1", str(trabajo), manifiesto, clave="clave-1")
        self.assertEqual((destino / "salida.txt").read_bytes(), b"huerfano")

    def test_una_salida_no_puede_llamarse_como_el_sidecar(self):
        almacen = AlmacenArtefactosProyecto(self.raiz)
        trabajo = self.raiz / "cache" / "jobs" / "j1" / "intento"
        trabajo.mkdir(parents=True)
        (trabajo / NOMBRE_PUBLICACION).write_bytes(b"{}")
        from clipperkick.domain.jobs import ArchivoSalida
        with self.assertRaises(ErrorValidacionSalida):
            almacen.validar(str(trabajo), ManifiestoSalida((ArchivoSalida(NOMBRE_PUBLICACION, 2),)))


# --------------------------------------------------------------------------- #
# Configuracion que dejaria jobs atascados
# --------------------------------------------------------------------------- #

class ConfiguracionTests(unittest.TestCase):
    def test_un_timeout_de_cancelacion_no_finito_se_rechaza(self):
        """`NaN` no vence nunca: la terminacion forzada no llegaria jamas."""
        for invalido in (float("nan"), float("inf"), -1, True, "1", None):
            with self.subTest(invalido=invalido):
                with self.assertRaises(ErrorJob):
                    PoliticaCancelacion(timeout_cooperativo=invalido)

    def test_contadores_de_job_invalidos_se_rechazan_al_construir(self):
        for claves in ({"max_intentos": True}, {"max_intentos": 1.5}, {"max_intentos": 0},
                       {"intentos_usados": -1}, {"interrupciones_usadas": -1},
                       {"max_interrupciones": -1}, {"max_interrupciones": True},
                       {"prioridad": "alta"}):
            with self.subTest(claves=claves):
                with self.assertRaises(ErrorJob):
                    Job(id="j", stage="prueba", version_contrato="1", **claves)

    def test_una_politica_de_retry_no_entera_se_rechaza(self):
        for invalido in (True, 1.5, "3"):
            with self.subTest(invalido=invalido):
                with self.assertRaises(ErrorJob):
                    PoliticaRetry(max_intentos=invalido)

    def test_un_lease_no_positivo_o_no_finito_se_rechaza_al_construir(self):
        for invalido in (0, -1, float("nan"), float("inf"), True, "30"):
            with self.subTest(invalido=invalido):
                with self.assertRaises(ErrorJob):
                    Coordinador(None, None, None, {}, duracion_lease=invalido)

    def test_un_limite_de_ejecucion_invalido_no_despacha_nada_antes_de_fallar(self):
        registro = []

        class EjecutorEspia:
            def lanzar(self, solicitud):
                registro.append(solicitud)
                raise AssertionError("no debio lanzarse nada")

        class RepositorioVacio:
            def listar(self):
                registro.append("listar")
                return ()

        coordinador = Coordinador(RepositorioVacio(), EjecutorEspia(), None, {})
        for invalido in (-1, float("nan"), float("inf"), True, "10"):
            with self.subTest(invalido=invalido):
                with self.assertRaises(ErrorJob):
                    coordinador.ejecutar(limite_segundos=invalido)
        self.assertEqual(registro, [], "un limite invalido no puede tener efectos previos")

    def test_pesos_de_recurso_invalidos_se_rechazan(self):
        for invalido in ({"cpu": True}, {"gpu": -1}, {"disk": 1.5}, {"network": "1"}):
            with self.subTest(invalido=invalido):
                with self.assertRaises(ErrorRecursoJob):
                    Recursos(**invalido)


# --------------------------------------------------------------------------- #
# Protocolo: ninguna forma invalida cruza como valor
# --------------------------------------------------------------------------- #

class ProtocoloEstrictoTests(unittest.TestCase):
    def linea(self, **campos):
        base = {"protocolo": "clipsapp-worker/1", "tipo": "progreso"}
        base.update(campos)
        return json.dumps(base).encode("utf-8")

    def test_booleanos_y_no_finitos_no_pasan_por_numeros(self):
        casos = (
            {"unidades_hechas": True}, {"unidades_totales": False},
            {"tipo": "inicio", "pid": True}, {"tipo": "inicio", "pid": 1.5},
            {"tipo": "inicio", "pid": "1234"},
            {"unidades_hechas": "3"}, {"unidades_hechas": None, "unidades_totales": []},
        )
        for campos in casos:
            with self.subTest(campos=campos):
                with self.assertRaises(ErrorProtocoloWorker):
                    decodificar(self.linea(**campos))

    def test_nan_e_infinito_se_rechazan_aunque_json_los_admita(self):
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                crudo = ('{"protocolo": "clipsapp-worker/1", "tipo": "progreso",'
                         f' "unidades_hechas": {literal}}}').encode("utf-8")
                with self.assertRaises(ErrorProtocoloWorker):
                    decodificar(crudo)

    def test_campos_de_texto_que_no_son_texto_se_rechazan(self):
        for campos in ({"clave": 5}, {"tipo": "error", "codigo": 7},
                       {"tipo": "error", "detalle": {"a": 1}}):
            with self.subTest(campos=campos):
                with self.assertRaises(ErrorProtocoloWorker):
                    decodificar(self.linea(**campos))

    def test_un_tamano_booleano_no_declara_un_byte(self):
        crudo = json.dumps({
            "protocolo": "clipsapp-worker/1", "tipo": "resultado",
            "manifiesto": {"archivos": [{"ruta": "a.txt", "bytes": True}], "metadata": {}},
        }).encode("utf-8")
        with self.assertRaises(ErrorProtocoloWorker):
            decodificar(crudo)

    def test_una_ruta_que_no_es_texto_se_rechaza(self):
        crudo = json.dumps({
            "protocolo": "clipsapp-worker/1", "tipo": "resultado",
            "manifiesto": {"archivos": [{"ruta": ["a"], "bytes": 1}], "metadata": {}},
        }).encode("utf-8")
        with self.assertRaises(ErrorProtocoloWorker):
            decodificar(crudo)

    def test_lo_valido_sigue_atravesando_intacto(self):
        from clipperkick.domain.jobs import ArchivoSalida, MensajeWorker, TipoMensaje

        mensaje = MensajeWorker(tipo=TipoMensaje.RESULTADO, clave="k",
                                manifiesto=ManifiestoSalida((ArchivoSalida("a.txt", 0),), {"k": 1}))
        self.assertEqual(decodificar(codificar(mensaje)), mensaje)


if __name__ == "__main__":
    unittest.main()
