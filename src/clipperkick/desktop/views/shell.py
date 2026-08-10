"""Ventana de la carcasa: recientes, workspace y la barra que explica el estado.

La ventana no decide nada. Traduce clics a ordenes del view model y estados del
view model a widgets; incluso el bloqueo de los botones durante una apertura
viene del estado publicado, no de una bandera propia. Es lo que permite probar
la navegacion sin abrir una ventana.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QInputDialog, QLabel,
                               QListWidget, QListWidgetItem, QMainWindow, QPushButton,
                               QStackedWidget, QVBoxLayout, QWidget)

from clipperkick.desktop.viewmodels import EstadoCarcasa, Pantalla, ViewModelCarcasa

from .playback import PanelReproduccion


FILTRO_VIDEO = "Videos (*.mp4 *.mkv *.mov *.flv *.ts);;Todos (*.*)"


class PantallaRecientes(QWidget):
    def __init__(self, vm: ViewModelCarcasa, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._vm = vm

        titulo = QLabel("Proyectos", self)
        titulo.setObjectName("tituloPantalla")
        self.lista = QListWidget(self)
        self.lista.setAlternatingRowColors(True)

        self.nuevo = QPushButton("Nuevo proyecto…", self)
        self.abrir = QPushButton("Abrir carpeta…", self)
        self.olvidar = QPushButton("Quitar de la lista", self)

        acciones = QHBoxLayout()
        acciones.addWidget(self.nuevo)
        acciones.addWidget(self.abrir)
        acciones.addStretch(1)
        acciones.addWidget(self.olvidar)

        cuerpo = QVBoxLayout(self)
        cuerpo.addWidget(titulo)
        cuerpo.addWidget(self.lista, 1)
        cuerpo.addLayout(acciones)

        self.lista.itemActivated.connect(self._activar)
        self.olvidar.clicked.connect(self._olvidar)

    def pintar(self, estado: EstadoCarcasa) -> None:
        seleccion = self.ruta_seleccionada()
        self.lista.clear()
        for reciente in estado.recientes:
            elemento = QListWidgetItem(f"{reciente.nombre}\n{reciente.ruta}", self.lista)
            elemento.setData(Qt.ItemDataRole.UserRole, reciente.ruta)
            if reciente.ruta == seleccion:
                self.lista.setCurrentItem(elemento)
        if not estado.recientes:
            vacio = QListWidgetItem("Todavia no hay proyectos. Crea uno para empezar.", self.lista)
            vacio.setFlags(Qt.ItemFlag.NoItemFlags)
        for control in (self.nuevo, self.abrir, self.olvidar, self.lista):
            control.setEnabled(estado.acepta_ordenes)

    def ruta_seleccionada(self) -> str:
        elemento = self.lista.currentItem()
        dato = elemento.data(Qt.ItemDataRole.UserRole) if elemento is not None else None
        return str(dato) if dato else ""

    def _activar(self, elemento: QListWidgetItem) -> None:
        ruta = elemento.data(Qt.ItemDataRole.UserRole)
        if ruta:
            self._vm.abrir(str(ruta))

    def _olvidar(self) -> None:
        ruta = self.ruta_seleccionada()
        if ruta:
            self._vm.olvidar(ruta)


class PantallaWorkspace(QWidget):
    """Workspace vacio: identidad del proyecto, su salud y el reproductor.

    Candidatos, inspector y timeline llegan en tickets posteriores; lo que hay
    aqui es el marco donde encajaran y la unica pieza que T08 debia dejar
    resuelta —la reproduccion— ya montada sobre su puerto.
    """

    def __init__(self, vm: ViewModelCarcasa, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._vm = vm

        self.nombre = QLabel("", self)
        self.nombre.setObjectName("tituloPantalla")
        self.detalle = QLabel("", self)
        self.detalle.setWordWrap(True)
        self.salud = QLabel("", self)
        self.salud.setObjectName("saludProyecto")
        self.salud.setWordWrap(True)

        self.cargar_video = QPushButton("Previsualizar un video…", self)
        self.cerrar = QPushButton("Cerrar proyecto", self)
        encabezado = QHBoxLayout()
        encabezado.addWidget(self.nombre, 1)
        encabezado.addWidget(self.cargar_video)
        encabezado.addWidget(self.cerrar)

        self.reproduccion = PanelReproduccion(self)

        cuerpo = QVBoxLayout(self)
        cuerpo.addLayout(encabezado)
        cuerpo.addWidget(self.detalle)
        cuerpo.addWidget(self.salud)
        cuerpo.addWidget(self.reproduccion, 1)

        self.cerrar.clicked.connect(self._vm.cerrar_proyecto)

    def pintar(self, estado: EstadoCarcasa) -> None:
        proyecto = estado.proyecto
        if proyecto is None:
            return
        sufijo = " · solo lectura" if proyecto.solo_lectura else ""
        self.nombre.setText(proyecto.nombre + sufijo)
        self.detalle.setText(f"{proyecto.ruta}\nesquema v{proyecto.schema_version} · {proyecto.project_id}")
        self.salud.setText(estado.mensaje or "Proyecto listo.")
        self.cerrar.setEnabled(estado.acepta_ordenes)


class VentanaPrincipal(QMainWindow):
    def __init__(self, vm: ViewModelCarcasa) -> None:
        super().__init__()
        self.vm = vm
        self.al_previsualizar: Callable[[str], None] | None = None
        self.setWindowTitle("ClipsApp — editor asistido de clips")
        self.resize(980, 660)

        self.recientes = PantallaRecientes(vm, self)
        self.workspace = PantallaWorkspace(vm, self)
        self.pilas = QStackedWidget(self)
        self.pilas.addWidget(self.recientes)
        self.pilas.addWidget(self.workspace)
        self.setCentralWidget(self.pilas)

        self.estado_barra = QLabel("", self)
        self.statusBar().addWidget(self.estado_barra)

        self.recientes.nuevo.clicked.connect(self.pedir_nuevo)
        self.recientes.abrir.clicked.connect(self.pedir_abrir)
        self.workspace.cargar_video.clicked.connect(self.pedir_video)

        vm.estado.suscribir(self.pintar)
        vm.refrescar_recientes()

    # -- estado -------------------------------------------------------------- #

    def pintar(self, estado: EstadoCarcasa) -> None:
        self.pilas.setCurrentIndex(1 if estado.pantalla is Pantalla.WORKSPACE else 0)
        self.recientes.pintar(estado)
        self.workspace.pintar(estado)
        self.estado_barra.setText(estado.actividad or estado.mensaje)

    # -- dialogos ------------------------------------------------------------ #

    def pedir_nuevo(self) -> None:
        contenedor = QFileDialog.getExistingDirectory(self, "¿Donde guardo el proyecto?")
        if not contenedor:
            return
        nombre, aceptado = QInputDialog.getText(self, "Nuevo proyecto", "Nombre del proyecto:")
        if not aceptado or not nombre.strip():
            return
        self.vm.crear(str(Path(contenedor) / nombre.strip()), nombre)

    def pedir_abrir(self) -> None:
        carpeta = QFileDialog.getExistingDirectory(self, "Elige la carpeta del proyecto")
        if carpeta:
            self.vm.abrir(carpeta)

    def pedir_video(self) -> None:
        ruta, _filtro = QFileDialog.getOpenFileName(self, "Elige un video", "", FILTRO_VIDEO)
        if ruta:
            self.previsualizar(ruta)

    def previsualizar(self, ruta: str) -> None:
        """Entrega el medio a quien sepa reproducirlo.

        La ventana se construye antes que el reproductor —el adaptador necesita
        la superficie de video que vive en el panel—, asi que el cableado instala
        `al_previsualizar` despues. Mientras no lo haga, elegir un video no hace
        nada y tampoco rompe nada.
        """
        if self.al_previsualizar is not None:
            self.al_previsualizar(ruta)
