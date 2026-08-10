"""Reglas puras del coordinador: estados, recursos, DAG y protocolo.

Todo lo de aqui corre sin base de datos ni procesos. Si una de estas pruebas
falla, el problema es de politica, no de adaptador.
"""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipperkick.domain.jobs import (  # noqa: E402
    PROTOCOLO_WORKER, ArchivoSalida, ErrorDependenciaJob, ErrorJob, ErrorProtocoloWorker,
    ErrorRecursoJob, ErrorTransicionJob, ErrorValidacionSalida, EstadoDependencias, EstadoJob, Job,
    ManifiestoSalida, MensajeWorker, PoliticaCancelacion, PoliticaRetry,
    Recursos, StageDefinition,
    TRANSICIONES, TipoMensaje, clasificar_dependencias, detectar_ciclo, excede_capacidad,
    exigir_dag, exigir_transicion, recursos_en_curso, seleccionar_ejecutables,
)
from clipperkick.infrastructure.jobs.protocol import (  # noqa: E402
    codificar, codificar_control, decodificar, es_control_de_cancelacion,
)


def job(identificador, **claves):
    base = dict(stage="prueba", version_contrato="1")
    base.update(claves)
    return Job(id=identificador, **base)


class MaquinaDeEstadosTests(unittest.TestCase):
    def test_los_siete_estados_del_ticket_y_ninguno_mas(self):
        self.assertEqual(
            {estado.value for estado in EstadoJob},
            {"queued", "running", "validating", "succeeded", "cancel_requested", "failed",
             "interrupted"})
        self.assertEqual(set(TRANSICIONES), set(EstadoJob),
                         "cada estado debe declarar explicitamente sus salidas")

    def test_succeeded_es_terminal_y_no_se_alcanza_sin_validar(self):
        self.assertEqual(TRANSICIONES[EstadoJob.SUCCEEDED], frozenset())
        with self.assertRaises(ErrorTransicionJob):
            exigir_transicion(EstadoJob.RUNNING, EstadoJob.SUCCEEDED)
        self.assertEqual(exigir_transicion(EstadoJob.RUNNING, EstadoJob.VALIDATING),
                         EstadoJob.VALIDATING)
        self.assertEqual(exigir_transicion(EstadoJob.VALIDATING, EstadoJob.SUCCEEDED),
                         EstadoJob.SUCCEEDED)

    def test_transiciones_prohibidas_representativas(self):
        prohibidas = (
            (EstadoJob.QUEUED, EstadoJob.VALIDATING),
            (EstadoJob.QUEUED, EstadoJob.SUCCEEDED),
            (EstadoJob.QUEUED, EstadoJob.INTERRUPTED),
            (EstadoJob.SUCCEEDED, EstadoJob.QUEUED),
            (EstadoJob.SUCCEEDED, EstadoJob.FAILED),
            (EstadoJob.FAILED, EstadoJob.RUNNING),
            (EstadoJob.INTERRUPTED, EstadoJob.RUNNING),
        )
        for origen, destino in prohibidas:
            with self.subTest(origen=origen, destino=destino):
                with self.assertRaises(ErrorTransicionJob):
                    exigir_transicion(origen, destino)

    def test_un_reintento_pasa_siempre_por_la_cola(self):
        self.assertIn(EstadoJob.QUEUED, TRANSICIONES[EstadoJob.FAILED])
        self.assertIn(EstadoJob.QUEUED, TRANSICIONES[EstadoJob.INTERRUPTED])
        self.assertNotIn(EstadoJob.RUNNING, TRANSICIONES[EstadoJob.FAILED])


