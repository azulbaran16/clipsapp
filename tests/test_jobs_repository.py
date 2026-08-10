"""Contrato durable del repositorio de trabajos: leases, transiciones y eventos.

Estas pruebas no lanzan workers: aislan la capa que decide *quien puede escribir
que*. Los procesos reales viven en `test_jobs_coordinator`.
"""

import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipperkick.domain.jobs import (  # noqa: E402
    CODIGO_INTERRUMPIDO, ErrorJob, ErrorLeaseJob, ErrorTransicionJob, EstadoJob, EventoProgreso,
    Job, Recursos, TipoEvento,
)
from clipperkick.domain.project import ErrorProyecto  # noqa: E402
from clipperkick.infrastructure.jobs import RepositorioSqliteJobs  # noqa: E402
from clipperkick.infrastructure.project import abrir_proyecto, crear_proyecto  # noqa: E402
from clipperkick.infrastructure.project.persistence import (  # noqa: E402
    DIRECTORIOS_PROYECTO, NOMBRE_BASE, NOMBRE_MANIFIESTO, VERSION_ESQUEMA, _conexion, _sql_v1,
    _sql_v2,
)


DUENIO = "coordinador-a"
OTRO = "coordinador-b"


def job(identificador="j1", **claves):
    base = dict(stage="prueba", version_contrato="1")
    base.update(claves)
    return Job(id=identificador, **base)


def historia(job_id="j1", clave="prueba", tipo=None):
    """Evento minimo para los comandos que exigen historia.

    Las pruebas de este modulo no observan el contenido del evento salvo cuando
    lo dicen explicitamente; lo que el contrato exige es que exista.
    """
    return EventoProgreso(job_id=job_id, tipo=tipo or TipoEvento.FIN, instante=1.0, clave=clave)


def historias(job_id="j1", clave="prueba", tipo=None):
    return (historia(job_id, clave, tipo),)


class BaseRepositorio(unittest.TestCase):
    def setUp(self):
        self.temporal = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporal.cleanup)
        self.base = Path(self.temporal.name)
        self.raiz = self.base / "demo.clipsapp"
        self.proyecto = crear_proyecto(self.raiz, "Demo")
        self.addCleanup(self.cerrar_proyecto)
        self.repositorio = RepositorioSqliteJobs(self.proyecto.repositorio)

    def cerrar_proyecto(self):
        try:
            self.proyecto.close()
        except Exception:  # ya cerrado por la propia prueba
            pass

    def arrancado(self, identificador="j1", ahora=1000.0, duracion=60.0, **claves):
        self.repositorio.encolar(job(identificador, **claves), evento=historia(identificador))
        return self.repositorio.adquirir_lease(identificador, DUENIO, duracion, ahora,
                                               evento=historia(identificador))


# --------------------------------------------------------------------------- #
# Registro y lectura
# --------------------------------------------------------------------------- #

class RegistroTests(BaseRepositorio):
    def test_un_job_sobrevive_ida_y_vuelta_con_todos_sus_campos(self):
        original = job("j1", clave_materializacion="clave-1", recursos=Recursos(cpu=2, gpu=1),
                       prioridad=7, max_intentos=4, max_interrupciones=2,
                       payload={"pasos": 3, "texto": "áé"})
        self.repositorio.encolar(original, evento=historia())
        recuperado = self.repositorio.obtener("j1")
        self.assertEqual(
            (recuperado.stage, recuperado.version_contrato, recuperado.clave_materializacion,
             recuperado.recursos, recuperado.prioridad, recuperado.max_intentos,
             recuperado.max_interrupciones, dict(recuperado.payload), recuperado.estado),
            ("prueba", "1", "clave-1", Recursos(cpu=2, gpu=1), 7, 4, 2,
             {"pasos": 3, "texto": "áé"}, EstadoJob.QUEUED))

    def test_las_dependencias_se_guardan_como_aristas_consultables(self):
        self.repositorio.encolar(job("a"), evento=historia())
        self.repositorio.encolar(job("b"), evento=historia())
        self.repositorio.encolar(job("c", dependencias=("a", "b")), evento=historia("c"))
        self.assertEqual(self.repositorio.obtener("c").dependencias, ("a", "b"))
        filas = self.proyecto.repositorio.conexion.execute(
            "SELECT job_id, depends_on FROM JobDependency ORDER BY depends_on").fetchall()
        self.assertEqual(filas, [("c", "a"), ("c", "b")])

    def test_una_dependencia_inexistente_la_rechaza_la_base(self):
        with self.assertRaises(ErrorJob):
            self.repositorio.encolar(job("c", dependencias=("fantasma",)), evento=historia("c"))
        self.assertEqual(self.repositorio.listar(), (), "un encolado fallido no deja el job a medias")

    def test_un_job_repetido_no_se_duplica(self):
        self.repositorio.encolar(job("j1"), evento=historia())
        with self.assertRaises(ErrorJob):
            self.repositorio.encolar(job("j1"), evento=historia())
        self.assertEqual(len(self.repositorio.listar()), 1)

    def test_solo_se_registran_jobs_encolados(self):
        with self.assertRaises(ErrorJob):
            self.repositorio.encolar(job("j1", estado=EstadoJob.RUNNING), evento=historia())

    def test_obtener_un_job_inexistente_falla_tipado(self):
        with self.assertRaises(ErrorJob):
            self.repositorio.obtener("no-existe")


# --------------------------------------------------------------------------- #
# Leases
# --------------------------------------------------------------------------- #

