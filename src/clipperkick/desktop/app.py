"""Raiz de composicion de la carcasa PySide6.

Este es el unico modulo del paquete `desktop` autorizado a conocer adaptadores
concretos. Todo lo demas —view models y vistas— recibe puertos ya construidos,
y por eso la carcasa se puede probar sin proyecto real y el reproductor se puede
sustituir sin tocar una sola vista.

Es tambien el unico sitio donde se decide que hacer cuando falta el reproductor:
la aplicacion arranca igual y el panel explica que falta. Perder la
previsualizacion no puede impedir abrir un proyecto que ya existe.
"""

from __future__ import annotations

import sys
import uuid

from PySide6.QtWidgets import QApplication

from clipperkick.application.project import CasoDeUsoProyectos
from clipperkick.domain.playback import ElementoPlaylist, ErrorBackendNoDisponible
from clipperkick.infrastructure.playback import crear_reproductor
from clipperkick.infrastructure.project.adapters import CatalogoProyectosLocal
from clipperkick.infrastructure.project.recents import RecientesJson

from .qt.executor import EjecutorQt
from .viewmodels import Pantalla, ViewModelCarcasa, ViewModelReproduccion
from .views import VentanaPrincipal


def crear_ventana(parent=None) -> tuple[VentanaPrincipal, EjecutorQt, ViewModelReproduccion | None]:
    """Ensambla la carcasa completa y devuelve lo que hay que cerrar al salir."""
    ejecutor = EjecutorQt(parent)
    carcasa = ViewModelCarcasa(CasoDeUsoProyectos(CatalogoProyectosLocal(), RecientesJson()), ejecutor)
    ventana = VentanaPrincipal(carcasa)

    reproduccion: ViewModelReproduccion | None = None
    try:
        reproductor = crear_reproductor(ventana.workspace.reproduccion.superficie)
    except ErrorBackendNoDisponible as error:
        ventana.workspace.reproduccion.degradar(str(error))
    else:
        reproduccion = ViewModelReproduccion(reproductor)
        ventana.workspace.reproduccion.conectar(reproduccion)
        ventana.al_previsualizar = lambda ruta: reproduccion.cargar(
            [ElementoPlaylist(str(uuid.uuid4()), ruta, etiqueta=ruta)])
        # Volver a recientes significa que la carpeta dejo de ser la sesion
        # activa. Se suelta su medio sin destruir el backend, que se reutilizara
        # al abrir el proyecto siguiente.
        carcasa.estado.suscribir(
            lambda estado: reproduccion.vaciar() if estado.pantalla is Pantalla.RECIENTES else None)
    return ventana, ejecutor, reproduccion


def main(argv: list[str] | None = None) -> int:
    aplicacion = QApplication(argv if argv is not None else sys.argv)
    aplicacion.setApplicationName("ClipsApp")
    ventana, ejecutor, reproduccion = crear_ventana(aplicacion)

    def apagar() -> None:
        # El orden importa: primero se suelta el medio —en Windows retiene el
        # archivo—, despues se drena el hilo para que ningun trabajo siga
        # escribiendo en un proyecto que esta a punto de cerrarse, y solo
        # entonces se cierra el proyecto y se libera su lock.
        if reproduccion is not None:
            reproduccion.cerrar()
        ventana.vm.preparar_cierre_aplicacion()
        ejecutor.detener()

    aplicacion.aboutToQuit.connect(apagar)
    ventana.show()
    return aplicacion.exec()


if __name__ == "__main__":
    raise SystemExit(main())
