"""Modelos puros de reproduccion compartidos por UI y adaptadores."""

from .errors import ErrorBackendNoDisponible, ErrorReproduccion
from .models import (ElementoPlaylist, EstadoReproduccion, FalloReproduccion,
                     ProgresoReproduccion, RangoReproduccion)

__all__ = ["ElementoPlaylist", "ErrorBackendNoDisponible", "ErrorReproduccion",
           "EstadoReproduccion", "FalloReproduccion", "ProgresoReproduccion",
           "RangoReproduccion"]