class RecursosTests(unittest.TestCase):
    def test_pesos_negativos_o_no_enteros_se_rechazan(self):
        for invalido in ({"cpu": -1}, {"gpu": 1.5}, {"disk": True}):
            with self.subTest(invalido=invalido):
                with self.assertRaises(ErrorRecursoJob):
                    Recursos(**invalido)

    def test_aritmetica_de_capacidad(self):
        self.assertEqual(Recursos(cpu=1).sumar(Recursos(cpu=2, gpu=1)), Recursos(cpu=3, gpu=1))
        self.assertEqual(Recursos(cpu=1).restar(Recursos(cpu=5)), Recursos())
        self.assertTrue(Recursos(cpu=1).cabe_en(Recursos(cpu=1)))
        self.assertFalse(Recursos(cpu=1, gpu=1).cabe_en(Recursos(cpu=4)))

    def test_serializacion_ida_y_vuelta(self):
        recursos = Recursos(cpu=2, gpu=1, disk=3, network=4)
        self.assertEqual(Recursos.desde_dict(recursos.como_dict()), recursos)

    def test_un_documento_de_recursos_incompleto_o_corrupto_no_degrada_a_cero(self):
        """Degradar a cero convertiria una fila rota en un job sin peso."""
        for documento in ({}, {"cpu": 1}, {"cpu": "1", "gpu": 0, "disk": 0, "network": 0},
                          {"cpu": True, "gpu": 0, "disk": 0, "network": 0},
                          {"cpu": 1.5, "gpu": 0, "disk": 0, "network": 0},
                          {"cpu": None, "gpu": 0, "disk": 0, "network": 0}):
            with self.subTest(documento=documento):
                with self.assertRaises(ErrorRecursoJob):
                    Recursos.desde_dict(documento)

    def test_un_job_mas_pesado_que_la_capacidad_total_es_un_error_de_configuracion(self):
        self.assertTrue(excede_capacidad(job("j", recursos=Recursos(gpu=2)), Recursos(gpu=1)))
        self.assertFalse(excede_capacidad(job("j", recursos=Recursos(gpu=1)), Recursos(gpu=1)))


class PoliticasTests(unittest.TestCase):
    def test_solo_un_codigo_declarado_es_transitorio(self):
        politica = PoliticaRetry(max_intentos=3, codigos_transitorios={"transitorio.io"})
        self.assertTrue(politica.es_transitorio("transitorio.io"))
        self.assertFalse(politica.es_transitorio("entrada.invalida"))
        self.assertFalse(politica.es_transitorio(None))
        self.assertFalse(PoliticaRetry().es_transitorio("transitorio.io"),
                         "sin declaracion explicita nada se reintenta")

    def test_politicas_invalidas_se_rechazan(self):
        with self.assertRaises(ErrorJob):
            PoliticaRetry(max_intentos=0)
        with self.assertRaises(ErrorJob):
            PoliticaCancelacion(timeout_cooperativo=-1)
        with self.assertRaises(ErrorJob):
            StageDefinition(nombre="", version_contrato="1")

    def test_un_job_declara_dependencias_coherentes(self):
        with self.assertRaises(ErrorDependenciaJob):
            job("j1", dependencias=("j1",))
        with self.assertRaises(ErrorDependenciaJob):
            job("j1", dependencias=("j2", "j2"))
        with self.assertRaises(ErrorJob):
            job("j1", max_intentos=0)

    def test_presupuesto_de_interrupcion_es_independiente_del_de_reintento(self):
        candidato = job("j", max_intentos=1, intentos_usados=1, max_interrupciones=3)
        self.assertEqual(candidato.intentos_restantes, 0)
        self.assertEqual(candidato.reanudaciones_restantes, 3,
                         "un corte de luz no debe gastar el credito de fallos de la etapa")


class DependenciasTests(unittest.TestCase):
    def test_detecta_ciclo_directo_indirecto_y_ausencia(self):
        self.assertEqual(detectar_ciclo({"a": ["b"], "b": ["a"]}), ("a", "b", "a"))
        self.assertIsNotNone(detectar_ciclo({"a": ["b"], "b": ["c"], "c": ["a"]}))
        self.assertIsNone(detectar_ciclo({"a": ["b"], "b": [], "c": ["b"]}))
        self.assertIsNone(detectar_ciclo({}))

    def test_un_diamante_no_es_ciclo(self):
        self.assertIsNone(detectar_ciclo({"d": ["b", "c"], "b": ["a"], "c": ["a"], "a": []}))

    def test_exigir_dag_rechaza_aristas_rotas_y_ciclos(self):
        with self.assertRaises(ErrorDependenciaJob):
            exigir_dag({"a": ["fantasma"]}, ("a",))
        with self.assertRaises(ErrorDependenciaJob):
            exigir_dag({"a": ["b"], "b": ["a"]}, ("a", "b"))
        exigir_dag({"a": [], "b": ["a"]}, ("a", "b"))

    def test_clasificacion_distingue_pendiente_de_imposible(self):
        dependiente = job("hijo", dependencias=("p1", "p2"))
        casos = (
            ({"p1": EstadoJob.SUCCEEDED, "p2": EstadoJob.SUCCEEDED}, EstadoDependencias.LISTAS),
            ({"p1": EstadoJob.SUCCEEDED, "p2": EstadoJob.RUNNING}, EstadoDependencias.PENDIENTES),
            ({"p1": EstadoJob.FAILED, "p2": EstadoJob.SUCCEEDED}, EstadoDependencias.IMPOSIBLES),
            ({"p1": EstadoJob.FAILED, "p2": EstadoJob.RUNNING}, EstadoDependencias.IMPOSIBLES),
            ({"p1": EstadoJob.SUCCEEDED}, EstadoDependencias.IMPOSIBLES),
        )
        for estados, esperado in casos:
            with self.subTest(estados=estados):
                self.assertIs(clasificar_dependencias(dependiente, estados), esperado)

    def test_un_padre_fallido_no_arrastra_a_un_hermano(self):
        jobs = (job("padre", estado=EstadoJob.FAILED), job("hijo", dependencias=("padre",)),
                job("hermano"))
        self.assertEqual(seleccionar_ejecutables(jobs, Recursos(cpu=4)), ("hermano",))