class LeaseTests(BaseRepositorio):
    def test_adquirir_pasa_a_running_y_crea_el_intento(self):
        intento = self.arrancado()
        self.assertEqual(intento.numero, 1)
        self.assertEqual(intento.lease_owner, DUENIO)
        job_actual = self.repositorio.obtener("j1")
        self.assertEqual((job_actual.estado, job_actual.intentos_usados), (EstadoJob.RUNNING, 1))
        self.assertEqual(len(self.repositorio.intentos("j1")), 1)

    def test_un_segundo_arrendatario_no_puede_arrancar_lo_ya_arrancado(self):
        self.arrancado()
        with self.assertRaises(ErrorLeaseJob):
            self.repositorio.adquirir_lease("j1", OTRO, 60.0, 1000.0, evento=historia())
        self.assertEqual(len(self.repositorio.intentos("j1")), 1,
                         "un arranque rechazado no puede dejar un intento fantasma")

    def test_no_se_arranca_un_job_cancelado_ni_uno_sin_presupuesto(self):
        self.repositorio.encolar(job("cancelado"), evento=historia())
        self.repositorio.solicitar_cancelacion("cancelado", evento=historia())
        with self.assertRaises(ErrorLeaseJob):
            self.repositorio.adquirir_lease("cancelado", DUENIO, 60.0, 1000.0, evento=historia())

        intento = self.arrancado("agotado", max_intentos=1)
        self.repositorio.finalizar(intento.id, DUENIO, EstadoJob.FAILED, 1001.0, error_codigo="x", eventos=historias())
        self.repositorio.reprogramar("agotado", 1002.0, eventos=historias())
        with self.assertRaises(ErrorLeaseJob):
            self.repositorio.adquirir_lease("agotado", DUENIO, 60.0, 1003.0, evento=historia())

    def test_toda_mutacion_exige_al_arrendatario_vigente(self):
        intento = self.arrancado()
        mutaciones = (
            ("renovar", lambda duenio, ahora: self.repositorio.renovar_lease(intento.id, duenio, 60.0, ahora)),
            ("pid", lambda duenio, ahora: self.repositorio.registrar_pid(intento.id, duenio, 42, ahora)),
            ("checkpoint", lambda duenio, ahora: self.repositorio.registrar_checkpoint(intento.id, duenio, {"p": 1}, ahora)),
            ("estado", lambda duenio, ahora: self.repositorio.marcar_estado(intento.id, duenio, EstadoJob.VALIDATING, ahora, eventos=historias())),
            ("finalizar", lambda duenio, ahora: self.repositorio.finalizar(intento.id, duenio, EstadoJob.FAILED, ahora, eventos=historias())),
        )
        for nombre, mutacion in mutaciones:
            with self.subTest(mutacion=nombre, motivo="duenio ajeno"):
                with self.assertRaises(ErrorLeaseJob):
                    mutacion(OTRO, 1001.0)
            with self.subTest(mutacion=nombre, motivo="lease vencido"):
                with self.assertRaises(ErrorLeaseJob):
                    mutacion(DUENIO, 9999.0)
        self.assertEqual(self.repositorio.obtener("j1").estado, EstadoJob.RUNNING,
                         "ninguna mutacion rechazada pudo cambiar el estado")

    def test_renovar_extiende_la_vigencia_y_permite_seguir_escribiendo(self):
        intento = self.arrancado(duracion=10.0)
        with self.assertRaises(ErrorLeaseJob):
            self.repositorio.registrar_pid(intento.id, DUENIO, 1, 1020.0)
        self.repositorio.renovar_lease(intento.id, DUENIO, 60.0, 1005.0)
        self.repositorio.registrar_pid(intento.id, DUENIO, 1, 1020.0)
        self.assertEqual(self.repositorio.intentos("j1")[0].pid, 1)

    def test_cerrar_el_intento_suelta_el_lease_y_lo_vuelve_inmutable(self):
        intento = self.arrancado()
        self.repositorio.finalizar(intento.id, DUENIO, EstadoJob.FAILED, 1001.0, error_codigo="x", eventos=historias())
        self.assertIsNone(self.repositorio.intentos("j1")[0].lease_owner)
        with self.assertRaises(ErrorLeaseJob):
            self.repositorio.registrar_checkpoint(intento.id, DUENIO, {"p": 1}, 1002.0)

    def test_un_intento_inexistente_no_se_confunde_con_uno_ajeno(self):
        with self.assertRaises(ErrorJob) as capturado:
            self.repositorio.marcar_estado("no-existe", DUENIO, EstadoJob.VALIDATING, 1.0, eventos=historias())
        self.assertNotIsInstance(capturado.exception, ErrorLeaseJob)


# --------------------------------------------------------------------------- #
# Transiciones
# --------------------------------------------------------------------------- #

