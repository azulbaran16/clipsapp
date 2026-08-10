"""`PlaybackPort` sobre QtMultimedia.

QtMultimedia entrega un reproductor de archivo completo; ClipsApp necesita un
reproductor de *tramos*. La diferencia la absorbe este adaptador y solo el:

- **El rango se vigila en `positionChanged`.** Qt no sabe terminar en un punto
  arbitrario, asi que el corte lo impone el adaptador. La granularidad del
  evento acota la precision del final —se mide en el spike— y por eso el tramo
  se cierra con `pause` + `setPosition`, no dejando correr el medio.
- **Buscar antes de que el medio cargue no hace nada.** `setSource` es asincrono:
  una posicion fijada antes de `LoadedMedia` se pierde en silencio. Se recuerda
  la intencion —posicion y si habia que reproducir— y se aplica al cargar.
- **`liberar()` desmonta de verdad.** En Windows el handle del archivo vive
  mientras el `QMediaPlayer` conserve la fuente: sin `setSource(QUrl())` el
  proyecto no se puede archivar, mover ni limpiar aunque la ventana ya no exista.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QObject, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaDevices, QMediaPlayer

from clipperkick.application.playback import OyenteReproduccion
from clipperkick.domain.playback import (ElementoPlaylist, EstadoReproduccion,
                                         FalloReproduccion, ProgresoReproduccion,
                                         RangoReproduccion)


NOMBRE = "qtmultimedia"


def _segundos(milisegundos: int) -> float:
    return max(0, milisegundos) / 1000.0


def _milisegundos(segundos: float) -> int:
    return int(round(max(0.0, segundos) * 1000))


class AdaptadorQtMultimedia:
    """Reproductor de playlist de rangos respaldado por `QMediaPlayer`."""

    def __init__(self, salida_video: QObject | None = None, *, con_audio: bool = True) -> None:
        self._reproductor = QMediaPlayer()
        self._salida_audio: QAudioOutput | None = None
        if con_audio and not QMediaDevices.defaultAudioOutput().isNull():
            # Un equipo sin dispositivo de salida no es un fallo del proyecto:
            # el video debe seguir decodificando y el tramo, siendo navegable.
            self._salida_audio = QAudioOutput()
            self._reproductor.setAudioOutput(self._salida_audio)
        if salida_video is not None:
            self._reproductor.setVideoOutput(salida_video)

        self._oyentes: list[OyenteReproduccion] = []
        self._elementos: tuple[ElementoPlaylist, ...] = ()
        self._indice = -1
        self._rango: RangoReproduccion | None = None
        self._estado = EstadoReproduccion.INACTIVO
        self._reproducir_al_cargar = False
        self._buscar_al_cargar: float | None = None
        self._liberado = False

        self._reproductor.positionChanged.connect(self._al_avanzar)
        self._reproductor.durationChanged.connect(self._al_medir)
        self._reproductor.mediaStatusChanged.connect(self._al_cambiar_medio)
        self._reproductor.playbackStateChanged.connect(self._al_cambiar_estado)
        self._reproductor.errorOccurred.connect(self._al_fallar)

    # -- PlaybackPort -------------------------------------------------------- #

    def suscribir(self, oyente: OyenteReproduccion) -> None:
        self._oyentes.append(oyente)

    def cargar(self, elementos: Sequence[ElementoPlaylist], indice: int = 0) -> None:
        self._elementos = tuple(elementos)
        self._reproducir_al_cargar = False
        if not self._elementos:
            self._indice, self._rango = -1, None
            self._reproductor.setSource(QUrl())
            self._publicar(EstadoReproduccion.INACTIVO)
            return
        self._activar(min(max(indice, 0), len(self._elementos) - 1))

    def seleccionar(self, indice: int) -> None:
        if not 0 <= indice < len(self._elementos) or indice == self._indice:
            return
        # Cambiar de tramo conserva la intencion: quien estaba viendo sigue viendo.
        self._reproducir_al_cargar = self._estado is EstadoReproduccion.REPRODUCIENDO
        self._activar(indice)

    def reproducir(self) -> None:
        if self._indice < 0:
            return
        self._reproducir_al_cargar = True
        if self._medio_listo():
            self._reproductor.play()

    def pausar(self) -> None:
        self._reproducir_al_cargar = False
        self._reproductor.pause()

    def buscar(self, segundos: float) -> None:
        destino = self._rango.acotar(segundos) if self._rango else max(0.0, segundos)
        if self._medio_listo():
            self._reproductor.setPosition(_milisegundos(destino))
        else:
            self._buscar_al_cargar = destino

    def fijar_rango(self, rango: RangoReproduccion | None) -> None:
        self._rango = rango
        if 0 <= self._indice < len(self._elementos):
            elementos = list(self._elementos)
            actual = elementos[self._indice]
            elementos[self._indice] = ElementoPlaylist(actual.elemento_id, actual.ruta,
                                                       rango, actual.etiqueta)
            self._elementos = tuple(elementos)
        if rango is not None and not rango.contiene(self._posicion()):
            self.buscar(rango.inicio)
        else:
            self._publicar(self._estado)

    def liberar(self) -> None:
        """Suelta el archivo y los recursos de Qt. Repetirlo no es un error."""
        if self._liberado:
            return
        self._liberado = True
        for senal, ranura in ((self._reproductor.positionChanged, self._al_avanzar),
                              (self._reproductor.durationChanged, self._al_medir),
                              (self._reproductor.mediaStatusChanged, self._al_cambiar_medio),
                              (self._reproductor.playbackStateChanged, self._al_cambiar_estado),
                              (self._reproductor.errorOccurred, self._al_fallar)):
            try:
                senal.disconnect(ranura)
            except (RuntimeError, TypeError):
                pass  # Qt ya lo desconecto al destruir el objeto: nada que soltar.
        self._reproductor.stop()
        self._reproductor.setSource(QUrl())
        self._reproductor.setVideoOutput(None)
        self._reproductor.setAudioOutput(None)
        self._salida_audio = None
        self._reproductor.deleteLater()
        self._elementos, self._indice, self._rango = (), -1, None
        self._estado = EstadoReproduccion.INACTIVO

    # -- senales de Qt ------------------------------------------------------- #

    def _al_avanzar(self, milisegundos: int) -> None:
        if self._rango is not None and _segundos(milisegundos) >= self._rango.fin:
            self._cerrar_tramo()
            return
        self._publicar(self._estado, milisegundos)

    def _al_medir(self, _milis: int) -> None:
        self._publicar(self._estado)

    def _al_cambiar_medio(self, estado: QMediaPlayer.MediaStatus) -> None:
        if estado in (QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia):
            destino = self._buscar_al_cargar
            self._buscar_al_cargar = None
            if destino is not None:
                self._reproductor.setPosition(_milisegundos(destino))
            if self._reproducir_al_cargar:
                self._reproductor.play()
        elif estado is QMediaPlayer.MediaStatus.EndOfMedia:
            self._cerrar_tramo()
        elif estado is QMediaPlayer.MediaStatus.InvalidMedia:
            self._reportar("medio_invalido", "No se pudo leer este medio.")

    def _al_cambiar_estado(self, estado: QMediaPlayer.PlaybackState) -> None:
        if self._estado in (EstadoReproduccion.FALLIDO, EstadoReproduccion.FINALIZADO) \
                and estado is not QMediaPlayer.PlaybackState.PlayingState:
            return  # Un `stop` provocado por el propio fallo no lo borra.
        if estado is QMediaPlayer.PlaybackState.PlayingState:
            self._publicar(EstadoReproduccion.REPRODUCIENDO)
        elif estado is QMediaPlayer.PlaybackState.PausedState:
            self._publicar(EstadoReproduccion.PAUSADO)
        else:
            self._publicar(EstadoReproduccion.INACTIVO)

    def _al_fallar(self, _error: QMediaPlayer.Error, cadena: str) -> None:
        self._reportar("backend", cadena or "El reproductor no pudo continuar.")

    # -- interno ------------------------------------------------------------- #

    def _activar(self, indice: int) -> None:
        self._indice = indice
        elemento = self._elementos[indice]
        self._rango = elemento.rango
        self._buscar_al_cargar = elemento.inicio
        self._publicar(EstadoReproduccion.CARGANDO, 0)
        self._reproductor.setSource(QUrl.fromLocalFile(elemento.ruta))

    def _cerrar_tramo(self) -> None:
        """Fin de rango o de archivo: continuar la playlist o quedarse quieto."""
        if self._indice + 1 < len(self._elementos):
            self._reproducir_al_cargar = self._estado is EstadoReproduccion.REPRODUCIENDO
            self._activar(self._indice + 1)
            return
        self._reproducir_al_cargar = False
        self._reproductor.pause()
        inicio = self._rango.inicio if self._rango else 0.0
        self._reproductor.setPosition(_milisegundos(inicio))
        self._publicar(EstadoReproduccion.FINALIZADO, _milisegundos(inicio))

    def _medio_listo(self) -> bool:
        return self._reproductor.mediaStatus() in (
            QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia,
            QMediaPlayer.MediaStatus.BufferingMedia, QMediaPlayer.MediaStatus.EndOfMedia)

    def _posicion(self) -> float:
        return _segundos(self._reproductor.position())

    def _identidad(self) -> str:
        return self._elementos[self._indice].elemento_id if 0 <= self._indice < len(self._elementos) else ""

    def _publicar(self, estado: EstadoReproduccion, milisegundos: int | None = None) -> None:
        self._estado = estado
        posicion = self._posicion() if milisegundos is None else _segundos(milisegundos)
        progreso = ProgresoReproduccion(self._indice, self._identidad(), estado, posicion,
                                        _segundos(self._reproductor.duration()), self._rango)
        for oyente in tuple(self._oyentes):
            oyente.progreso(progreso)

    def _reportar(self, codigo: str, mensaje: str) -> None:
        fallo = FalloReproduccion(self._identidad(), codigo, mensaje)
        self._publicar(EstadoReproduccion.FALLIDO)
        for oyente in tuple(self._oyentes):
            oyente.fallo(fallo)
