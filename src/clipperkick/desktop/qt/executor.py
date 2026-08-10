"""`Ejecutor` Qt con un hilo dedicado para toda la sesion de proyecto.

No basta con usar tareas sueltas de ``QThreadPool``. SQLite asigna cada
conexion al hilo que la creo y T02 conserva esa conexion mientras el proyecto
permanece abierto. Si ``abrir`` corre en un hilo del pool y ``cerrar`` en otro,
la liberacion falla precisamente al salir y el lock queda vivo.

Este ejecutor mantiene una cola sobre un unico ``QThread``: crear, abrir,
reconciliar y cerrar ocurren siempre en el mismo hilo. Los resultados vuelven
por una senal al hilo de Qt, de modo que los view models siguen siendo Python
puro y nunca protegen su estado con locks.
"""

from __future__ import annotations

from collections.abc import Callable
import threading
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot


class _Trabajador(QObject):
    terminado = Signal(object, object, object)

    @Slot(object, object)
    def ejecutar(self, identificador: object, trabajo: Callable[[], Any]) -> None:
        try:
            resultado, error = trabajo(), None
        except Exception as fallo:
            resultado, error = None, fallo
        self.terminado.emit(identificador, resultado, error)


class EjecutorQt(QObject):
    _ejecutar = Signal(object, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._hilo = QThread(self)
        self._trabajador = _Trabajador()
        self._trabajador.moveToThread(self._hilo)
        self._hilo.finished.connect(self._trabajador.deleteLater)
        self._ejecutar.connect(self._trabajador.ejecutar)
        self._trabajador.terminado.connect(self._entregar)
        self._continuaciones: dict[object, tuple[Callable[[Any], None],
                                                  Callable[[BaseException], None]]] = {}
        self._detenido = False
        self._hilo.start()

    @property
    def pendientes(self) -> int:
        return len(self._continuaciones)

    def enviar(self, trabajo: Callable[[], Any],
               al_terminar: Callable[[Any], None],
               al_fallar: Callable[[BaseException], None]) -> None:
        if self._detenido:
            raise RuntimeError("El ejecutor de la sesion ya esta cerrado.")
        identificador = object()
        self._continuaciones[identificador] = (al_terminar, al_fallar)
        self._ejecutar.emit(identificador, trabajo)

    @Slot(object, object, object)
    def _entregar(self, identificador: object, resultado: Any,
                  error: BaseException | None) -> None:
        continuaciones = self._continuaciones.pop(identificador, None)
        if continuaciones is None:
            return
        al_terminar, al_fallar = continuaciones
        if error is None:
            al_terminar(resultado)
        else:
            al_fallar(error)

    def esperar(self, milisegundos: int = -1) -> bool:
        """Espera a que termine todo lo enviado antes de esta llamada.

        Se usa un centinela dentro de la misma cola, no el contador de callbacks:
        durante ``aboutToQuit`` el hilo principal esta esperando y las senales de
        respuesta pueden seguir encoladas. El centinela demuestra que el trabajo
        —incluido el cierre de SQLite— ya termino aunque su repintado no se entregue.
        """
        if self._detenido:
            return True
        drenado = threading.Event()
        self._ejecutar.emit(object(), drenado.set)
        espera = None if milisegundos < 0 else milisegundos / 1000.0
        return drenado.wait(espera)

    def detener(self, milisegundos: int = -1) -> bool:
        """Drena la cola y termina el hilo. Es idempotente."""
        if self._detenido:
            return True
        if not self.esperar(milisegundos):
            return False
        self._detenido = True
        self._hilo.quit()
        terminado = self._hilo.wait() if milisegundos < 0 else self._hilo.wait(milisegundos)
        if terminado:
            # Durante aboutToQuit las entregas al hilo principal pueden seguir
            # encoladas. El trabajo ya termino y esas continuaciones no deben
            # retener la ventana ni el caso de uso mientras Qt se destruye.
            self._continuaciones.clear()
        return terminado
