"""Que reproductores existen realmente en este equipo, y cual se usa.

El spike de T08 compara tres candidatos y solo uno quedo integrado; los otros
dos siguen nombrados aqui a proposito. Un sondeo que diga "libmpv no carga
porque falta `libmpv-2.dll`" es la diferencia entre una decision revisable y una
afirmacion de memoria: cuando alguien quiera repetir la comparacion, el harness
sabra si puede medir o no, y por que.

El sondeo distingue dos ausencias distintas porque exigen remedios distintos: el
binding de Python se instala con `pip`, la biblioteca nativa no.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from clipperkick.domain.playback import ErrorBackendNoDisponible


QTMULTIMEDIA = "qtmultimedia"
LIBMPV = "libmpv"
VLC = "vlc"


@dataclass(frozen=True)
class Disponibilidad:
    nombre: str
    disponible: bool
    version: str = ""
    motivo: str = ""
    remedio: str = ""


def sondear_qtmultimedia() -> Disponibilidad:
    try:
        core = importlib.import_module("PySide6.QtCore")
    except ImportError as error:
        return Disponibilidad(QTMULTIMEDIA, False, motivo=str(error),
                              remedio="pip install PySide6")
    try:
        importlib.import_module("PySide6.QtMultimedia")
    except ImportError as error:
        # QtMultimedia viaja en PySide6-Addons, no en PySide6-Essentials: una
        # instalacion "minima" de Qt deja la carcasa en pie y sin reproductor.
        return Disponibilidad(QTMULTIMEDIA, False, version=core.qVersion(), motivo=str(error),
                              remedio="pip install PySide6 (Addons incluye QtMultimedia)")
    return Disponibilidad(QTMULTIMEDIA, True, version=core.qVersion())


def sondear_libmpv() -> Disponibilidad:
    try:
        mpv = importlib.import_module("mpv")
    except ImportError as error:
        return Disponibilidad(LIBMPV, False, motivo=str(error), remedio="pip install python-mpv")
    try:
        instancia = mpv.MPV(video=False)
    except Exception as error:  # OSError de la carga del DLL, y variantes del binding.
        return Disponibilidad(LIBMPV, False, motivo=str(error),
                              remedio="instalar libmpv-2.dll en PATH (no viene con pip)")
    try:
        return Disponibilidad(LIBMPV, True, version=str(instancia.mpv_version))
    finally:
        instancia.terminate()


def sondear_vlc() -> Disponibilidad:
    try:
        vlc = importlib.import_module("vlc")
    except (ImportError, OSError, NameError) as error:
        # `python-vlc` resuelve `libvlc` al importarse: sin VLC instalado el
        # propio import falla, y no siempre con `ImportError`.
        return Disponibilidad(VLC, False, motivo=str(error), remedio="pip install python-vlc")
    try:
        instancia = vlc.Instance("--no-video-title-show")
        if instancia is None:
            raise OSError("libvlc no devolvio una instancia")
    except Exception as error:
        return Disponibilidad(VLC, False, motivo=str(error),
                              remedio="instalar VLC (libvlc) en el sistema")
    try:
        return Disponibilidad(VLC, True, version=str(vlc.libvlc_get_version().decode("utf-8", "replace")))
    finally:
        instancia.release()


SONDEOS = {QTMULTIMEDIA: sondear_qtmultimedia, LIBMPV: sondear_libmpv, VLC: sondear_vlc}


def sondear() -> tuple[Disponibilidad, ...]:
    return tuple(sondeo() for sondeo in SONDEOS.values())


def crear_reproductor(salida_video=None, *, con_audio: bool = True):
    """Devuelve el reproductor elegido en el spike, o explica por que no puede.

    No hay cascada silenciosa a otro backend: la eleccion esta documentada y un
    reemplazo automatico produciria capturas de pantalla y mediciones que nadie
    podria atribuir. Si falta QtMultimedia, la carcasa degrada de forma visible.
    """
    estado = sondear_qtmultimedia()
    if not estado.disponible:
        raise ErrorBackendNoDisponible(
            f"El reproductor no esta disponible: {estado.motivo}. Solucion: {estado.remedio}.")
    from .qtmultimedia import AdaptadorQtMultimedia

    return AdaptadorQtMultimedia(salida_video, con_audio=con_audio)
