"""Contratos puros de la carcasa y del reproductor de T08."""

from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from clipperkick.application.project import (  # noqa: E402
    CasoDeUsoProyectos, ProyectoReciente, SnapshotProyecto,
)
from clipperkick.desktop.viewmodels import (  # noqa: E402
    Pantalla, ViewModelCarcasa, ViewModelReproduccion,
)
from clipperkick.domain.playback import (  # noqa: E402
    ElementoPlaylist, EstadoReproduccion, FalloReproduccion,
    ProgresoReproduccion, RangoReproduccion,
)
from clipperkick.domain.project import InformacionProyecto, ResultadoReconciliacion  # noqa: E402
from clipperkick.infrastructure.project.recents import RecientesEnMemoria  # noqa: E402


class SesionFalsa:
    def __init__(self, ruta="demo.clipsapp", nombre="Demo", *, faltantes=0,
                 reconstruibles=0, solo_lectura=False, fallo=None):
        self.ruta = ruta
        self.solo_lectura = solo_lectura
        self._informacion = InformacionProyecto("project-1", nombre, 6)
        self._reconciliacion = ResultadoReconciliacion(faltantes, reconstruibles)
        self._fallo = fallo
        self.cerrada = False

    def informacion(self):
        return self._informacion

    def reconciliar(self):
        if self._fallo is not None:
            raise self._fallo
        return self._reconciliacion

    def cerrar(self):
        self.cerrada = True


class CatalogoFalso:
    def __init__(self, sesiones):
        self.sesiones = list(sesiones)
        self.llamadas = []

    def crear(self, ruta, nombre):
        self.llamadas.append(("crear", ruta, nombre))
        return self.sesiones.pop(0)

    def abrir(self, ruta):
        self.llamadas.append(("abrir", ruta))
        return self.sesiones.pop(0)


class EjecutorDiferido:
    def __init__(self):
        self.pendientes = []

    def enviar(self, trabajo, al_terminar, al_fallar):
        self.pendientes.append((trabajo, al_terminar, al_fallar))

    def resolver(self):
        trabajo, al_terminar, al_fallar = self.pendientes.pop(0)
        try:
            resultado = trabajo()
        except Exception as error:
            al_fallar(error)
        else:
            al_terminar(resultado)


class ReproductorFalso:
    def __init__(self):
        self.oyente = None
        self.llamadas = []

    def suscribir(self, oyente):
        self.oyente = oyente

    def cargar(self, elementos, indice=0):
        self.llamadas.append(("cargar", tuple(elementos), indice))

    def seleccionar(self, indice):
        self.llamadas.append(("seleccionar", indice))

    def reproducir(self):
        self.llamadas.append(("reproducir",))

    def pausar(self):
        self.llamadas.append(("pausar",))

    def buscar(self, segundos):
        self.llamadas.append(("buscar", segundos))

    def fijar_rango(self, rango):
        self.llamadas.append(("fijar_rango", rango))

    def liberar(self):
        self.llamadas.append(("liberar",))


class CasoDeUsoProyectosTests(unittest.TestCase):
    def test_adopta_snapshot_reconcilia_y_recuerda_solo_despues_del_exito(self):
        sesion = SesionFalsa(faltantes=2, reconstruibles=1, solo_lectura=True)
        recientes = RecientesEnMemoria()
        caso = CasoDeUsoProyectos(CatalogoFalso([sesion]), recientes, reloj=lambda: 123.0)

        snapshot = caso.abrir(sesion.ruta)

        self.assertEqual((snapshot.nombre, snapshot.fuentes_faltantes,
                          snapshot.artefactos_reconstruibles, snapshot.solo_lectura),
                         ("Demo", 2, 1, True))
        self.assertTrue(snapshot.requiere_atencion)
        self.assertEqual(caso.recientes(), (ProyectoReciente(sesion.ruta, "Demo", 123.0),))

    def test_una_sesion_que_no_se_puede_describir_se_cierra_y_no_se_recuerda(self):
        sesion = SesionFalsa(fallo=RuntimeError("reconciliacion rota"))
        recientes = RecientesEnMemoria()
        caso = CasoDeUsoProyectos(CatalogoFalso([sesion]), recientes)

        with self.assertRaises(RuntimeError):
            caso.abrir(sesion.ruta)

        self.assertTrue(sesion.cerrada)
        self.assertEqual(caso.recientes(), ())

    def test_reabrir_cierra_la_sesion_anterior_antes_de_adoptar_la_siguiente(self):
        primera, segunda = SesionFalsa(ruta="uno"), SesionFalsa(ruta="dos")
        caso = CasoDeUsoProyectos(CatalogoFalso([primera, segunda]), RecientesEnMemoria())
        caso.abrir("uno")

        caso.abrir("dos")

        self.assertTrue(primera.cerrada)
        self.assertFalse(segunda.cerrada)
        caso.cerrar()
        caso.cerrar()
        self.assertTrue(segunda.cerrada, "cerrar debe ser idempotente")


