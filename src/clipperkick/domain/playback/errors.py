"""Errores tipados de reproduccion, sin nombrar a ningun reproductor."""

from clipperkick.domain.errors import ErrorAmigable


class ErrorReproduccion(ErrorAmigable):
    """Una operacion de reproduccion no pudo plantearse siquiera."""


class ErrorBackendNoDisponible(ErrorReproduccion):
    """El reproductor elegido no esta instalado o no carga en este equipo.

    Se distingue del fallo de un medio concreto porque su recuperacion es otra:
    aqui no sirve reintentar ni cambiar de archivo; falta una dependencia y la
    aplicacion debe degradar de forma explicita.
    """
