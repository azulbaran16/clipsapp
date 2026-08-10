"""Ciclo de vida del worker, orden del protocolo y durabilidad observable.

Aqui viven las pruebas de los bordes que solo existen cuando hay un proceso de
por medio (arranque, muerte del padre, huerfanos) y las del orden del protocolo,
que se ejercitan con dobles porque lo que se prueba es la maquina de estados del
coordinador, no la velocidad de un interprete.
"""

from contextlib import contextmanager
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipperkick.application.jobs import Coordinador, SolicitudEjecucion  # noqa: E402
from clipperkick.domain.jobs import (  # noqa: E402
    CODIGO_ARRANQUE, CODIGO_CANCELADO, CODIGO_DEPENDENCIA_INVALIDA, CODIGO_PRESUPUESTO,
    CODIGO_PROTOCOLO, CODIGO_SALIDA_INVALIDA, CODIGO_TIMEOUT_CANCELACION, ErrorIdentificadorJob,
    ErrorJob, EstadoJob, Job, ManifiestoSalida, MensajeWorker, PoliticaCancelacion, PoliticaRetry,
    Recursos, StageDefinition, TipoMensaje,
)
from clipperkick.infrastructure.jobs import (  # noqa: E402
    AlmacenArtefactosProyecto, RepositorioSqliteJobs, crear_coordinador,
)
from clipperkick.infrastructure.project import abrir_proyecto, crear_proyecto  # noqa: E402


SRC = str(Path(__file__).resolve().parents[1] / "src")
ENTORNO = dict(os.environ, PYTHONPATH=SRC, PYTHONDONTWRITEBYTECODE="1")
LIMITE = 90.0

STAGE = StageDefinition(
    nombre="prueba", version_contrato="1", salidas=("salida.txt",), recursos=Recursos(cpu=1),
    soporta_checkpoint=True,
    politica_retry=PoliticaRetry(max_intentos=2, codigos_transitorios=frozenset({"transitorio.io"})),
    politica_cancelacion=PoliticaCancelacion(timeout_cooperativo=0.4, timeout_arranque=30.0))
CATALOGO = {STAGE.nombre: STAGE}


def proceso_vivo(pid: int) -> bool:
    """Liveness portable, sin dependencias externas."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes

    ACCESO, STILL_ACTIVE = 0x1000, 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(ACCESO, False, pid)
    if not handle:
        return False
    try:
        codigo = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(codigo)):
            return False
        return codigo.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


# Coordinador que arranca un job muy largo y muere de golpe dejando vivo al worker.
HIJO_ABANDONA_WORKER = """
import os, sys, time
from clipperkick.domain.jobs import Job, PoliticaCancelacion, PoliticaRetry, Recursos, StageDefinition
from clipperkick.application.jobs import Coordinador
from clipperkick.infrastructure.jobs import AlmacenArtefactosProyecto, RepositorioSqliteJobs
from clipperkick.infrastructure.jobs.process import MODULO_WORKER, EjecutorSubproceso
from clipperkick.infrastructure.project import abrir_proyecto

raiz, job_id = sys.argv[1], sys.argv[2]
stage = StageDefinition(nombre="prueba", version_contrato="1", soporta_checkpoint=True,
                        recursos=Recursos(cpu=1), politica_retry=PoliticaRetry(max_intentos=3))
proyecto = abrir_proyecto(raiz)
# El modulo del worker es parametrizable para que una mutacion pueda alcanzar al
# nieto: el coordinador que lo lanza vive en *otro* interprete, y parchear el
# proceso de la prueba no cambiaria nada de lo que hace el worker real.
coordinador = Coordinador(
    RepositorioSqliteJobs(proyecto.repositorio),
    EjecutorSubproceso(modulo=os.environ.get("WORKER_MODULE", MODULO_WORKER)),
    AlmacenArtefactosProyecto(proyecto.repositorio.raiz), {"prueba": stage})
coordinador.recuperar()
coordinador.encolar(Job(id=job_id, stage="prueba", version_contrato="1", max_intentos=3,
                        payload={"pasos": 4000, "retardo": 0.05}))
limite = time.monotonic() + 60
while time.monotonic() < limite:
    coordinador.paso()
    intentos = coordinador.intentos(job_id)
    if intentos and intentos[0].pid:
        print(intentos[0].pid, flush=True)
        os._exit(17)
    time.sleep(0.01)