class ViewModelCarcasaTests(unittest.TestCase):
    def test_crear_es_diferido_bloquea_ordenes_y_publica_workspace_al_terminar(self):
        sesion = SesionFalsa(ruta="nuevo.clipsapp", reconstruibles=1)
        catalogo = CatalogoFalso([sesion])
        ejecutor = EjecutorDiferido()
        vm = ViewModelCarcasa(CasoDeUsoProyectos(catalogo, RecientesEnMemoria()), ejecutor)

        vm.crear("nuevo.clipsapp", "Nuevo")
        vm.abrir("ignorado.clipsapp")

        self.assertTrue(vm.estado.valor.ocupado)
        self.assertEqual(catalogo.llamadas, [], "la operacion no debe ejecutarse en el event loop")
        self.assertEqual(len(ejecutor.pendientes), 1, "una segunda orden ocupada se ignora")
        ejecutor.resolver()

        self.assertIs(vm.estado.valor.pantalla, Pantalla.WORKSPACE)
        self.assertFalse(vm.estado.valor.ocupado)
        self.assertEqual(vm.estado.valor.proyecto.ruta, "nuevo.clipsapp")
        self.assertIn("se regeneraran", vm.estado.valor.mensaje)

    def test_fallo_vuelve_a_recientes_con_mensaje_y_sin_proyecto_fantasma(self):
        sesion = SesionFalsa(fallo=RuntimeError("detalle reproducible"))
        ejecutor = EjecutorDiferido()
        vm = ViewModelCarcasa(
            CasoDeUsoProyectos(CatalogoFalso([sesion]), RecientesEnMemoria()), ejecutor)

        vm.abrir("roto.clipsapp")
        ejecutor.resolver()

        self.assertIs(vm.estado.valor.pantalla, Pantalla.RECIENTES)
        self.assertIsNone(vm.estado.valor.proyecto)
        self.assertIn("detalle reproducible", vm.estado.valor.mensaje)

    def test_cerrar_tambien_pasa_por_el_ejecutor_de_la_sesion(self):
        sesion = SesionFalsa()
        ejecutor = EjecutorDiferido()
        vm = ViewModelCarcasa(
            CasoDeUsoProyectos(CatalogoFalso([sesion]), RecientesEnMemoria()), ejecutor)
        vm.abrir(sesion.ruta)
        ejecutor.resolver()

        vm.cerrar_proyecto()

        self.assertTrue(vm.estado.valor.ocupado)
        self.assertFalse(sesion.cerrada)
        ejecutor.resolver()
        self.assertTrue(sesion.cerrada)
        self.assertIs(vm.estado.valor.pantalla, Pantalla.RECIENTES)


class ViewModelReproduccionTests(unittest.TestCase):
    def setUp(self):
        self.puerto = ReproductorFalso()
        self.vm = ViewModelReproduccion(self.puerto)
        self.rango = RangoReproduccion(10.0, 20.0)
        self.elementos = (
            ElementoPlaylist("uno", "uno.mp4", self.rango, "Uno"),
            ElementoPlaylist("dos", "dos.mp4", etiqueta="Dos"),
        )

    def test_traduce_playlist_progreso_y_seek_relativo_sin_conocer_backend(self):
        self.vm.cargar(self.elementos)
        self.puerto.oyente.progreso(ProgresoReproduccion(
            0, "uno", EstadoReproduccion.REPRODUCIENDO, 12.5, 90.0, self.rango))

        estado = self.vm.estado.valor
        self.assertEqual((estado.etiqueta, estado.posicion, estado.duracion, estado.fraccion),
                         ("Uno", 2.5, 10.0, 0.25))
        self.vm.buscar_fraccion(0.75)
        self.assertEqual(self.puerto.llamadas[-1], ("buscar", 17.5))

    def test_transporte_fallo_playlist_y_liberacion_conservan_el_contexto_correcto(self):
        self.vm.cargar(self.elementos)
        self.puerto.oyente.progreso(ProgresoReproduccion(
            0, "uno", EstadoReproduccion.PAUSADO, 10.0, 90.0, self.rango))
        self.vm.alternar()
        self.vm.siguiente()
        self.puerto.oyente.fallo(FalloReproduccion("uno", "medio", "No se pudo leer"))

        self.assertIn(("reproducir",), self.puerto.llamadas)
        self.assertIn(("seleccionar", 1), self.puerto.llamadas)
        self.assertEqual(self.vm.estado.valor.mensaje, "No se pudo leer")
        self.assertEqual(self.vm.estado.valor.total, 2, "un fallo no debe vaciar la playlist")

        self.vm.vaciar()
        self.assertEqual(self.puerto.llamadas[-1], ("cargar", (), 0))
        self.assertFalse(self.vm.estado.valor.hay_medio)

        self.vm.cargar(self.elementos)
        self.vm.cerrar()
        self.assertEqual(self.puerto.llamadas[-1], ("liberar",))
        llamadas = len(self.puerto.llamadas)
        self.vm.cerrar()
        self.vm.vaciar()
        self.assertEqual(len(self.puerto.llamadas), llamadas,
                         "el cierre tardio de la carcasa debe ser idempotente")
        self.assertFalse(self.vm.estado.valor.hay_medio)


class ModelosReproduccionTests(unittest.TestCase):
    def test_rangos_invalidos_y_elementos_sin_identidad_se_rechazan_antes_del_backend(self):
        for argumentos in ((-1, 1), (2, 2), (3, 2)):
            with self.subTest(argumentos=argumentos), self.assertRaises(Exception):
                RangoReproduccion(*argumentos)
        with self.assertRaises(Exception):
            ElementoPlaylist("", "video.mp4")
        with self.assertRaises(Exception):
            ElementoPlaylist("id", "")


if __name__ == "__main__":
    unittest.main()
