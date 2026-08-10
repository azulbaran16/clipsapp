"""Panel de reproduccion: superficie de video, transporte y barra de tramo.

La vista no conoce el reproductor. Recibe un view model ya construido —o un
motivo por el que no lo hay— y se limita a pintar su estado y a traducir gestos
a ordenes. Por eso el panel se puede construir antes de saber si QtMultimedia
existe en este equipo: `degradar()` es una pantalla mas, no una excepcion.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSizePolicy,
                               QSlider, QVBoxLayout, QWidget)

from clipperkick.desktop.viewmodels import EstadoReproductor, ViewModelReproduccion


PASOS = 1000


def _reloj(segundos: float) -> str:
    segundos = max(0, int(segundos))
    return f"{segundos // 60:02d}:{segundos % 60:02d}"


class PanelReproduccion(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._vm: ViewModelReproduccion | None = None

        self.superficie = QVideoWidget(self)
        self.superficie.setMinimumHeight(220)
        self.superficie.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self._titulo = QLabel("Sin medio cargado", self)
        self._titulo.setObjectName("tituloMedio")
        self._aviso = QLabel("", self)
        self._aviso.setWordWrap(True)
        self._aviso.setObjectName("avisoMedio")

        self._anterior = QPushButton("◀◀", self)
        self._alternar = QPushButton("▶", self)
        self._siguiente = QPushButton("▶▶", self)
        for boton in (self._anterior, self._alternar, self._siguiente):
            boton.setFixedWidth(56)
        self._tiempo = QLabel("00:00 / 00:00", self)
        self._barra = QSlider(Qt.Orientation.Horizontal, self)
        self._barra.setRange(0, PASOS)

        transporte = QHBoxLayout()
        transporte.addWidget(self._anterior)
        transporte.addWidget(self._alternar)
        transporte.addWidget(self._siguiente)
        transporte.addWidget(self._barra, 1)
        transporte.addWidget(self._tiempo)

        cuerpo = QVBoxLayout(self)
        cuerpo.addWidget(self._titulo)
        cuerpo.addWidget(self.superficie, 1)
        cuerpo.addLayout(transporte)
        cuerpo.addWidget(self._aviso)

        self._anterior.clicked.connect(lambda: self._vm and self._vm.anterior())
        self._siguiente.clicked.connect(lambda: self._vm and self._vm.siguiente())
        self._alternar.clicked.connect(lambda: self._vm and self._vm.alternar())
        self._barra.sliderMoved.connect(self._arrastrar)
        self._habilitar(False)

    # -- cableado ------------------------------------------------------------ #

    def conectar(self, vm: ViewModelReproduccion) -> None:
        self._vm = vm
        vm.estado.suscribir(self.pintar)

    def degradar(self, motivo: str) -> None:
        """Sin reproductor la carcasa sigue siendo util: se dice que falta y ya."""
        self._vm = None
        self.superficie.hide()
        self._titulo.setText("Previsualizacion no disponible")
        self._aviso.setText(motivo)
        self._habilitar(False)

    # -- estado -------------------------------------------------------------- #

    def pintar(self, estado: EstadoReproductor) -> None:
        self._titulo.setText(estado.etiqueta or "Sin medio cargado")
        self._alternar.setText("❚❚" if estado.reproduciendo else "▶")
        self._alternar.setEnabled(estado.hay_medio)
        self._anterior.setEnabled(estado.puede_retroceder)
        self._siguiente.setEnabled(estado.puede_avanzar)
        self._barra.setEnabled(estado.hay_medio)
        self._tiempo.setText(f"{_reloj(estado.posicion)} / {_reloj(estado.duracion)}")
        self._aviso.setText(estado.mensaje)
        if not self._barra.isSliderDown():
            # Mientras la persona arrastra, el estado que llega es el de *antes*
            # del salto: reescribir la barra le arrancaria el pulgar de la mano.
            self._barra.setValue(int(estado.fraccion * PASOS))

    def _arrastrar(self, valor: int) -> None:
        if self._vm is not None:
            self._vm.buscar_fraccion(valor / PASOS)

    def _habilitar(self, activo: bool) -> None:
        for control in (self._anterior, self._alternar, self._siguiente, self._barra):
            control.setEnabled(activo)
