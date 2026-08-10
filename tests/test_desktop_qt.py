"""Integracion Qt opcional: hilo de sesion, carcasa y cierre de handles."""

from pathlib import Path
import os
import sys
import tempfile
import threading
import time
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
except ImportError:
    QApplication = None


@unittest.skipUnless(QApplication is not None, "PySide6 es un extra opcional")
class IntegracionQtTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def esperar(self, condicion, timeout=5.0):
        limite = time.monotonic() + timeout
        while not condicion() and time.monotonic() < limite:
            self.app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
            time.sleep(0.005)
        self.assertTrue(condicion(), "la operacion Qt no termino dentro del timeout")

    def test_el_trabajo_vive_en_un_hilo_y_la_continuacion_vuelve_al_hilo_qt(self):
        from clipperkick.desktop.qt import EjecutorQt

        ejecutor = EjecutorQt(self.app)
        principal = threading.get_ident()
        observado = []
        ejecutor.enviar(threading.get_ident,
                        lambda hilo: observado.append((hilo, threading.get_ident())),
                        lambda error: observado.append(error))
        self.esperar(lambda: bool(observado))

        self.assertNotEqual(observado[0][0], principal)
        self.assertEqual(observado[0][1], principal)
        self.assertTrue(ejecutor.detener())

    def test_crear_abrir_y_cerrar_repetido_conserva_afinidad_y_libera_archivos(self):
        from clipperkick.application.project import CasoDeUsoProyectos
        from clipperkick.desktop.qt import EjecutorQt
        from clipperkick.desktop.viewmodels import Pantalla, ViewModelCarcasa
        from clipperkick.infrastructure.project.adapters import CatalogoProyectosLocal
        from clipperkick.infrastructure.project.recents import RecientesEnMemoria

        with tempfile.TemporaryDirectory() as temporal:
            base = Path(temporal)
            raiz = base / "demo.clipsapp"
            ejecutor = EjecutorQt(self.app)
            vm = ViewModelCarcasa(
                CasoDeUsoProyectos(CatalogoProyectosLocal(), RecientesEnMemoria()), ejecutor)

            vm.crear(str(raiz), "Demo")
            self.esperar(lambda: not vm.estado.valor.ocupado)
            self.assertIs(vm.estado.valor.pantalla, Pantalla.WORKSPACE)
            vm.cerrar_proyecto()
            self.esperar(lambda: not vm.estado.valor.ocupado)

            for _ in range(3):
                vm.abrir(str(raiz))
                self.esperar(lambda: not vm.estado.valor.ocupado)
                self.assertFalse(vm.estado.valor.proyecto.solo_lectura)
                vm.cerrar_proyecto()
                self.esperar(lambda: not vm.estado.valor.ocupado)

            self.assertTrue(ejecutor.detener())
            movida = base / "movida.clipsapp"
            raiz.rename(movida)
            (movida / "state.sqlite3").rename(movida / "state-renamed.sqlite3")

    def test_la_carcasa_se_construye_sin_retirar_el_entrypoint_tkinter(self):
        from clipperkick.desktop.app import crear_ventana

        ventana, ejecutor, reproduccion = crear_ventana(self.app)
        try:
            self.assertEqual(ventana.windowTitle().split(" ")[0], "ClipsApp")
            self.assertTrue((Path(__file__).resolve().parents[1] / "ClipperKick.pyw").is_file())
        finally:
            if reproduccion is not None:
                reproduccion.cerrar()
            ventana.vm.preparar_cierre_aplicacion()
            self.assertTrue(ejecutor.detener())
            ventana.close()


if __name__ == "__main__":
    unittest.main()