class TransicionTests(BaseRepositorio):
    def test_el_camino_feliz_pasa_por_validating(self):
        intento = self.arrancado()
        self.repositorio.marcar_estado(intento.id, DUENIO, EstadoJob.VALIDATING, 1001.0, eventos=historias())
        self.repositorio.finalizar(intento.id, DUENIO, EstadoJob.SUCCEEDED, 1002.0,
                                   artefactos=(("artifacts/j1/s.txt", "prueba"),), eventos=historias())
        self.assertEqual(self.repositorio.obtener("j1").estado, EstadoJob.SUCCEEDED)
        self.assertEqual(
            self.proyecto.repositorio.conexion.execute(
                "SELECT relative_path, kind, state FROM AnalysisArtifact").fetchall(),
            [("artifacts/j1/s.txt", "prueba", "available")])

    def test_no_se_puede_saltar_a_succeeded_desde_running(self):
        intento = self.arrancado()
        with self.assertRaises(ErrorTransicionJob):
            self.repositorio.finalizar(intento.id, DUENIO, EstadoJob.SUCCEEDED, 1001.0, eventos=historias())
        self.assertEqual(self.repositorio.obtener("j1").estado, EstadoJob.RUNNING)

    def test_finalizar_no_admite_estados_que_no_cierran(self):
        intento = self.arrancado()
        for destino in (EstadoJob.QUEUED, EstadoJob.RUNNING, EstadoJob.VALIDATING,
                        EstadoJob.CANCEL_REQUESTED):
            with self.subTest(destino=destino):
                with self.assertRaises(ErrorTransicionJob):
                    self.repositorio.finalizar(intento.id, DUENIO, destino, 1001.0, eventos=historias())

    def test_el_artefacto_y_el_exito_se_escriben_o_se_deshacen_juntos(self):
        intento = self.arrancado()
        self.repositorio.marcar_estado(intento.id, DUENIO, EstadoJob.VALIDATING, 1001.0, eventos=historias())
        conexion = self.proyecto.repositorio.conexion
        conexion.execute("CREATE TRIGGER inyectado BEFORE INSERT ON AnalysisArtifact"
                         " BEGIN SELECT RAISE(ABORT, 'inyectado'); END")
        conexion.commit()
        with self.assertRaises(ErrorJob):
            self.repositorio.finalizar(intento.id, DUENIO, EstadoJob.SUCCEEDED, 1002.0,
                                       artefactos=(("artifacts/j1/s.txt", "prueba"),), eventos=historias())
        self.assertEqual(self.repositorio.obtener("j1").estado, EstadoJob.VALIDATING,
                         "el proyecto no puede afirmar un exito cuyo artefacto no se registro")
        self.assertEqual(conexion.execute("SELECT COUNT(*) FROM AnalysisArtifact").fetchone()[0], 0)

    def test_reprogramar_solo_desde_un_estado_que_lo_admite(self):
        intento = self.arrancado()
        with self.assertRaises(ErrorTransicionJob):
            self.repositorio.reprogramar("j1", 1001.0, eventos=historias())
        self.repositorio.finalizar(intento.id, DUENIO, EstadoJob.FAILED, 1002.0, error_codigo="x", eventos=historias())
        self.assertTrue(self.repositorio.reprogramar("j1", 1003.0, eventos=historias()))
        job_actual = self.repositorio.obtener("j1")
        self.assertEqual((job_actual.estado, job_actual.error_codigo), (EstadoJob.QUEUED, None))

    def test_un_reintento_no_gasta_credito_de_interrupcion_y_viceversa(self):
        """Un fallo cobra intentos; una interrupcion los devuelve y cobra los suyos."""
        intento = self.arrancado("j1", max_intentos=3)
        self.repositorio.finalizar(intento.id, DUENIO, EstadoJob.FAILED, 1001.0, error_codigo="x", eventos=historias())
        self.repositorio.reprogramar("j1", 1002.0, eventos=historias())
        tras_el_fallo = self.repositorio.obtener("j1")
        self.assertEqual((tras_el_fallo.interrupciones_usadas, tras_el_fallo.intentos_usados),
                         (0, 1))

        self.repositorio.adquirir_lease("j1", DUENIO, 60.0, 1003.0, evento=historia())
        self.repositorio.recuperar_huerfanos(OTRO, 1004.0, evento=lambda j, a: historia(j))
        self.repositorio.reprogramar("j1", 1005.0, eventos=historias())
        job_actual = self.repositorio.obtener("j1")
        self.assertEqual((job_actual.interrupciones_usadas, job_actual.intentos_usados), (1, 1),
                         "la interrupcion devuelve el intento que habia cobrado al arrancar")
        self.assertEqual([intento.numero for intento in self.repositorio.intentos("j1")], [1, 2],
                         "la numeracion no puede reutilizarse al devolver presupuesto")

    def test_un_job_de_un_solo_intento_sobrevive_a_una_interrupcion(self):
        """El caso que dejaba la cola atascada para siempre.

        Sin devolucion de presupuesto el job vuelve a `queued` pero no puede
        adquirir lease jamas: `ejecutar()` giraria en vacio sin avanzar nada.
        """
        self.arrancado("j1", max_intentos=1)
        self.repositorio.recuperar_huerfanos(OTRO, 1001.0, evento=lambda j, a: historia(j))
        self.repositorio.reprogramar("j1", 1002.0, eventos=historias())
        self.assertEqual(self.repositorio.obtener("j1").estado, EstadoJob.QUEUED)
        segundo = self.repositorio.adquirir_lease("j1", DUENIO, 60.0, 1003.0, evento=historia())
        self.assertEqual(segundo.numero, 2)
        self.repositorio.marcar_estado(segundo.id, DUENIO, EstadoJob.VALIDATING, 1004.0, eventos=historias())
        self.repositorio.finalizar(segundo.id, DUENIO, EstadoJob.SUCCEEDED, 1005.0, eventos=historias())
        self.assertEqual(self.repositorio.obtener("j1").estado, EstadoJob.SUCCEEDED)

    def test_el_credito_de_interrupciones_es_finito(self):
        self.repositorio.encolar(job("j1", max_intentos=1, max_interrupciones=2), evento=historia())
        for vuelta in range(2):
            self.repositorio.adquirir_lease("j1", DUENIO, 60.0, 1000.0 + vuelta, evento=historia())
            self.repositorio.recuperar_huerfanos(OTRO, 1010.0 + vuelta, evento=lambda j, a: historia(j))
            self.repositorio.reprogramar("j1", 1020.0 + vuelta, eventos=historias())
        job_actual = self.repositorio.obtener("j1")
        self.assertEqual((job_actual.interrupciones_usadas, job_actual.reanudaciones_restantes),
                         (2, 0))
        self.assertEqual([intento.numero for intento in self.repositorio.intentos("j1")], [1, 2])

    def test_una_interrupcion_no_regala_reintentos_transitorios(self):
        """Tras reanudar, el presupuesto de fallos sigue siendo el declarado."""
        self.arrancado("j1", max_intentos=1)
        self.repositorio.recuperar_huerfanos(OTRO, 1001.0, evento=lambda j, a: historia(j))
        self.repositorio.reprogramar("j1", 1002.0, eventos=historias())
        segundo = self.repositorio.adquirir_lease("j1", DUENIO, 60.0, 1003.0, evento=historia())
        self.repositorio.finalizar(segundo.id, DUENIO, EstadoJob.FAILED, 1004.0,
                                   error_codigo="transitorio.io", eventos=historias())
        self.repositorio.reprogramar("j1", 1005.0, eventos=historias())
        self.assertEqual(self.repositorio.obtener("j1").intentos_restantes, 0)
        with self.assertRaises(ErrorLeaseJob):
            self.repositorio.adquirir_lease("j1", DUENIO, 60.0, 1006.0, evento=historia())

    def test_cancelar_es_idempotente_y_no_alcanza_lo_ya_terminado(self):
        intento = self.arrancado()
        self.assertTrue(self.repositorio.solicitar_cancelacion("j1", evento=historia()))
        self.assertFalse(self.repositorio.solicitar_cancelacion("j1", evento=historia()))
        self.repositorio.marcar_estado(intento.id, DUENIO, EstadoJob.CANCEL_REQUESTED, 1001.0, eventos=historias())
        self.repositorio.finalizar(intento.id, DUENIO, EstadoJob.FAILED, 1002.0,
                                   error_codigo="cancelado", eventos=historias())
        self.repositorio.encolar(job("terminado"), evento=historia())
        self.repositorio.marcar_fallo_directo("terminado", 1003.0, "x", eventos=historias())
        self.assertFalse(self.repositorio.solicitar_cancelacion("terminado", evento=historia()))


