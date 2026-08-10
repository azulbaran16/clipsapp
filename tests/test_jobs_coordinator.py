"""Coordinador con procesos reales: crash, cancelacion, recuperacion y limites.

Nada se simula aqui. Cada job arranca un interprete de verdad que escribe en la
carpeta del proyecto, y los fallos se provocan matando procesos o dejando que se
maten solos. Un mock del ejecutor probaria que el coordinador habla bien consigo
mismo; lo que el ticket exige es que sobreviva a un sistema operativo.
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

from clipperkick.domain.jobs import (  # noqa: E402
    CODIGO_CANCELADO, CODIGO_CONTRATO, CODIGO_CRASH, CODIGO_DEPENDENCIA, CODIGO_PROTOCOLO,
    CODIGO_RECURSOS, CODIGO_SALIDA_INVALIDA, CODIGO_STAGE, CODIGO_TIMEOUT_CANCELACION,
    ESTADOS_ACTIVOS, ErrorContratoStage, ErrorDependenciaJob, ErrorIdentificadorJob, ErrorJob,
    ErrorStageDesconocido, ErrorValidacionSalida, EstadoJob, Job, PoliticaCancelacion,
    PoliticaRetry, Recursos, StageDefinition, TipoEvento, TipoMensaje,
)
from clipperkick.application.jobs import SolicitudEjecucion  # noqa: E402
from clipperkick.infrastructure.jobs import (  # noqa: E402
    CODIGO_PERMANENTE_PRUEBA, CODIGO_TRANSITORIO_PRUEBA, AlmacenArtefactosProyecto,
    EjecutorSubproceso, NOMBRE_STAGE_PRUEBA_GPU, crear_coordinador,
)
from clipperkick.infrastructure.jobs.artifacts import NOMBRE_PUBLICACION  # noqa: E402
from clipperkick.infrastructure.project import abrir_proyecto, crear_proyecto  # noqa: E402


SRC = str(Path(__file__).resolve().parents[1] / "src")
ENTORNO = dict(os.environ, PYTHONPATH=SRC, PYTHONDONTWRITEBYTECODE="1")
LIMITE = 90.0        # techo de seguridad; el trabajo real dura muy por debajo
REPETICIONES = 3     # los focos de concurrencia se repiten para delatar flakiness

STAGE = StageDefinition(
    nombre="prueba", version_contrato="1", salidas=("salida.txt",), recursos=Recursos(cpu=1),
    soporta_checkpoint=True,
    politica_retry=PoliticaRetry(max_intentos=3,
                                 codigos_transitorios=frozenset({CODIGO_TRANSITORIO_PRUEBA})),
    politica_cancelacion=PoliticaCancelacion(timeout_cooperativo=0.4))
STAGE_GPU = StageDefinition(
    nombre=NOMBRE_STAGE_PRUEBA_GPU, version_contrato="1", recursos=Recursos(gpu=1),
    soporta_checkpoint=True, politica_cancelacion=PoliticaCancelacion(timeout_cooperativo=0.4))
CATALOGO = {STAGE.nombre: STAGE, STAGE_GPU.nombre: STAGE_GPU}


# Coordinador que arranca un job largo y muere de golpe con el intento vivo.
# Es la unica forma honesta de producir un huerfano: sin `finally`, sin cierre.
HIJO_MUERE_CON_JOB_VIVO = """
import os, sys, time
from pathlib import Path
from clipperkick.domain.jobs import EstadoJob, Job, PoliticaCancelacion, PoliticaRetry, Recursos, StageDefinition
from clipperkick.infrastructure.jobs import crear_coordinador
from clipperkick.infrastructure.project import abrir_proyecto

raiz, job_id, pasos = sys.argv[1], sys.argv[2], int(sys.argv[3])
stage = StageDefinition(nombre="prueba", version_contrato="1", soporta_checkpoint=True,
                        politica_retry=PoliticaRetry(max_intentos=3),
                        politica_cancelacion=PoliticaCancelacion(timeout_cooperativo=0.4))
proyecto = abrir_proyecto(raiz)
coordinador = crear_coordinador(proyecto, catalogo={"prueba": stage}, duracion_lease=30.0)
coordinador.encolar(Job(id=job_id, stage="prueba", version_contrato="1", max_intentos=3,
                        payload={"pasos": pasos, "retardo": 0.15, "checkpoint_cada": 1}))
limite = time.monotonic() + 60
while time.monotonic() < limite:
    coordinador.paso()
    checkpoint = coordinador.job(job_id).checkpoint
    if checkpoint and int(checkpoint.get("paso", 0)) >= 2:
        print("VIVO", flush=True)
        os._exit(17)
    time.sleep(0.01)