class PlanificacionTests(unittest.TestCase):
    def test_respeta_la_capacidad_por_clase(self):
        jobs = tuple(job(f"j{indice}", recursos=Recursos(cpu=1)) for indice in range(4))
        self.assertEqual(seleccionar_ejecutables(jobs, Recursos(cpu=1)), ("j0",))
        self.assertEqual(seleccionar_ejecutables(jobs, Recursos(cpu=2)), ("j0", "j1"))

    def test_descuenta_lo_que_ya_esta_en_curso(self):
        jobs = (job("a", estado=EstadoJob.RUNNING, recursos=Recursos(cpu=1)),
                job("b", recursos=Recursos(cpu=1)))
        self.assertEqual(recursos_en_curso(jobs), Recursos(cpu=1))
        self.assertEqual(seleccionar_ejecutables(jobs, Recursos(cpu=1), recursos_en_curso(jobs)), ())
        self.assertEqual(seleccionar_ejecutables(jobs, Recursos(cpu=2), recursos_en_curso(jobs)),
                         ("b",))

    def test_clases_distintas_no_compiten_entre_si(self):
        jobs = (job("cpu", recursos=Recursos(cpu=1)), job("gpu", recursos=Recursos(gpu=1)))
        self.assertEqual(seleccionar_ejecutables(jobs, Recursos(cpu=1, gpu=1)), ("cpu", "gpu"))

    def test_un_pesado_que_no_cabe_no_bloquea_a_los_ligeros(self):
        jobs = (job("pesado", prioridad=9, recursos=Recursos(cpu=4)),
                job("ligero", recursos=Recursos(cpu=1)))
        self.assertEqual(seleccionar_ejecutables(jobs, Recursos(cpu=2)), ("ligero",))

    def test_el_orden_es_prioridad_y_luego_identidad(self):
        jobs = (job("zeta"), job("alfa"), job("urgente", prioridad=5))
        self.assertEqual(seleccionar_ejecutables(jobs, Recursos(cpu=9)),
                         ("urgente", "alfa", "zeta"))

    def test_un_job_con_cancelacion_pendiente_nunca_se_elige(self):
        jobs = (job("j", cancelacion_solicitada=True),)
        self.assertEqual(seleccionar_ejecutables(jobs, Recursos(cpu=4)), ())

    def test_solo_se_eligen_encolados(self):
        for estado in EstadoJob:
            with self.subTest(estado=estado):
                elegidos = seleccionar_ejecutables((job("j", estado=estado),), Recursos(cpu=4))
                self.assertEqual(elegidos, ("j",) if estado is EstadoJob.QUEUED else ())