# --------------------------------------------------------------------------- #
# Recuperacion de huerfanos
# --------------------------------------------------------------------------- #

class RecuperacionTests(BaseRepositorio):
    def test_un_intento_de_otra_sesion_queda_interrumpido(self):
        self.arrancado("j1")
        interrumpidos = self.repositorio.recuperar_huerfanos(OTRO, 1001.0, evento=lambda j, a: historia(j))
        self.assertEqual(interrumpidos, ("j1",))
        job_actual = self.repositorio.obtener("j1")
        self.assertEqual((job_actual.estado, job_actual.error_codigo),
                         (EstadoJob.INTERRUPTED, CODIGO_INTERRUMPIDO))
        self.assertEqual(self.repositorio.intentos("j1")[0].estado, EstadoJob.INTERRUPTED)
        self.assertIsNone(self.repositorio.intentos("j1")[0].lease_owner)

    def test_los_tres_estados_vivos_se_recuperan(self):
        for identificador, destino in (("corriendo", None), ("validando", EstadoJob.VALIDATING),
                                       ("cancelando", EstadoJob.CANCEL_REQUESTED)):
            intento = self.arrancado(identificador)
            if destino is not None:
                self.repositorio.marcar_estado(intento.id, DUENIO, destino, 1001.0, eventos=historias())
        self.assertEqual(set(self.repositorio.recuperar_huerfanos(OTRO, 1002.0, evento=lambda j, a: historia(j))),
                         {"corriendo", "validando", "cancelando"})

    def test_la_propia_sesion_con_lease_vigente_no_se_interrumpe(self):
        self.arrancado("j1", duracion=600.0)
        self.assertEqual(self.repositorio.recuperar_huerfanos(DUENIO, 1001.0, evento=lambda j, a: historia(j)), ())
        self.assertEqual(self.repositorio.obtener("j1").estado, EstadoJob.RUNNING)

    def test_un_lease_vencido_de_la_propia_sesion_si_se_recupera(self):
        self.arrancado("j1", duracion=10.0)
        self.assertEqual(self.repositorio.recuperar_huerfanos(DUENIO, 9999.0, evento=lambda j, a: historia(j)), ("j1",))

    def test_lo_ya_terminado_no_se_toca(self):
        intento = self.arrancado("terminado")
        self.repositorio.marcar_estado(intento.id, DUENIO, EstadoJob.VALIDATING, 1001.0, eventos=historias())
        self.repositorio.finalizar(intento.id, DUENIO, EstadoJob.SUCCEEDED, 1002.0, eventos=historias())
        self.repositorio.encolar(job("encolado"), evento=historia())
        self.assertEqual(self.repositorio.recuperar_huerfanos(OTRO, 1003.0, evento=lambda j, a: historia(j)), ())
        self.assertEqual(self.repositorio.obtener("terminado").estado, EstadoJob.SUCCEEDED)
        self.assertEqual(self.repositorio.obtener("encolado").estado, EstadoJob.QUEUED)

    def test_recuperar_es_idempotente(self):
        self.arrancado("j1")
        self.assertEqual(self.repositorio.recuperar_huerfanos(OTRO, 1001.0, evento=lambda j, a: historia(j)), ("j1",))
        self.assertEqual(self.repositorio.recuperar_huerfanos(OTRO, 1002.0, evento=lambda j, a: historia(j)), ())

    def test_el_checkpoint_del_intento_muerto_queda_disponible_en_el_job(self):
        intento = self.arrancado("j1")
        self.repositorio.registrar_checkpoint(intento.id, DUENIO, {"paso": 2}, 1001.0)
        self.repositorio.recuperar_huerfanos(OTRO, 1002.0, evento=lambda j, a: historia(j))
        self.assertEqual(dict(self.repositorio.obtener("j1").checkpoint), {"paso": 2},
                         "reanudar es decision del job, no del intento que murio")