print("SIN_PID", flush=True)
os._exit(1)
"""


class BaseCiclo(unittest.TestCase):
    def setUp(self):
        self.temporal = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporal.cleanup)
        self.base = Path(self.temporal.name)
        self.raiz = self.base / "demo.clipsapp"
        crear_proyecto(self.raiz, "Demo").close()

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

    def job(self, identificador, **claves):
        payload = claves.pop("payload", {})
        return Job(id=identificador, stage="prueba", version_contrato="1", payload=payload,
                   **claves)


# --------------------------------------------------------------------------- #
# (1) El worker no sobrevive a su coordinador
# --------------------------------------------------------------------------- #

class HuerfanoTests(BaseCiclo):
    def test_el_worker_muere_con_su_coordinador_antes_de_reprogramar(self):
        """Un huerfano vivo competiria con el intento que la reapertura crea.

        El coordinador hijo muere sin cerrar nada. El worker debe darse cuenta
        por el EOF de su tuberia de control —la senal portable de que su padre ya
        no existe— y retirarse *antes* de que nadie reprograme el job.
        """
        proceso = subprocess.run(
            [sys.executable, "-c", HIJO_ABANDONA_WORKER, str(self.raiz), "j1"],
            stdout=subprocess.PIPE, text=True, env=ENTORNO, timeout=LIMITE)
        self.assertEqual(proceso.returncode, 17, proceso.stdout)
        pid = int(proceso.stdout.strip())
        self.assertGreater(pid, 0)

        limite = time.monotonic() + 30
        while proceso_vivo(pid) and time.monotonic() < limite:
            time.sleep(0.02)
        self.assertFalse(proceso_vivo(pid),
                         f"el worker {pid} sobrevivio a la muerte de su coordinador")

        with self.sesion() as coordinador:
            self.assertEqual(coordinador.job("j1").estado, EstadoJob.QUEUED)
            self.assertFalse(proceso_vivo(pid),
                             "el huerfano seguia vivo cuando el job ya estaba reprogramado")

    def test_el_cierre_ordenado_tambien_retira_al_worker(self):
        proyecto = abrir_proyecto(self.raiz)
        coordinador = crear_coordinador(proyecto, catalogo=CATALOGO)
        try:
            coordinador.encolar(self.job("j1", payload={"pasos": 400, "retardo": 0.05}))
            limite = time.monotonic() + LIMITE
            pid = None
            while pid is None and time.monotonic() < limite:
                coordinador.paso()
                intentos = coordinador.intentos("j1")
                pid = intentos[0].pid if intentos else None
                time.sleep(0.01)
            self.assertIsNotNone(pid)
        finally:
            coordinador.cerrar()
            proyecto.close()
        limite = time.monotonic() + 30
        while proceso_vivo(pid) and time.monotonic() < limite:
            time.sleep(0.02)
        self.assertFalse(proceso_vivo(pid))


# --------------------------------------------------------------------------- #
# (2) El techo de intentos no se compra con una fila
# --------------------------------------------------------------------------- #

class TechoDeIntentosTests(BaseCiclo):
    def test_una_fila_manipulada_no_arranca_un_intento_de_mas(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", max_intentos=2, payload={"pasos": 1}))
            self.conexion.execute(
                "UPDATE Job SET attempts_used=2, max_attempts=100, state='queued' WHERE id='j1'")
            self.conexion.commit()
            coordinador.paso()
            job = coordinador.job("j1")
            self.assertEqual(coordinador.intentos("j1"), (),
                             "no puede existir un tercer intento con techo dos")
            self.assertEqual((job.estado, job.error_codigo),
                             (EstadoJob.FAILED, CODIGO_PRESUPUESTO))

    def test_la_cola_no_gira_en_vacio_con_un_job_sin_presupuesto(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", max_intentos=2, payload={"pasos": 1}))
            self.conexion.execute("UPDATE Job SET attempts_used=99 WHERE id='j1'")
            self.conexion.commit()
            self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE),
                            "un job sin presupuesto debe cerrarse, no bloquear el bucle")


# --------------------------------------------------------------------------- #
# (3) Un dependiente no corre sobre una entrada que ya no existe
# --------------------------------------------------------------------------- #

class EntradasInvalidadasTests(BaseCiclo):
    def test_un_hijo_no_corre_si_el_artefacto_del_padre_desaparecio(self):
        with self.sesion(limites=Recursos(cpu=2)) as coordinador:
            coordinador.encolar(self.job("padre", payload={"pasos": 1}))
            self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE))
            self.assertEqual(coordinador.job("padre").estado, EstadoJob.SUCCEEDED)

            (self.raiz / "artifacts" / "padre" / "salida.txt").unlink()
            self.assertEqual(self.abierto.repositorio.reconciliar().artefactos_reconstruibles, 1)

            coordinador.encolar(self.job("hijo", dependencias=("padre",), payload={"pasos": 1}))
            coordinador.encolar(self.job("ajeno", payload={"pasos": 1}))
            self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE))

            hijo = coordinador.job("hijo")
            self.assertEqual((hijo.estado, hijo.error_codigo, hijo.intentos_usados),
                             (EstadoJob.FAILED, CODIGO_DEPENDENCIA_INVALIDA, 0))
            self.assertFalse((self.raiz / "artifacts" / "hijo").exists())
            self.assertEqual(coordinador.job("ajeno").estado, EstadoJob.SUCCEEDED,
                             "un hermano sin relacion sigue su curso")
            self.assertEqual(coordinador.job("padre").estado, EstadoJob.SUCCEEDED,
                             "la historia del padre no se pierde")

    def test_un_artefacto_intacto_no_bloquea_al_dependiente(self):
        with self.sesion(limites=Recursos(cpu=2)) as coordinador:
            coordinador.encolar(self.job("padre", payload={"pasos": 1}))
            coordinador.encolar(self.job("hijo", dependencias=("padre",), payload={"pasos": 1}))
            self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE))
            self.abierto.repositorio.reconciliar()
            self.assertEqual(coordinador.job("hijo").estado, EstadoJob.SUCCEEDED)

    def test_el_vinculo_job_artefacto_queda_persistido(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", payload={"pasos": 1}))
            self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE))
            filas = self.conexion.execute(
                "SELECT job_id, relative_path FROM AnalysisArtifact").fetchall()
            self.assertEqual(filas, [("j1", "artifacts/j1/salida.txt")])


# --------------------------------------------------------------------------- #
# (4) Identidad canonica: dos ids no pueden compartir carpeta
# --------------------------------------------------------------------------- #

class IdentidadCanonicaTests(BaseCiclo):
    def test_dos_ids_que_colisionan_en_disco_no_pueden_coexistir(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("Alpha", payload={"pasos": 1}))
            for gemelo in ("alpha", "ALPHA", "AlPhA"):
                with self.subTest(gemelo=gemelo):
                    with self.assertRaises(ErrorJob):
                        coordinador.encolar(self.job(gemelo, payload={"pasos": 1}))
            self.assertEqual([job.id for job in coordinador.jobs()], ["Alpha"])

    def test_una_variante_unicode_no_normalizada_se_rechaza(self):
        import unicodedata

        descompuesto = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(descompuesto, "café")
        with self.assertRaises(ErrorIdentificadorJob):
            Job(id=descompuesto, stage="prueba", version_contrato="1")

    def test_dos_ids_distintos_de_verdad_siguen_conviviendo(self):
        with self.sesion(limites=Recursos(cpu=2)) as coordinador:
            coordinador.encolar(self.job("alpha", payload={"pasos": 1}))
            coordinador.encolar(self.job("beta", payload={"pasos": 1}))
            self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE))
            self.assertEqual({job.estado for job in coordinador.jobs()}, {EstadoJob.SUCCEEDED})


# --------------------------------------------------------------------------- #
# (5) El contrato de salidas gobierna la publicacion
# --------------------------------------------------------------------------- #

class ContratoDeSalidasTests(BaseCiclo):
    def test_publicar_algo_no_declarado_falla_y_no_deja_artefacto(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", payload={"pasos": 1,
                                                        "nombre_salida": "no-declarada.bin"}))
            self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE))
            job = coordinador.job("j1")
            self.assertEqual((job.estado, job.error_codigo),
                             (EstadoJob.FAILED, CODIGO_SALIDA_INVALIDA))
            self.assertFalse((self.raiz / "artifacts" / "j1").exists())
            self.assertEqual(self.conexion.execute(
                "SELECT COUNT(*) FROM AnalysisArtifact").fetchone()[0], 0)

    def test_una_etapa_sin_contrato_de_salidas_no_queda_restringida(self):
        libre = StageDefinition(nombre="prueba", version_contrato="1", recursos=Recursos(cpu=1))
        with self.sesion(catalogo={"prueba": libre}) as coordinador:
            coordinador.encolar(self.job("j1", payload={"pasos": 1,
                                                        "nombre_salida": "lo-que-sea.bin"}))
            self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE))
            self.assertEqual(coordinador.job("j1").estado, EstadoJob.SUCCEEDED)

    def test_lo_declarado_por_el_contrato_si_se_publica(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", payload={"pasos": 1}))
            self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE))
            self.assertEqual(coordinador.job("j1").estado, EstadoJob.SUCCEEDED)
            self.assertTrue((self.raiz / "artifacts" / "j1" / "salida.txt").is_file())


# --------------------------------------------------------------------------- #
# (9) El comando durable y su evento son una sola cosa
# --------------------------------------------------------------------------- #

class AtomicidadTests(BaseCiclo):
    def test_si_el_evento_no_se_puede_escribir_el_job_no_queda_encolado(self):
        with self.sesion() as coordinador:
            self.conexion.execute("CREATE TRIGGER sin_eventos BEFORE INSERT ON JobEvent"
                                  " BEGIN SELECT RAISE(ABORT, 'inyectado'); END")
            self.conexion.commit()
            with self.assertRaises(ErrorJob):
                coordinador.encolar(self.job("j1", payload={"pasos": 1}))
            self.assertEqual(self.conexion.execute("SELECT COUNT(*) FROM Job").fetchone()[0], 0,
                             "la vista observable no puede contradecir al estado")
            self.conexion.execute("DROP TRIGGER sin_eventos")
            self.conexion.commit()

    def test_un_cierre_sin_intento_publica_estado_y_evento_a_la_vez(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", payload={"pasos": 1}))
            coordinador.solicitar_cancelacion("j1")
            self.conexion.execute("CREATE TRIGGER sin_eventos BEFORE INSERT ON JobEvent"
                                  " BEGIN SELECT RAISE(ABORT, 'inyectado'); END")
            self.conexion.commit()
            with self.assertRaises(ErrorJob):
                coordinador.paso()
            self.assertEqual(coordinador.job("j1").estado, EstadoJob.QUEUED,
                             "sin evento no hay transicion")
            self.conexion.execute("DROP TRIGGER sin_eventos")
            self.conexion.commit()
            self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE))
            job = coordinador.job("j1")
            self.assertEqual((job.estado, job.error_codigo), (EstadoJob.FAILED, CODIGO_CANCELADO))
            self.assertIn("job.cancelado",
                          [evento.clave for evento in coordinador.eventos("j1")])


# --------------------------------------------------------------------------- #
# Dobles deterministas: arranque y orden del protocolo
# --------------------------------------------------------------------------- #

class RelojManual:
    def __init__(self) -> None:
        self.instante = 1000.0

    def ahora(self) -> float:
        return self.instante

    def dormir(self, segundos: float) -> None:
        self.instante += segundos

    def avanzar(self, segundos: float) -> None:
        self.instante += segundos


class HandleFalso:
    """Worker controlado a mano: nada ocurre salvo lo que la prueba decida."""

    def __init__(self) -> None:
        self.pendientes: list[MensajeWorker] = []
        self.cancelaciones = 0
        self.terminado = False
        self._vivo = True

    @property
    def pid(self) -> int | None:
        return 4242

    def entregar(self, mensaje: MensajeWorker) -> None:
        self.pendientes.append(mensaje)

    def mensajes(self) -> tuple[MensajeWorker, ...]:
        drenados, self.pendientes = tuple(self.pendientes), []
        return drenados

    def vivo(self) -> bool:
        return self._vivo

    def agotado(self) -> bool:
        return not self._vivo and not self.pendientes

    def morir(self) -> None:
        self._vivo = False

    def solicitar_cancelacion(self) -> None:
        self.cancelaciones += 1

    def terminar(self) -> None:
        self.terminado = True
        self._vivo = False

    def codigo_salida(self) -> int | None:
        return None if self._vivo else 9

    def diagnostico(self) -> str:
        return ""

    def cerrar(self, gracia: float = 0.0) -> None:
        self._vivo = False


class EjecutorFalso:
    def __init__(self) -> None:
        self.handles: list[HandleFalso] = []

    def lanzar(self, solicitud: SolicitudEjecucion) -> HandleFalso:
        handle = HandleFalso()
        self.handles.append(handle)
        return handle


class BaseDoble(BaseCiclo):
    def montar(self, stage=STAGE):
        proyecto = abrir_proyecto(self.raiz)
        self.addCleanup(proyecto.close)
        self.abierto = proyecto
        self.reloj = RelojManual()
        self.ejecutor = EjecutorFalso()
        coordinador = Coordinador(
            RepositorioSqliteJobs(proyecto.repositorio), self.ejecutor,
            AlmacenArtefactosProyecto(proyecto.repositorio.raiz), {stage.nombre: stage},
            limites=Recursos(cpu=2), reloj=self.reloj, duracion_lease=600.0)
        self.addCleanup(coordinador.cerrar)
        return coordinador

    def arrancar(self, coordinador, job_id="j1", **claves):
        coordinador.encolar(self.job(job_id, **claves))
        coordinador.paso()
        self.assertEqual(len(self.ejecutor.handles), 1, "el job no llego a lanzarse")
        return self.ejecutor.handles[0]

    def listo(self, handle):
        handle.entregar(MensajeWorker(tipo=TipoMensaje.INICIO, pid=4242))


class ArranqueTests(BaseDoble):
    def test_la_gracia_no_corre_mientras_el_worker_no_este_listo(self):
        """Es el fallo de WSL, hecho determinista.

        El arranque de un interprete sobre un sistema de archivos montado tarda
        mas que el propio timeout cooperativo. Si el cronometro empieza al mandar
        la orden, se mata a un worker que todavia no tenia escucha y una
        cancelacion cooperativa se reporta como forzada.
        """
        coordinador = self.montar()
        handle = self.arrancar(coordinador)
        coordinador.solicitar_cancelacion("j1")
        coordinador.paso()
        self.assertEqual(handle.cancelaciones, 1, "la orden se manda aunque aun no escuche")

        self.reloj.avanzar(10 * STAGE.politica_cancelacion.timeout_cooperativo)
        coordinador.paso()
        self.assertFalse(handle.terminado,
                         "no se puede matar a quien todavia no pudo oir la orden")

        self.listo(handle)
        coordinador.paso()
        self.assertFalse(handle.terminado, "la gracia acaba de empezar")

        self.reloj.avanzar(STAGE.politica_cancelacion.timeout_cooperativo + 0.01)
        coordinador.paso()
        self.assertTrue(handle.terminado, "pasada la gracia con el worker listo, se fuerza")
        self.assertIn("cancelacion.forzada",
                      [evento.clave for evento in coordinador.eventos("j1")])

    def test_un_worker_lento_que_coopera_termina_como_cancelado(self):
        coordinador = self.montar()
        handle = self.arrancar(coordinador)
        coordinador.solicitar_cancelacion("j1")
        coordinador.paso()
        self.reloj.avanzar(5 * STAGE.politica_cancelacion.timeout_cooperativo)
        coordinador.paso()

        self.listo(handle)
        coordinador.paso()
        handle.entregar(MensajeWorker(tipo=TipoMensaje.CANCELADO))
        coordinador.paso()

        job = coordinador.job("j1")
        self.assertEqual((job.estado, job.error_codigo), (EstadoJob.FAILED, CODIGO_CANCELADO))
        self.assertNotIn("cancelacion.forzada",
                         [evento.clave for evento in coordinador.eventos("j1")])
        self.assertFalse(handle.terminado)

    def test_un_worker_que_nunca_confirma_inicio_no_queda_eterno(self):
        coordinador = self.montar()
        handle = self.arrancar(coordinador)
        self.reloj.avanzar(STAGE.politica_cancelacion.timeout_arranque - 0.01)
        coordinador.paso()
        self.assertFalse(handle.terminado, "todavia esta dentro de su plazo de arranque")

        self.reloj.avanzar(0.02)
        coordinador.paso()
        self.assertTrue(handle.terminado)
        job = coordinador.job("j1")
        self.assertEqual((job.estado, job.error_codigo), (EstadoJob.FAILED, CODIGO_ARRANQUE))
        self.assertEqual(coordinador.resumen().en_curso, ())

    def test_un_stage_sordo_que_si_arranco_se_fuerza_tras_su_gracia(self):
        coordinador = self.montar()
        handle = self.arrancar(coordinador)
        self.listo(handle)
        coordinador.paso()
        coordinador.solicitar_cancelacion("j1")
        coordinador.paso()
        self.assertFalse(handle.terminado)
        self.reloj.avanzar(STAGE.politica_cancelacion.timeout_cooperativo + 0.01)
        coordinador.paso()
        self.assertTrue(handle.terminado)
        handle.morir()
        coordinador.paso()
        self.assertEqual(coordinador.job("j1").error_codigo, CODIGO_TIMEOUT_CANCELACION)

    def test_un_arranque_normal_no_dispara_el_vigilante(self):
        coordinador = self.montar()
        handle = self.arrancar(coordinador)
        self.listo(handle)
        coordinador.paso()
        self.reloj.avanzar(10 * STAGE.politica_cancelacion.timeout_arranque)
        coordinador.paso()
        self.assertFalse(handle.terminado)
        self.assertEqual(coordinador.job("j1").estado, EstadoJob.RUNNING)


class OrdenDelProtocoloTests(BaseDoble):
    def mensajes_fuera_de_orden(self):
        return (
            ("resultado", MensajeWorker(tipo=TipoMensaje.RESULTADO,
                                        manifiesto=ManifiestoSalida())),
            ("progreso", MensajeWorker(tipo=TipoMensaje.PROGRESO, unidades_hechas=1)),
            ("checkpoint", MensajeWorker(tipo=TipoMensaje.CHECKPOINT, checkpoint={"p": 1})),
            ("cancelado", MensajeWorker(tipo=TipoMensaje.CANCELADO)),
        )

    def test_nada_se_acepta_antes_de_que_el_worker_se_presente(self):
        for nombre, mensaje in self.mensajes_fuera_de_orden():
            with self.subTest(mensaje=nombre):
                temporal = tempfile.TemporaryDirectory()
                self.addCleanup(temporal.cleanup)
                raiz = Path(temporal.name) / "d.clipsapp"
                crear_proyecto(raiz, "D").close()
                self.raiz = raiz
                coordinador = self.montar()
                handle = self.arrancar(coordinador)
                handle.entregar(mensaje)
                coordinador.paso()
                job = coordinador.job("j1")
                self.assertEqual((job.estado, job.error_codigo),
                                 (EstadoJob.FAILED, CODIGO_PROTOCOLO))
                self.assertFalse((raiz / "artifacts" / "j1").exists(),
                                 "un resultado sin inicio jamas puede publicar")

    def test_un_segundo_inicio_rompe_el_protocolo(self):
        coordinador = self.montar()
        handle = self.arrancar(coordinador)
        self.listo(handle)
        coordinador.paso()
        self.listo(handle)
        coordinador.paso()
        job = coordinador.job("j1")
        self.assertEqual((job.estado, job.error_codigo), (EstadoJob.FAILED, CODIGO_PROTOCOLO))

    def test_una_linea_invalida_antes_de_inicio_sigue_siendo_fallo_de_protocolo(self):
        """El lector convierte la basura en `error`; eso debe seguir aceptandose."""
        coordinador = self.montar()
        handle = self.arrancar(coordinador)
        handle.entregar(MensajeWorker(tipo=TipoMensaje.ERROR, codigo=CODIGO_PROTOCOLO,
                                      detalle="linea basura"))
        coordinador.paso()
        job = coordinador.job("j1")
        self.assertEqual((job.estado, job.error_codigo), (EstadoJob.FAILED, CODIGO_PROTOCOLO))

    def test_el_orden_correcto_atraviesa_sin_incidentes(self):
        coordinador = self.montar()
        handle = self.arrancar(coordinador)
        self.listo(handle)
        handle.entregar(MensajeWorker(tipo=TipoMensaje.PROGRESO, unidades_hechas=1,
                                      unidades_totales=1))
        handle.entregar(MensajeWorker(tipo=TipoMensaje.CHECKPOINT, checkpoint={"paso": 1}))
        coordinador.paso()
        self.assertEqual(coordinador.job("j1").estado, EstadoJob.RUNNING)
        self.assertEqual(dict(coordinador.job("j1").checkpoint), {"paso": 1})


if __name__ == "__main__":
    unittest.main()