class ProtocoloTests(unittest.TestCase):
    def ida_y_vuelta(self, mensaje):
        return decodificar(codificar(mensaje))

    def test_cada_tipo_sobrevive_la_serializacion(self):
        casos = (
            MensajeWorker(tipo=TipoMensaje.INICIO, pid=1234),
            MensajeWorker(tipo=TipoMensaje.PROGRESO, clave="stage.paso",
                          unidades_hechas=2, unidades_totales=5),
            MensajeWorker(tipo=TipoMensaje.CHECKPOINT, checkpoint={"paso": 3, "lineas": ["a"]}),
            MensajeWorker(tipo=TipoMensaje.RESULTADO,
                          manifiesto=ManifiestoSalida((ArchivoSalida("s.txt", 12),),
                                                      {"kind": "prueba"})),
            MensajeWorker(tipo=TipoMensaje.ERROR, codigo="entrada.invalida", detalle="detalle"),
            MensajeWorker(tipo=TipoMensaje.CANCELADO),
        )
        for mensaje in casos:
            with self.subTest(tipo=mensaje.tipo):
                self.assertEqual(self.ida_y_vuelta(mensaje), mensaje)

    def test_progreso_sin_total_conserva_lo_desconocido(self):
        mensaje = self.ida_y_vuelta(MensajeWorker(tipo=TipoMensaje.PROGRESO, unidades_hechas=7))
        self.assertEqual(mensaje.unidades_hechas, 7)
        self.assertIsNone(mensaje.unidades_totales, "unidades desconocidas no pueden inventarse")

    def test_texto_no_ascii_viaja_intacto(self):
        mensaje = self.ida_y_vuelta(MensajeWorker(tipo=TipoMensaje.ERROR, codigo="x",
                                                  detalle="cancelación · 90% ✓"))
        self.assertEqual(mensaje.detalle, "cancelación · 90% ✓")

    def test_toda_linea_fuera_del_contrato_es_error_de_protocolo(self):
        casos = (
            b"no es json",
            b"[1, 2, 3]",
            b'{"tipo": "progreso"}',                                   # sin protocolo
            b'{"protocolo": "otro/9", "tipo": "progreso"}',            # version ajena
            b'{"protocolo": "' + PROTOCOLO_WORKER.encode() + b'", "tipo": "inventado"}',
            b'{"protocolo": "' + PROTOCOLO_WORKER.encode()
            + b'", "tipo": "progreso", "unidades_hechas": "muchas"}',
            b'{"protocolo": "' + PROTOCOLO_WORKER.encode()
            + b'", "tipo": "resultado", "manifiesto": {"archivos": "no es lista"}}',
            b'{"protocolo": "' + PROTOCOLO_WORKER.encode()
            + b'", "tipo": "resultado", "manifiesto": {"archivos": [{"ruta": "a"}]}}',
            b"\xff\xfe no es utf-8",
        )
        for linea in casos:
            with self.subTest(linea=linea[:40]):
                with self.assertRaises(ErrorProtocoloWorker):
                    decodificar(linea)

    def test_una_linea_desmesurada_se_rechaza_sin_intentar_parsearla(self):
        with self.assertRaises(ErrorProtocoloWorker):
            decodificar(b"x" * (1 << 21))

    def test_un_manifiesto_degenerado_se_rechaza_en_el_modelo(self):
        with self.assertRaises(ErrorValidacionSalida):
            ArchivoSalida("", 1)
        with self.assertRaises(ErrorValidacionSalida):
            ArchivoSalida("a.txt", -1)

    def test_el_canal_de_control_solo_reconoce_su_unica_orden(self):
        self.assertTrue(es_control_de_cancelacion(codificar_control("cancelar").strip()))
        for ruido in (b"cancelar", b"{}", b'{"tipo": "cancelar"}', b"\xff", b"[]"):
            with self.subTest(ruido=ruido):
                self.assertFalse(es_control_de_cancelacion(ruido))

    def test_una_linea_de_mensaje_es_exactamente_una_linea(self):
        codificado = codificar(MensajeWorker(tipo=TipoMensaje.ERROR, detalle="con\nsalto"))
        self.assertEqual(codificado.count(b"\n"), 1, "un salto embebido partiria el flujo NDJSON")
        self.assertEqual(decodificar(codificado.strip()).detalle, "con\nsalto")


class EventoTests(unittest.TestCase):
    def test_la_fraccion_solo_existe_cuando_el_total_se_conoce(self):
        from clipperkick.domain.jobs import EventoProgreso, TipoEvento

        conocido = EventoProgreso("j", TipoEvento.PROGRESO, 0.0, unidades_hechas=1,
                                  unidades_totales=4)
        self.assertAlmostEqual(conocido.fraccion, 0.25)
        for desconocido in (EventoProgreso("j", TipoEvento.PROGRESO, 0.0, unidades_hechas=1),
                            EventoProgreso("j", TipoEvento.PROGRESO, 0.0, unidades_totales=4),
                            EventoProgreso("j", TipoEvento.PROGRESO, 0.0, unidades_hechas=1,
                                           unidades_totales=0)):
            self.assertIsNone(desconocido.fraccion)


if __name__ == "__main__":
    unittest.main()