print("NO_LLEGO", flush=True)
os._exit(1)
"""


class BaseCoordinador(unittest.TestCase):
    def setUp(self):
        self.temporal = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporal.cleanup)
        self.base = Path(self.temporal.name)
        self.raiz = self.base / "demo.clipsapp"
        crear_proyecto(self.raiz, "Demo").close()

    @contextmanager
    def sesion(self, **claves):
        proyecto = abrir_proyecto(self.raiz)
        self.assertFalse(proyecto.solo_lectura, "la sesion de prueba debe poder escribir")
        coordinador = crear_coordinador(proyecto, catalogo=CATALOGO, **claves)
        self.abierto = proyecto
        try:
            yield coordinador
        finally:
            self.abierto = None
            coordinador.cerrar()
            proyecto.close()

    @property
    def conexion(self):
        """La base solo se inspecciona por la frontera que la posee."""
        return self.abierto.repositorio.conexion

    # -- utilidades ------------------------------------------------------ #

    def job(self, identificador, **claves):
        payload = claves.pop("payload", {})
        return Job(id=identificador, stage="prueba", version_contrato="1", payload=payload,
                   **claves)

    def estado(self, coordinador, job_id):
        return coordinador.job(job_id)

    def claves(self, coordinador, job_id=None):
        return [evento.clave for evento in coordinador.eventos(job_id)]

    def correr(self, coordinador):
        self.assertTrue(coordinador.ejecutar(limite_segundos=LIMITE),
                        "el coordinador no vacio la cola dentro del limite")

    def artefacto(self, job_id, nombre="salida.txt"):
        return self.raiz / "artifacts" / job_id / nombre


# --------------------------------------------------------------------------- #
# Contrato del handle: muerto no es lo mismo que leido
# --------------------------------------------------------------------------- #

class HandleWorkerTests(BaseCoordinador):
    def lanzar(self, **payload):
        directorio = self.base / "handle"
        directorio.mkdir(exist_ok=True)
        handle = EjecutorSubproceso().lanzar(SolicitudEjecucion(
            job_id="h", attempt_id="a", stage="prueba", version_contrato="1",
            directorio=str(directorio), payload=payload))
        self.addCleanup(handle.cerrar)
        return handle

    def test_un_worker_muerto_no_esta_agotado_mientras_quede_algo_por_leer(self):
        """Declararlo agotado en cuanto muere perderia su ultimo mensaje.

        El orden del bucle es el que hace la prueba determinista: se pregunta
        primero si esta agotado y solo despues se drena. Una implementacion que
        confunda "murio" con "ya lo lei" corta el bucle antes de recoger el
        resultado, y el resultado nunca aparece.
        """
        handle = self.lanzar(pasos=2)
        limite = time.monotonic() + LIMITE
        while handle.vivo():
            self.assertLess(time.monotonic(), limite, "el worker no termino")
            time.sleep(0.005)

        recogidos = []
        while time.monotonic() < limite:
            if handle.agotado():
                break
            recogidos.extend(handle.mensajes())
            time.sleep(0.005)
        else:
            self.fail("el handle nunca se declaro agotado")

        tipos = [mensaje.tipo for mensaje in recogidos]
        self.assertIn(TipoMensaje.RESULTADO, tipos,
                      "el resultado del worker se perdio al declararlo agotado antes de tiempo")
        self.assertIn(TipoMensaje.INICIO, tipos)
        self.assertEqual(handle.mensajes(), (), "un handle agotado no puede seguir entregando")
        self.assertEqual(handle.codigo_salida(), 0)

    def test_el_diagnostico_recoge_stderr_sin_contaminar_el_protocolo(self):
        handle = self.lanzar(pasos=1)
        limite = time.monotonic() + LIMITE
        while not handle.agotado() and time.monotonic() < limite:
            handle.mensajes()
            time.sleep(0.005)
        self.assertEqual(handle.diagnostico(), "",
                         "la etapa de prueba no debe escribir ruido en stderr")


# --------------------------------------------------------------------------- #
# Camino nominal y eventos
# --------------------------------------------------------------------------- #

class CaminoNominalTests(BaseCoordinador):
    def test_un_job_se_ejecuta_valida_publica_y_queda_registrado(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", payload={"pasos": 3}))
            self.correr(coordinador)
            job = self.estado(coordinador, "j1")
            self.assertEqual((job.estado, job.error_codigo), (EstadoJob.SUCCEEDED, None))
            self.assertEqual(self.artefacto("j1").read_text(encoding="utf-8"),
                             "ok:0\nok:1\nok:2\n")
            self.assertEqual(
                self.conexion.execute(
                    "SELECT relative_path, state FROM AnalysisArtifact").fetchall(),
                [("artifacts/j1/salida.txt", "available")])

    def test_el_progreso_es_estructurado_y_no_texto_para_humanos(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", payload={"pasos": 4}))
            self.correr(coordinador)
            progreso = [evento for evento in coordinador.eventos("j1")
                        if evento.tipo is TipoEvento.PROGRESO]
            self.assertEqual([(evento.unidades_hechas, evento.unidades_totales)
                              for evento in progreso],
                             [(1.0, 4.0), (2.0, 4.0), (3.0, 4.0), (4.0, 4.0)])
            self.assertEqual({evento.clave for evento in progreso}, {"stage.prueba.paso"})
            self.assertEqual([evento.fraccion for evento in progreso],
                             [0.25, 0.5, 0.75, 1.0])

    def test_la_historia_del_job_se_lee_por_secuencia_y_por_tipo(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", payload={"pasos": 1, "checkpoint_cada": 1}))
            self.correr(coordinador)
            tipos = [evento.tipo for evento in coordinador.eventos("j1")]
            for esperado in (TipoEvento.ENCOLADO, TipoEvento.INICIO, TipoEvento.PROGRESO,
                             TipoEvento.CHECKPOINT, TipoEvento.VALIDACION, TipoEvento.PUBLICACION,
                             TipoEvento.FIN):
                self.assertIn(esperado, tipos)
            secuencias = [evento.secuencia for evento in coordinador.eventos("j1")]
            self.assertEqual(secuencias, sorted(secuencias))
            self.assertEqual(coordinador.eventos("j1", desde=secuencias[-1]), ())

    def test_encolar_una_etapa_que_no_existe_se_rechaza_en_el_acto(self):
        with self.sesion() as coordinador:
            with self.assertRaises(ErrorStageDesconocido):
                coordinador.encolar(Job(id="j1", stage="inexistente", version_contrato="1"))
            self.assertEqual(coordinador.jobs(), ())

    def test_un_stage_desconocido_ya_persistido_falla_sin_lanzar_nada(self):
        """La fila puede venir de una version con mas etapas que esta."""
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", payload={"pasos": 1}))
            self.conexion.execute("UPDATE Job SET stage='inexistente' WHERE id='j1'")
            self.conexion.commit()
            self.correr(coordinador)
            job = self.estado(coordinador, "j1")
            self.assertEqual((job.estado, job.error_codigo, job.intentos_usados),
                             (EstadoJob.FAILED, CODIGO_STAGE, 0))
            self.assertFalse((self.raiz / "artifacts" / "j1").exists())


# --------------------------------------------------------------------------- #
# Crash del worker
# --------------------------------------------------------------------------- #

class CrashTests(BaseCoordinador):
    def test_un_worker_que_muere_de_golpe_no_derriba_al_coordinador(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("muere", payload={"pasos": 4, "crash_en": 1}))
            coordinador.encolar(self.job("vive", payload={"pasos": 2}))
            self.correr(coordinador)
            muerto = self.estado(coordinador, "muere")
            self.assertEqual((muerto.estado, muerto.error_codigo),
                             (EstadoJob.FAILED, CODIGO_CRASH))
            self.assertEqual(self.estado(coordinador, "vive").estado, EstadoJob.SUCCEEDED,
                             "el hermano sano debe completarse pese al crash")
            self.assertTrue(self.artefacto("vive").is_file())
            self.assertFalse((self.raiz / "artifacts" / "muere").exists(),
                             "un crash no puede publicar nada")

    def test_un_crash_no_es_transitorio_y_no_consume_el_presupuesto_de_reintentos(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", max_intentos=3,
                                         payload={"pasos": 2, "crash_en": 0}))
            self.correr(coordinador)
            job = self.estado(coordinador, "j1")
            self.assertEqual(job.intentos_usados, 1,
                             "un fallo no declarado transitorio no se reintenta")
            self.assertEqual(len(coordinador.intentos("j1")), 1)

    def test_el_coordinador_sigue_aceptando_trabajo_despues_de_varios_crashes(self):
        with self.sesion() as coordinador:
            for indice in range(4):
                coordinador.encolar(self.job(f"crash{indice}",
                                             payload={"pasos": 2, "crash_en": 0}))
            self.correr(coordinador)
            coordinador.encolar(self.job("despues", payload={"pasos": 1}))
            self.correr(coordinador)
            self.assertEqual(self.estado(coordinador, "despues").estado, EstadoJob.SUCCEEDED)

    def test_una_linea_que_rompe_el_protocolo_mata_el_job_diagnosticado(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", payload={"pasos": 1,
                                                        "salida_invalida": "protocolo"}))
            self.correr(coordinador)
            job = self.estado(coordinador, "j1")
            self.assertEqual((job.estado, job.error_codigo), (EstadoJob.FAILED, CODIGO_PROTOCOLO))
            self.assertFalse((self.raiz / "artifacts" / "j1").exists())


# --------------------------------------------------------------------------- #
# Cancelacion
# --------------------------------------------------------------------------- #

class CancelacionTests(BaseCoordinador):
    def arrancar(self, coordinador, job_id, **payload):
        coordinador.encolar(self.job(job_id, payload=payload))
        limite = time.monotonic() + LIMITE
        while time.monotonic() < limite:
            coordinador.paso()
            if self.estado(coordinador, job_id).estado in ESTADOS_ACTIVOS:
                return
            time.sleep(0.01)
        self.fail(f"{job_id} nunca llego a correr")

    def test_la_cancelacion_cooperativa_termina_sin_forzar(self):
        for repeticion in range(REPETICIONES):
            with self.subTest(repeticion=repeticion), self.sesion() as coordinador:
                identificador = f"j{repeticion}"
                self.arrancar(coordinador, identificador, pasos=200, retardo=0.05)
                self.assertTrue(coordinador.solicitar_cancelacion(identificador))
                self.correr(coordinador)
                job = self.estado(coordinador, identificador)
                self.assertEqual((job.estado, job.error_codigo),
                                 (EstadoJob.FAILED, CODIGO_CANCELADO))
                self.assertNotIn("cancelacion.forzada", self.claves(coordinador, identificador),
                                 "una etapa que coopera no debe ser matada")
                self.assertIn("cancelacion.cooperativa", self.claves(coordinador, identificador))
                self.assertFalse((self.raiz / "artifacts" / identificador).exists())

    def test_una_etapa_sorda_se_fuerza_solo_despues_del_timeout(self):
        for repeticion in range(REPETICIONES):
            with self.subTest(repeticion=repeticion), self.sesion() as coordinador:
                identificador = f"sordo{repeticion}"
                self.arrancar(coordinador, identificador, pasos=1, retardo=30.0,
                              respetar_cancelacion=False)
                inicio = time.monotonic()
                coordinador.solicitar_cancelacion(identificador)
                self.correr(coordinador)
                transcurrido = time.monotonic() - inicio
                job = self.estado(coordinador, identificador)
                self.assertEqual((job.estado, job.error_codigo),
                                 (EstadoJob.FAILED, CODIGO_TIMEOUT_CANCELACION))
                self.assertIn("cancelacion.forzada", self.claves(coordinador, identificador))
                self.assertGreaterEqual(transcurrido, STAGE.politica_cancelacion.timeout_cooperativo,
                                        "no se puede matar antes de dar la oportunidad de cooperar")
                self.assertLess(transcurrido, 25.0, "tampoco se puede esperar al final del retardo")

    def test_cancelar_un_job_encolado_lo_cierra_sin_arrancarlo(self):
        with self.sesion(limites=Recursos(cpu=1)) as coordinador:
            self.arrancar(coordinador, "ocupa", pasos=40, retardo=0.05)
            coordinador.encolar(self.job("espera", payload={"pasos": 1}))
            self.assertTrue(coordinador.solicitar_cancelacion("espera"))
            coordinador.solicitar_cancelacion("ocupa")
            self.correr(coordinador)
            job = self.estado(coordinador, "espera")
            self.assertEqual((job.estado, job.error_codigo, job.intentos_usados),
                             (EstadoJob.FAILED, CODIGO_CANCELADO, 0))
            self.assertEqual(coordinador.intentos("espera"), ())

    def test_un_resultado_que_llega_despues_de_cancelar_no_se_publica(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", payload={"pasos": 3, "retardo": 0.05}))
            coordinador.paso()
            coordinador.solicitar_cancelacion("j1")
            self.correr(coordinador)
            job = self.estado(coordinador, "j1")
            self.assertEqual((job.estado, job.error_codigo), (EstadoJob.FAILED, CODIGO_CANCELADO))
            self.assertFalse((self.raiz / "artifacts" / "j1").exists(),
                             "la cancelacion gana aunque el worker alcance a producir")

    def test_una_cancelacion_no_alcanza_a_los_demas_jobs(self):
        with self.sesion(limites=Recursos(cpu=2)) as coordinador:
            self.arrancar(coordinador, "cancelado", pasos=200, retardo=0.05)
            coordinador.encolar(self.job("sano", payload={"pasos": 2}))
            coordinador.solicitar_cancelacion("cancelado")
            self.correr(coordinador)
            self.assertEqual(self.estado(coordinador, "sano").estado, EstadoJob.SUCCEEDED)
            self.assertEqual(self.estado(coordinador, "cancelado").error_codigo, CODIGO_CANCELADO)


# --------------------------------------------------------------------------- #
# Reintento
# --------------------------------------------------------------------------- #

class ReintentoTests(BaseCoordinador):
    def test_un_fallo_transitorio_se_reintenta_y_el_segundo_intento_publica(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", max_intentos=3,
                                         payload={"pasos": 2,
                                                  "marcador_transitorio": "testigo.txt"}))
            self.correr(coordinador)
            job = self.estado(coordinador, "j1")
            self.assertEqual((job.estado, job.intentos_usados), (EstadoJob.SUCCEEDED, 2))
            self.assertIn("job.reintentado", self.claves(coordinador, "j1"))
            self.assertTrue(self.artefacto("j1").is_file())
            self.assertTrue((self.raiz / "cache" / "jobs" / "j1" / "_estado" / "testigo.txt").is_file(),
                            "el estado entre intentos vive dentro del proyecto")

    def test_un_transitorio_que_no_se_cura_agota_el_presupuesto_y_se_detiene(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", max_intentos=3,
                                         payload={"pasos": 2, "error_en": 0,
                                                  "codigo": CODIGO_TRANSITORIO_PRUEBA}))
            self.correr(coordinador)
            job = self.estado(coordinador, "j1")
            self.assertEqual((job.estado, job.error_codigo, job.intentos_usados),
                             (EstadoJob.FAILED, CODIGO_TRANSITORIO_PRUEBA, 3))
            self.assertEqual(len(coordinador.intentos("j1")), 3)

    def test_un_fallo_permanente_no_se_reintenta_jamas(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", max_intentos=5,
                                         payload={"pasos": 2, "error_en": 0,
                                                  "codigo": CODIGO_PERMANENTE_PRUEBA}))
            self.correr(coordinador)
            job = self.estado(coordinador, "j1")
            self.assertEqual((job.estado, job.error_codigo, job.intentos_usados),
                             (EstadoJob.FAILED, CODIGO_PERMANENTE_PRUEBA, 1))
            self.assertNotIn("job.reintentado", self.claves(coordinador, "j1"))

    def test_una_cancelacion_durante_el_reintento_lo_detiene(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", max_intentos=3,
                                         payload={"pasos": 2, "error_en": 0,
                                                  "codigo": CODIGO_TRANSITORIO_PRUEBA}))
            coordinador.paso()
            coordinador.solicitar_cancelacion("j1")
            self.correr(coordinador)
            self.assertLess(self.estado(coordinador, "j1").intentos_usados, 3)


# --------------------------------------------------------------------------- #
# Salida invalida
# --------------------------------------------------------------------------- #

class ValidacionSalidaTests(BaseCoordinador):
    def test_ninguna_forma_de_salida_invalida_llega_a_publicarse(self):
        casos = ("ausente", "tamano", "escapada", "sin_manifiesto")
        for caso in casos:
            with self.subTest(caso=caso), self.sesion() as coordinador:
                coordinador.encolar(self.job(caso, payload={"pasos": 1,
                                                            "salida_invalida": caso}))
                self.correr(coordinador)
                job = self.estado(coordinador, caso)
                self.assertEqual((job.estado, job.error_codigo),
                                 (EstadoJob.FAILED, CODIGO_SALIDA_INVALIDA))
                self.assertFalse((self.raiz / "artifacts" / caso).exists())
                self.assertEqual(
                    self.conexion.execute(
                        "SELECT COUNT(*) FROM AnalysisArtifact").fetchone()[0], 0)

    def test_una_salida_invalida_es_permanente_aunque_quede_presupuesto(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", max_intentos=3,
                                         payload={"pasos": 1, "salida_invalida": "tamano"}))
            self.correr(coordinador)
            self.assertEqual(self.estado(coordinador, "j1").intentos_usados, 1)

    def test_el_temporal_de_un_intento_fallido_queda_en_cuarentena(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", payload={"pasos": 1,
                                                        "salida_invalida": "ausente"}))
            self.correr(coordinador)
            # Desde T04-F07 la cuarentena preserva **en sitio**: el intento
            # ya estrena nombre, asi que nadie lo va a reutilizar y moverlo solo
            # anadia un destino deterministico que habia que despejar borrando.
            intentos = list((self.raiz / "cache" / "jobs" / "j1").glob("*"))
            self.assertEqual(len(intentos), 1,
                             "un temporal fallido no se borra en silencio: se conserva")
            self.assertTrue(any(intentos[0].iterdir()) or intentos[0].is_dir())

    def test_una_ruta_escapada_no_toca_nada_fuera_del_proyecto(self):
        victima = self.base / "fuera.txt"
        victima.write_text("no tocar", encoding="utf-8")
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", payload={"pasos": 1,
                                                        "salida_invalida": "escapada"}))
            self.correr(coordinador)
            self.assertEqual(victima.read_text(encoding="utf-8"), "no tocar")
            self.assertEqual(self.estado(coordinador, "j1").error_codigo, CODIGO_SALIDA_INVALIDA)


# --------------------------------------------------------------------------- #
# Dependencias
# --------------------------------------------------------------------------- #

class DependenciaTests(BaseCoordinador):
    def test_un_dependiente_no_arranca_antes_de_que_su_entrada_este_validada(self):
        with self.sesion(limites=Recursos(cpu=4)) as coordinador:
            coordinador.encolar(self.job("padre", payload={"pasos": 3, "retardo": 0.05}))
            coordinador.encolar(self.job("hijo", dependencias=("padre",), payload={"pasos": 1}))
            self.correr(coordinador)
            eventos = coordinador.eventos()
            fin_padre = next(evento.secuencia for evento in eventos
                             if evento.job_id == "padre" and evento.tipo is TipoEvento.PUBLICACION)
            inicio_hijo = next(evento.secuencia for evento in eventos
                               if evento.job_id == "hijo" and evento.tipo is TipoEvento.INICIO)
            self.assertGreater(inicio_hijo, fin_padre)
            self.assertEqual(self.estado(coordinador, "hijo").estado, EstadoJob.SUCCEEDED)

    def test_un_padre_fallido_marca_imposible_al_hijo_y_deja_correr_al_hermano(self):
        with self.sesion(limites=Recursos(cpu=4)) as coordinador:
            coordinador.encolar(self.job("padre", payload={"pasos": 1, "error_en": 0}))
            coordinador.encolar(self.job("hijo", dependencias=("padre",), payload={"pasos": 1}))
            coordinador.encolar(self.job("hermano", payload={"pasos": 1}))
            self.correr(coordinador)
            hijo = self.estado(coordinador, "hijo")
            self.assertEqual((hijo.estado, hijo.error_codigo, hijo.intentos_usados),
                             (EstadoJob.FAILED, CODIGO_DEPENDENCIA, 0))
            self.assertEqual(self.estado(coordinador, "hermano").estado, EstadoJob.SUCCEEDED)
            self.assertTrue(self.artefacto("hermano").is_file())

    def test_un_diamante_completo_respeta_el_orden(self):
        with self.sesion(limites=Recursos(cpu=4)) as coordinador:
            coordinador.encolar(self.job("raiz", payload={"pasos": 1}))
            coordinador.encolar(self.job("izq", dependencias=("raiz",), payload={"pasos": 1}))
            coordinador.encolar(self.job("der", dependencias=("raiz",), payload={"pasos": 1}))
            coordinador.encolar(self.job("union", dependencias=("izq", "der"),
                                         payload={"pasos": 1}))
            self.correr(coordinador)
            for identificador in ("raiz", "izq", "der", "union"):
                self.assertEqual(self.estado(coordinador, identificador).estado,
                                 EstadoJob.SUCCEEDED, identificador)

    def test_un_ciclo_o_una_arista_rota_se_rechaza_al_encolar(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("a"))
            with self.assertRaises(ErrorDependenciaJob):
                coordinador.encolar(self.job("b", dependencias=("fantasma",)))
            coordinador.encolar(self.job("b", dependencias=("a",)))
            # El ciclo solo existe contra el grafo ya persistido.
            with self.assertRaises(ErrorDependenciaJob):
                coordinador.encolar(self.job("c", dependencias=("b", "c")))
            self.assertEqual({job.id for job in coordinador.jobs()}, {"a", "b"})


# --------------------------------------------------------------------------- #
# Limites de recursos
# --------------------------------------------------------------------------- #

class RecursosTests(BaseCoordinador):
    def concurrencia_maxima(self, coordinador):
        """Maximo de intentos vivos observado, con diagnostico si algo no arranca.

        Un fallo aqui casi siempre significa que un worker no llego a lanzarse;
        sin el detalle del job la asercion solo diria "1 != 3", que no permite
        distinguir un limite mal aplicado de un arranque fallido.
        """
        maxima = 0
        limite = time.monotonic() + LIMITE
        while coordinador.paso():
            maxima = max(maxima, len(coordinador.resumen().en_curso))
            self.assertLess(time.monotonic(), limite,
                            f"la cola no se vacio a tiempo: {self.diagnostico(coordinador)}")
            time.sleep(0.005)
        return maxima

    def diagnostico(self, coordinador):
        return [(job.id, job.estado.value, job.error_codigo, job.diagnostico)
                for job in coordinador.jobs()]

    def test_un_solo_trabajo_pesado_por_clase(self):
        for repeticion in range(REPETICIONES):
            with self.subTest(repeticion=repeticion), self.sesion(limites=Recursos(cpu=1)) as coordinador:
                identificadores = [f"j{repeticion}-{indice}" for indice in range(4)]
                for identificador in identificadores:
                    coordinador.encolar(self.job(identificador, payload={"pasos": 3,
                                                                         "retardo": 0.05}))
                observada = self.concurrencia_maxima(coordinador)
                self.assertEqual(observada, 1, self.diagnostico(coordinador))
                for identificador in identificadores:
                    self.assertEqual(self.estado(coordinador, identificador).estado,
                                     EstadoJob.SUCCEEDED, identificador)

    def test_una_capacidad_mayor_permite_paralelismo_real(self):
        for repeticion in range(REPETICIONES):
            with self.subTest(repeticion=repeticion), self.sesion(limites=Recursos(cpu=3)) as coordinador:
                for indice in range(3):
                    coordinador.encolar(self.job(f"j{repeticion}-{indice}",
                                                 payload={"pasos": 6, "retardo": 0.05}))
                observada = self.concurrencia_maxima(coordinador)
                self.assertEqual(observada, 3, self.diagnostico(coordinador))

    def test_clases_distintas_no_se_estorban(self):
        """El peso lo declara la etapa, asi que cada clase necesita su contrato."""
        with self.sesion(limites=Recursos(cpu=1, gpu=1)) as coordinador:
            coordinador.encolar(self.job("cpu", payload={"pasos": 5, "retardo": 0.05}))
            coordinador.encolar(Job(id="gpu", stage=NOMBRE_STAGE_PRUEBA_GPU, version_contrato="1",
                                    payload={"pasos": 5, "retardo": 0.05}))
            observada = self.concurrencia_maxima(coordinador)
            self.assertEqual(observada, 2, self.diagnostico(coordinador))
            for identificador in ("cpu", "gpu"):
                self.assertEqual(self.estado(coordinador, identificador).estado,
                                 EstadoJob.SUCCEEDED, identificador)

    def test_un_job_mas_pesado_que_la_capacidad_falla_en_vez_de_esperar_para_siempre(self):
        with self.sesion(limites=Recursos(cpu=1)) as coordinador:
            coordinador.encolar(self.job("imposible", recursos=Recursos(cpu=4),
                                         payload={"pasos": 1}))
            coordinador.encolar(self.job("posible", payload={"pasos": 1}))
            self.correr(coordinador)
            self.assertEqual(self.estado(coordinador, "imposible").error_codigo, CODIGO_RECURSOS)
            self.assertEqual(self.estado(coordinador, "posible").estado, EstadoJob.SUCCEEDED)


# --------------------------------------------------------------------------- #
# Recuperacion tras cerrar o morir
# --------------------------------------------------------------------------- #

class RecuperacionTests(BaseCoordinador):
    def arrancar_y_abandonar(self, job_id, payload):
        """Deja un intento `running` con su worker suelto, como un corte real."""
        proyecto = abrir_proyecto(self.raiz)
        coordinador = crear_coordinador(proyecto, catalogo=CATALOGO, duracion_lease=30.0)
        coordinador.encolar(self.job(job_id, max_intentos=3, payload=payload))
        limite = time.monotonic() + LIMITE
        while time.monotonic() < limite:
            coordinador.paso()
            if self.estado(coordinador, job_id).checkpoint:
                coordinador.cerrar()          # mata workers, no cierra intentos
                proyecto.close()
                return
            time.sleep(0.01)
        coordinador.cerrar()
        proyecto.close()
        self.fail(f"{job_id} nunca llego a publicar un checkpoint")

    def test_reabrir_recupera_sin_que_nadie_lo_pida(self):
        """Ensamblar el coordinador *es* recuperar: no hay forma de olvidarlo."""
        self.arrancar_y_abandonar("j1", {"pasos": 20, "retardo": 0.05, "checkpoint_cada": 1})
        with self.sesion() as coordinador:
            job = self.estado(coordinador, "j1")
            self.assertEqual((job.estado, job.interrupciones_usadas), (EstadoJob.QUEUED, 1))
            self.assertEqual(coordinador.intentos("j1")[0].estado, EstadoJob.INTERRUPTED)
            self.assertIn("intento.interrumpido", self.claves(coordinador, "j1"))
            self.assertEqual(coordinador.recuperar(), (), "recuperar dos veces no hace nada nuevo")

    def test_un_huerfano_no_bloquea_la_capacidad_de_sus_hermanos(self):
        """Un activo sin worker seguiria ocupando su clase de recurso.

        Con un limite de una CPU, el hermano encolado no arrancaria nunca: es la
        forma exacta en que la cola se queda muerta sin que nada parezca roto.
        """
        self.arrancar_y_abandonar("zombi", {"pasos": 40, "retardo": 0.05, "checkpoint_cada": 1})
        with self.sesion(limites=Recursos(cpu=1)) as coordinador:
            coordinador.encolar(self.job("hermano", payload={"pasos": 1}))
            self.correr(coordinador)
            self.assertEqual(self.estado(coordinador, "hermano").estado, EstadoJob.SUCCEEDED)
            self.assertEqual(self.estado(coordinador, "zombi").estado, EstadoJob.SUCCEEDED)

    def test_un_job_de_un_solo_intento_reanuda_tras_una_interrupcion(self):
        """Sin devolucion de presupuesto este job quedaria encolado para siempre."""
        proyecto = abrir_proyecto(self.raiz)
        coordinador = crear_coordinador(proyecto, catalogo=CATALOGO)
        try:
            # `max_intentos=1` es el valor por defecto de un job.
            coordinador.encolar(self.job("j1", payload={"pasos": 8, "retardo": 0.05,
                                                        "checkpoint_cada": 1}))
            limite = time.monotonic() + LIMITE
            while time.monotonic() < limite and not self.estado(coordinador, "j1").checkpoint:
                coordinador.paso()
                time.sleep(0.01)
            self.assertTrue(self.estado(coordinador, "j1").checkpoint)
        finally:
            coordinador.cerrar()
            proyecto.close()
        with self.sesion() as segundo:
            self.assertEqual(self.estado(segundo, "j1").estado, EstadoJob.QUEUED)
            self.assertTrue(segundo.ejecutar(limite_segundos=LIMITE),
                            "un job de un solo intento reanudado debe poder arrancar")
            self.assertEqual(self.estado(segundo, "j1").estado, EstadoJob.SUCCEEDED)
            self.assertEqual(self.artefacto("j1").read_text(encoding="utf-8"),
                             "".join(f"ok:{paso}\n" for paso in range(8)))

    def test_la_reanudacion_parte_del_checkpoint_y_no_repite_lo_hecho(self):
        self.arrancar_y_abandonar("j1", {"pasos": 6, "retardo": 0.05, "checkpoint_cada": 1})
        with self.sesion() as coordinador:
            checkpoint = dict(self.estado(coordinador, "j1").checkpoint)
            self.correr(coordinador)
            job = self.estado(coordinador, "j1")
            self.assertEqual(job.estado, EstadoJob.SUCCEEDED)
            self.assertEqual([intento.numero for intento in coordinador.intentos("j1")], [1, 2],
                             "la reanudacion crea un intento nuevo y numerado")
            self.assertEqual(job.intentos_usados, 1,
                             "la interrupcion devolvio el intento que habia cobrado")
            reanudado = coordinador.eventos("j1")[-3:]
            self.assertTrue(self.artefacto("j1").is_file())
            self.assertEqual(self.artefacto("j1").read_text(encoding="utf-8"),
                             "".join(f"ok:{paso}\n" for paso in range(6)),
                             "reanudar no puede perder ni duplicar el trabajo previo")
            self.assertGreaterEqual(int(checkpoint["paso"]), 1)
            self.assertTrue(reanudado, "la reanudacion debe dejar rastro consultable")

    def test_una_etapa_sin_checkpoint_reanuda_creando_un_intento_nuevo(self):
        sin_checkpoint = StageDefinition(nombre="prueba", version_contrato="1",
                                         soporta_checkpoint=False,
                                         politica_retry=PoliticaRetry(max_intentos=3))
        self.arrancar_y_abandonar("j1", {"pasos": 4, "retardo": 0.05, "checkpoint_cada": 1})
        proyecto = abrir_proyecto(self.raiz)
        coordinador = crear_coordinador(proyecto, catalogo={"prueba": sin_checkpoint})
        try:
            self.correr(coordinador)
            self.assertEqual(self.estado(coordinador, "j1").estado, EstadoJob.SUCCEEDED)
            self.assertEqual(self.artefacto("j1").read_text(encoding="utf-8"),
                             "".join(f"ok:{paso}\n" for paso in range(4)),
                             "sin checkpoint se repite la etapa entera, no el pipeline")
        finally:
            coordinador.cerrar()
            proyecto.close()

    def test_agotado_el_credito_de_reanudacion_el_job_queda_fallido(self):
        self.arrancar_y_abandonar("j1", {"pasos": 20, "retardo": 0.05, "checkpoint_cada": 1})
        proyecto = abrir_proyecto(self.raiz)
        try:  # se agota el credito antes de que exista coordinador alguno
            proyecto.repositorio.conexion.execute(
                "UPDATE Job SET interruptions_used=max_interruptions WHERE id='j1'")
            proyecto.repositorio.conexion.commit()
        finally:
            proyecto.close()
        with self.sesion() as coordinador:
            job = self.estado(coordinador, "j1")
            self.assertEqual((job.estado, job.error_codigo),
                             (EstadoJob.FAILED, "interrumpido"))

    def test_un_coordinador_muerto_de_golpe_deja_un_proyecto_reabrible(self):
        for repeticion in range(REPETICIONES):
            with self.subTest(repeticion=repeticion):
                identificador = f"j{repeticion}"
                proceso = subprocess.run(
                    [sys.executable, "-c", HIJO_MUERE_CON_JOB_VIVO, str(self.raiz),
                     identificador, "6"],
                    stdout=subprocess.PIPE, text=True, env=ENTORNO, timeout=LIMITE)
                self.assertEqual((proceso.returncode, proceso.stdout.strip()), (17, "VIVO"))
                with self.sesion() as coordinador:
                    self.assertEqual(self.estado(coordinador, identificador).estado,
                                     EstadoJob.QUEUED, "reabrir ya recupero el huerfano")
                    self.correr(coordinador)
                    job = self.estado(coordinador, identificador)
                    self.assertEqual(job.estado, EstadoJob.SUCCEEDED)
                    self.assertEqual(self.artefacto(identificador).read_text(encoding="utf-8"),
                                     "".join(f"ok:{paso}\n" for paso in range(6)))
                    self.assertEqual(
                        self.conexion.execute(
                            "PRAGMA integrity_check").fetchone()[0], "ok")

    def test_un_proyecto_read_only_no_puede_coordinar(self):
        with self.sesion():
            segundo = abrir_proyecto(self.raiz)
            self.addCleanup(segundo.close)
            self.assertTrue(segundo.solo_lectura)
            with self.assertRaises(ErrorJob):
                crear_coordinador(segundo, catalogo=CATALOGO)

    def test_recuperar_sin_huerfanos_no_altera_nada(self):
        with self.sesion() as coordinador:
            coordinador.encolar(self.job("j1", payload={"pasos": 1}))
            self.correr(coordinador)
            antes = coordinador.eventos()
            self.assertEqual(coordinador.recuperar(), ())
            self.assertEqual(coordinador.eventos(), antes)
            self.assertEqual(self.estado(coordinador, "j1").estado, EstadoJob.SUCCEEDED)


# --------------------------------------------------------------------------- #
# Leases vistos desde el coordinador
# --------------------------------------------------------------------------- #

class LeaseCoordinadorTests(BaseCoordinador):
    def test_el_coordinador_renueva_su_lease_mientras_el_worker_vive(self):
        with self.sesion(duracion_lease=1.0) as coordinador:
            coordinador.encolar(self.job("j1", payload={"pasos": 20, "retardo": 0.05}))
            vencimientos = set()
            limite = time.monotonic() + LIMITE
            while coordinador.paso():
                intentos = coordinador.intentos("j1")
                if intentos and intentos[0].lease_expira_en is not None:
                    vencimientos.add(round(intentos[0].lease_expira_en, 3))
                self.assertLess(time.monotonic(), limite)
                time.sleep(0.01)
            self.assertGreater(len(vencimientos), 1, "un lease corto obliga a renovarlo")
            self.assertEqual(self.estado(coordinador, "j1").estado, EstadoJob.SUCCEEDED)

    def test_perder_el_lease_suelta_el_worker_sin_cerrar_el_intento_ajeno(self):
        with self.sesion(duracion_lease=1.0) as coordinador:
            coordinador.encolar(self.job("j1", payload={"pasos": 40, "retardo": 0.05}))
            coordinador.paso()
            intento = coordinador.intentos("j1")[0]
            # Otro duenio se apropia del intento: la renovacion debe fallar.
            self.conexion.execute(
                "UPDATE JobAttempt SET lease_owner='intruso' WHERE id=?", (intento.id,))
            self.conexion.commit()
            limite = time.monotonic() + LIMITE
            while coordinador.resumen().en_curso and time.monotonic() < limite:
                coordinador.paso()
                time.sleep(0.05)
            self.assertEqual(coordinador.resumen().en_curso, ())
            intento_final = coordinador.intentos("j1")[0]
            self.assertEqual(intento_final.lease_owner, "intruso",
                             "no se cierra ni se reescribe el intento de otro")
            self.assertEqual(self.estado(coordinador, "j1").estado, EstadoJob.RUNNING)


if __name__ == "__main__":
    unittest.main()