# --------------------------------------------------------------------------- #
# Eventos
# --------------------------------------------------------------------------- #

class EventoTests(BaseRepositorio):
    def evento(self, job_id, tipo=TipoEvento.PROGRESO, **claves):
        return self.repositorio.registrar_evento(
            EventoProgreso(job_id=job_id, tipo=tipo, instante=claves.pop("instante", 1.0),
                           **claves))

    def test_los_eventos_son_consultables_sin_parsear_texto(self):
        self.repositorio.encolar(job("j1"), evento=historia())
        registrado = self.evento("j1", clave="stage.paso", unidades_hechas=1, unidades_totales=4,
                                 datos={"codigo": "x"})
        # El alta del job ya ocupa la primera secuencia: el contrato es que la
        # secuencia crece, no que empiece en un numero concreto.
        self.assertEqual(registrado.secuencia, 2)
        recuperado = self.repositorio.eventos("j1")[1]
        self.assertEqual((recuperado.tipo, recuperado.clave, recuperado.unidades_hechas,
                          recuperado.unidades_totales, dict(recuperado.datos)),
                         (TipoEvento.PROGRESO, "stage.paso", 1.0, 4.0, {"codigo": "x"}))
        self.assertAlmostEqual(recuperado.fraccion, 0.25)

    def test_la_secuencia_es_monotona_y_permite_leer_solo_lo_nuevo(self):
        self.repositorio.encolar(job("j1"), evento=historia())
        secuencias = [self.evento("j1").secuencia for _ in range(5)]
        self.assertEqual(secuencias, sorted(secuencias))
        self.assertEqual(len(self.repositorio.eventos("j1", desde=secuencias[2])), 2)

    def test_el_filtro_por_job_no_mezcla_historias(self):
        # Cada alta deja ya su propio evento; lo que se comprueba aqui es que el
        # filtro separa historias, no cuantas hay en total.
        self.repositorio.encolar(job("a"), evento=historia("a", clave="alta"))
        self.repositorio.encolar(job("b"), evento=historia("b", clave="alta"))
        self.evento("a")
        self.evento("b")
        self.evento("b")

        def propios(job_id):
            return [evento for evento in self.repositorio.eventos(job_id)
                    if evento.clave != "alta"]

        self.assertEqual(len(propios("a")), 1)
        self.assertEqual(len(propios("b")), 2)
        self.assertEqual(len(self.repositorio.eventos()), 5)

    def test_un_evento_de_job_inexistente_lo_rechaza_la_base(self):
        with self.assertRaises(ErrorJob):
            self.evento("fantasma")


# --------------------------------------------------------------------------- #
# Persistencia entre sesiones
# --------------------------------------------------------------------------- #

class PersistenciaTests(BaseRepositorio):
    def test_jobs_intentos_dependencias_y_eventos_sobreviven_a_cerrar_y_reabrir(self):
        self.repositorio.encolar(job("a", payload={"pasos": 2}), evento=historia())
        self.repositorio.encolar(job("b", dependencias=("a",), prioridad=3), evento=historia("b"))
        intento = self.repositorio.adquirir_lease("a", DUENIO, 60.0, 1000.0, evento=historia())
        self.repositorio.registrar_checkpoint(intento.id, DUENIO, {"paso": 1}, 1001.0)
        self.repositorio.registrar_evento(
            EventoProgreso(job_id="a", tipo=TipoEvento.PROGRESO, instante=1.0, clave="k",
                           unidades_hechas=1, unidades_totales=2))
        self.proyecto.close()

        with abrir_proyecto(self.raiz) as reabierto:
            repositorio = RepositorioSqliteJobs(reabierto.repositorio)
            a, b = repositorio.obtener("a"), repositorio.obtener("b")
            self.assertEqual((a.estado, dict(a.checkpoint), dict(a.payload)),
                             (EstadoJob.RUNNING, {"paso": 1}, {"pasos": 2}))
            self.assertEqual((b.dependencias, b.prioridad), (("a",), 3))
            self.assertEqual(repositorio.intentos("a")[0].lease_owner, DUENIO)
            self.assertEqual([evento.clave for evento in repositorio.eventos("a")][-1], "k")

    def test_una_sesion_read_only_lee_pero_no_escribe(self):
        self.repositorio.encolar(job("a"), evento=historia())
        segundo = abrir_proyecto(self.raiz)
        self.addCleanup(segundo.close)
        repositorio = RepositorioSqliteJobs(segundo.repositorio)
        self.assertTrue(segundo.solo_lectura)
        self.assertEqual(repositorio.obtener("a").estado, EstadoJob.QUEUED)
        with self.assertRaises(ErrorJob):
            repositorio.adquirir_lease("a", OTRO, 60.0, 1000.0, evento=historia())
        with self.assertRaises(ErrorJob):
            repositorio.recuperar_huerfanos(OTRO, 1000.0, evento=lambda j, a: historia(j))
        self.assertEqual(self.repositorio.obtener("a").estado, EstadoJob.QUEUED)


# --------------------------------------------------------------------------- #
# Migracion v2 -> v3
# --------------------------------------------------------------------------- #

