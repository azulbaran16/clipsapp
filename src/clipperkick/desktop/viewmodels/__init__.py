"""View models de la carcasa: estado observable sin Qt ni adaptadores."""

from .observable import Emisor
from .playback import EstadoReproductor, ViewModelReproduccion
from .ports import Ejecutor, EjecutorInmediato
from .shell import EstadoCarcasa, Pantalla, ViewModelCarcasa

__all__ = ["Ejecutor", "EjecutorInmediato", "Emisor", "EstadoCarcasa",
           "EstadoReproductor", "Pantalla", "ViewModelCarcasa", "ViewModelReproduccion"]