class MigracionV3Tests(unittest.TestCase):
    def setUp(self):
        self.temporal = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporal.cleanup)
        self.base = Path(self.temporal.name)
        self.raiz = self.base / "demo.clipsapp"

    def fixture_v2(self, con_job=True):
        """Proyecto en la version aceptada por T02, listo para migrar."""
        self.raiz.mkdir()
        for directorio in DIRECTORIOS_PROYECTO:
            (self.raiz / directorio).mkdir()
        (self.raiz / NOMBRE_MANIFIESTO).write_text(
            json.dumps({"project_id": "p2", "name": "Antiguo", "schema_version": 2}),
            encoding="utf-8")
        (self.raiz / NOMBRE_BASE).touch()
        conexion = _conexion(self.raiz / NOMBRE_BASE)
        try:
            _sql_v1(conexion)
            _sql_v2(conexion)
            conexion.execute("PRAGMA user_version=2")
            conexion.execute("INSERT INTO Project VALUES ('p2', 'Antiguo', '{}')")
            if con_job:
                conexion.execute("INSERT INTO Job VALUES ('viejo', 'ingest', 'queued', '{}')")
                conexion.execute("INSERT INTO JobAttempt VALUES ('i1', 'viejo', 'queued', NULL)")
            conexion.commit()
        finally:
            conexion.close()

    def test_migra_desde_la_version_aceptada_conservando_las_filas(self):
        self.fixture_v2()
        with abrir_proyecto(self.raiz) as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().schema_version, VERSION_ESQUEMA)
            repositorio = RepositorioSqliteJobs(proyecto.repositorio)
            migrado = repositorio.obtener("viejo")
            self.assertEqual((migrado.stage, migrado.estado, migrado.max_intentos),
                             ("ingest", EstadoJob.QUEUED, 1),
                             "una fila v2 debe quedar representable, no solo presente")
            self.assertEqual(len(repositorio.intentos("viejo")), 1)
            self.assertEqual(
                proyecto.repositorio.conexion.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_el_esquema_nuevo_trae_sus_tablas_columnas_e_indices(self):
        self.fixture_v2(con_job=False)
        with abrir_proyecto(self.raiz) as proyecto:
            conexion = proyecto.repositorio.conexion
            tablas = {fila[0] for fila in conexion.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"JobDependency", "JobEvent"} <= tablas)
            columnas = {fila[1] for fila in conexion.execute("PRAGMA table_info(Job)")}
            self.assertTrue({"stage", "attempts_used", "max_interruptions", "cancel_requested",
                             "checkpoint_json"} <= columnas)
            columnas = {fila[1] for fila in conexion.execute("PRAGMA table_info(JobAttempt)")}
            self.assertTrue({"lease_owner", "lease_expires_at", "attempt_number"} <= columnas)

    def test_una_migracion_fallida_restaura_el_proyecto_v2_intacto(self):
        self.fixture_v2()
        from clipperkick.domain.project import ErrorMigracionProyecto

        with self.assertRaises(ErrorMigracionProyecto):
            abrir_proyecto(self.raiz,
                           fallo_migracion=lambda _v: (_ for _ in ()).throw(RuntimeError("no")))
        conexion = _conexion(self.raiz / NOMBRE_BASE)
        try:
            self.assertEqual(conexion.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertEqual({fila[1] for fila in conexion.execute("PRAGMA table_info(Job)")},
                             {"id", "kind", "state", "payload_json"})
            self.assertEqual(conexion.execute("SELECT id FROM Job").fetchall(), [("viejo",)])
        finally:
            conexion.close()
        self.assertEqual(json.loads((self.raiz / NOMBRE_MANIFIESTO).read_text("utf-8"))
                         ["schema_version"], 2)
        with abrir_proyecto(self.raiz) as proyecto:
            self.assertEqual(proyecto.repositorio.informacion().schema_version, VERSION_ESQUEMA)

    def test_una_base_que_declara_v3_sin_sus_columnas_no_devuelve_repositorio(self):
        with crear_proyecto(self.raiz, "Demo"):
            pass
        conexion = _conexion(self.raiz / NOMBRE_BASE)
        try:
            conexion.execute("ALTER TABLE Job DROP COLUMN max_attempts")
            conexion.commit()
        except sqlite3.OperationalError:
            self.skipTest("este SQLite no admite DROP COLUMN")
        finally:
            conexion.close()
        with self.assertRaises(ErrorProyecto) as capturado:
            abrir_proyecto(self.raiz)
        self.assertIn("columnas", str(capturado.exception))


# --------------------------------------------------------------------------- #
# Nada del disco es entrada confiable
# --------------------------------------------------------------------------- #

class LecturaEstrictaTests(BaseRepositorio):
    def corromper(self, columna, valor, job_id="j1"):
        self.repositorio.encolar(job(job_id), evento=historia())
        self.proyecto.repositorio.conexion.execute(
            f"UPDATE Job SET {columna}=? WHERE id=?", (valor, job_id))
        self.proyecto.repositorio.conexion.commit()

    def test_un_documento_no_objeto_no_se_degrada_a_vacio(self):
        for columna in ("resources_json", "payload_json", "checkpoint_json"):
            for valor in ("[]", '"texto"', "3", "no es json", "{"):
                with self.subTest(columna=columna, valor=valor):
                    temporal = tempfile.TemporaryDirectory()
                    self.addCleanup(temporal.cleanup)
                    with self.subTest():
                        self.corromper(columna, valor, job_id=f"j{abs(hash((columna, valor)))}")
                        with self.assertRaises(ErrorJob):
                            self.repositorio.listar()
                    # Se deja el proyecto limpio para el siguiente vector. El
                    # evento va primero: ahora todo job encolado tiene historia
                    # y la clave foranea no admite borrarlo por delante.
                    self.proyecto.repositorio.conexion.execute("DELETE FROM JobEvent")
                    self.proyecto.repositorio.conexion.execute("DELETE FROM Job")
                    self.proyecto.repositorio.conexion.commit()

    def test_un_peso_de_recurso_invalido_no_se_convierte_en_cero(self):
        for documento in ('{"cpu": "x", "gpu": 0, "disk": 0, "network": 0}',
                          '{"cpu": true, "gpu": 0, "disk": 0, "network": 0}',
                          '{"cpu": 1.5, "gpu": 0, "disk": 0, "network": 0}',
                          '{"cpu": null, "gpu": 0, "disk": 0, "network": 0}',
                          '{"gpu": 0, "disk": 0, "network": 0}'):
            with self.subTest(documento=documento):
                self.corromper("resources_json", documento)
                with self.assertRaises(ErrorJob) as capturado:
                    self.repositorio.listar()
                self.assertNotIsInstance(capturado.exception, (ValueError, TypeError))
                # El evento del alta va primero: la clave foranea no admite
                # borrar el job por delante de su historia.
                self.proyecto.repositorio.conexion.execute("DELETE FROM JobEvent")
                self.proyecto.repositorio.conexion.execute("DELETE FROM Job")
                self.proyecto.repositorio.conexion.commit()

    def test_un_contador_no_entero_no_cruza_la_frontera(self):
        self.corromper("attempts_used", "muchos")
        with self.assertRaises(ErrorJob):
            self.repositorio.listar()

    def test_un_estado_desconocido_se_diagnostica(self):
        self.corromper("state", "inventado")
        with self.assertRaises(ErrorJob) as capturado:
            self.repositorio.listar()
        self.assertNotIsInstance(capturado.exception, ValueError)

    def test_un_evento_de_tipo_desconocido_se_diagnostica(self):
        self.repositorio.encolar(job("j1"), evento=historia())
        self.repositorio.registrar_evento(
            EventoProgreso(job_id="j1", tipo=TipoEvento.PROGRESO, instante=1.0))
        self.proyecto.repositorio.conexion.execute("UPDATE JobEvent SET kind='inventado'")
        self.proyecto.repositorio.conexion.commit()
        with self.assertRaises(ErrorJob):
            self.repositorio.eventos("j1")

    def test_un_pid_corrupto_no_filtra_un_error_crudo_al_listar_intentos(self):
        """SQLite acepta texto en una columna INTEGER: la lectura debe defenderse."""
        self.arrancado("j1")
        for valor in ("bad", "", "12.5"):
            with self.subTest(valor=valor):
                self.proyecto.repositorio.conexion.execute(
                    "UPDATE JobAttempt SET worker_pid=?", (valor,))
                self.proyecto.repositorio.conexion.commit()
                with self.assertRaises(ErrorJob) as capturado:
                    self.repositorio.intentos("j1")
                self.assertNotIsInstance(capturado.exception, (ValueError, TypeError))

    def test_un_lease_corrupto_tampoco_filtra_un_error_crudo(self):
        self.arrancado("j1")
        self.proyecto.repositorio.conexion.execute("UPDATE JobAttempt SET lease_expires_at='bad'")
        self.proyecto.repositorio.conexion.commit()
        with self.assertRaises(ErrorJob) as capturado:
            self.repositorio.intentos("j1")
        self.assertNotIsInstance(capturado.exception, (ValueError, TypeError))

    def test_un_instante_de_evento_corrupto_no_filtra_un_error_crudo(self):
        self.repositorio.encolar(job("j1"), evento=historia())
        self.repositorio.registrar_evento(
            EventoProgreso(job_id="j1", tipo=TipoEvento.PROGRESO, instante=1.0))
        for columna, valor in (("at", "bad"), ("units_done", "bad"), ("units_total", "bad"),
                               ("seq", None)):
            if columna == "seq":
                continue
            with self.subTest(columna=columna):
                self.proyecto.repositorio.conexion.execute(
                    f"UPDATE JobEvent SET {columna}=?", (valor,))
                self.proyecto.repositorio.conexion.commit()
                with self.assertRaises(ErrorJob) as capturado:
                    self.repositorio.eventos("j1")
                self.assertNotIsInstance(capturado.exception, (ValueError, TypeError))
                self.proyecto.repositorio.conexion.execute(
                    f"UPDATE JobEvent SET {columna}=1.0")
                self.proyecto.repositorio.conexion.commit()

    def test_un_job_intacto_sigue_leyendose_despues_de_los_vectores(self):
        self.repositorio.encolar(job("sano", recursos=Recursos(cpu=2)), evento=historia("sano"))
        self.assertEqual(self.repositorio.obtener("sano").recursos, Recursos(cpu=2))


# --------------------------------------------------------------------------- #
# T03-F03 Contrato del repositorio: la historia no es opcional
# --------------------------------------------------------------------------- #

class ContratoDeHistoriaTests(BaseRepositorio):
    """La invariante se prueba contra la API cruda, no contra el coordinador.

    Mientras "estado + evento" fuese una costumbre del llamador, bastaba un
    caller nuevo que omitiese el argumento para reabrir el agujero. Aqui se
    comprueba lo contrario: que la omision no es expresable.
    """

    def comandos_de_estado(self):
        """Cada comando que cambia estado, con la llamada *sin* su historia."""
        intento = self.arrancado("conestado")
        segundo = self.arrancado("otro")
        self.repositorio.finalizar(segundo.id, DUENIO, EstadoJob.FAILED, 1001.0,
                                   eventos=historias("otro"), error_codigo="x")
        return (
            ("encolar", lambda: self.repositorio.encolar(job("nuevo"))),
            ("adquirir_lease", lambda: self.repositorio.adquirir_lease(
                "otro", DUENIO, 60.0, 1000.0)),
            ("marcar_estado", lambda: self.repositorio.marcar_estado(
                intento.id, DUENIO, EstadoJob.VALIDATING, 1002.0)),
            ("finalizar", lambda: self.repositorio.finalizar(
                intento.id, DUENIO, EstadoJob.FAILED, 1002.0)),
            ("solicitar_cancelacion", lambda: self.repositorio.solicitar_cancelacion("conestado")),
            ("reprogramar", lambda: self.repositorio.reprogramar("otro", 1002.0)),
            ("marcar_fallo_directo", lambda: self.repositorio.marcar_fallo_directo(
                "conestado", 1002.0, "x")),
            ("recuperar_huerfanos", lambda: self.repositorio.recuperar_huerfanos(OTRO, 9999.0)),
        )

    def test_omitir_la_historia_no_es_expresable(self):
        """La firma lo impide: el fallo es de llamada, antes de tocar la base."""
        for nombre, llamada in self.comandos_de_estado():
            with self.subTest(comando=nombre):
                with self.assertRaises(TypeError) as capturado:
                    llamada()
                self.assertIn("eventos" if "eventos" in str(capturado.exception) else "evento",
                              str(capturado.exception))

    def test_una_historia_vacia_falla_antes_de_abrir_transaccion(self):
        """La firma no impide pasar `()`, que es la misma omision escrita distinto."""
        intento = self.arrancado("vacio")
        vacios = (
            ("marcar_estado", lambda: self.repositorio.marcar_estado(
                intento.id, DUENIO, EstadoJob.VALIDATING, 1002.0, eventos=())),
            ("finalizar", lambda: self.repositorio.finalizar(
                intento.id, DUENIO, EstadoJob.FAILED, 1002.0, eventos=())),
            ("marcar_fallo_directo", lambda: self.repositorio.marcar_fallo_directo(
                "vacio", 1002.0, "x", eventos=())),
        )
        for nombre, llamada in vacios:
            with self.subTest(comando=nombre):
                with self.assertRaises(ErrorJob) as capturado:
                    llamada()
                self.assertIn("evento", str(capturado.exception))
                self.assertNotIsInstance(capturado.exception, ErrorLeaseJob)
        self.assertEqual(self.repositorio.obtener("vacio").estado, EstadoJob.RUNNING,
                         "ninguna llamada sin historia pudo cambiar el estado")
        self.assertEqual(self.repositorio.intentos("vacio")[0].estado, EstadoJob.RUNNING)

    def test_una_historia_que_no_son_eventos_se_rechaza(self):
        intento = self.arrancado("tipos")
        with self.assertRaises(ErrorJob):
            self.repositorio.finalizar(intento.id, DUENIO, EstadoJob.FAILED, 1002.0,
                                       eventos=("no soy un evento",))
        self.assertEqual(self.repositorio.obtener("tipos").estado, EstadoJob.RUNNING)

    def test_una_fabrica_de_eventos_esteril_deshace_la_interrupcion(self):
        """`recuperar_huerfanos` recibe una fabrica; si no produce, no hay transicion."""
        self.arrancado("huerfano")
        with self.assertRaises(ErrorJob):
            self.repositorio.recuperar_huerfanos(OTRO, 9999.0, evento=lambda j, a: None)
        job_actual = self.repositorio.obtener("huerfano")
        self.assertEqual(job_actual.estado, EstadoJob.RUNNING)
        self.assertEqual(job_actual.intentos_usados, 1,
                         "la devolucion de credito viaja en la misma transaccion")
        self.assertEqual(self.repositorio.intentos("huerfano")[0].lease_owner, DUENIO)

    def test_los_comandos_que_no_cambian_estado_no_exigen_historia(self):
        """No se fuerza historia donde no hay nada que explicar."""
        intento = self.arrancado("sinestado")
        self.repositorio.renovar_lease(intento.id, DUENIO, 60.0, 1001.0)
        self.repositorio.registrar_pid(intento.id, DUENIO, 4242, 1002.0)
        self.repositorio.registrar_checkpoint(intento.id, DUENIO, {"paso": 1}, 1003.0)
        self.assertEqual(self.repositorio.intentos("sinestado")[0].pid, 4242)
        self.assertEqual(dict(self.repositorio.obtener("sinestado").checkpoint), {"paso": 1})

    def test_el_evento_cuelga_de_la_entidad_que_el_comando_muto(self):
        """La identidad la fija el comando: un `job_id` ajeno no la contamina."""
        self.repositorio.encolar(job("duenio-real"),
                                 evento=historia("otro-cualquiera", clave="alta"))
        registrados = self.repositorio.eventos("duenio-real")
        self.assertEqual([evento.clave for evento in registrados], ["alta"])

        intento = self.repositorio.adquirir_lease("duenio-real", DUENIO, 60.0, 1000.0,
                                                  evento=historia("mentira", clave="arranque"))
        arranque = [evento for evento in self.repositorio.eventos("duenio-real")
                    if evento.clave == "arranque"]
        self.assertEqual(len(arranque), 1)
        self.assertEqual(arranque[0].attempt_id, intento.id,
                         "el evento debe quedar ligado al intento que nacio en esa transaccion")

    def test_un_fallo_al_insertar_la_historia_revierte_la_transicion(self):
        intento = self.arrancado("rollback")
        conexion = self.proyecto.repositorio.conexion
        conexion.execute("CREATE TRIGGER sin_eventos BEFORE INSERT ON JobEvent"
                         " BEGIN SELECT RAISE(ABORT, 'inyectado'); END")
        conexion.commit()
        try:
            with self.assertRaises(ErrorJob):
                self.repositorio.marcar_estado(intento.id, DUENIO, EstadoJob.VALIDATING, 1002.0,
                                               eventos=historias("rollback"))
            self.assertEqual(self.repositorio.obtener("rollback").estado, EstadoJob.RUNNING)
            self.assertEqual(self.repositorio.intentos("rollback")[0].estado, EstadoJob.RUNNING)
        finally:
            conexion.execute("DROP TRIGGER sin_eventos")
            conexion.commit()


if __name__ == "__main__":
    unittest.main()
