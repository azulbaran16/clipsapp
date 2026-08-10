"""Ensamblado por defecto de la ingesta sobre un proyecto abierto.

Existe por la misma razon que `jobs/wiring.py`: que el cableado sea *uno solo*.
Si el almacen de fuentes apuntase a una carpeta, el de artefactos a otra y el
repositorio a una tercera base, el fallo aparecerian mucho mas tarde y mucho
peor —artefactos publicados que ninguna fila explica, fuentes registradas cuyos
bytes viven en otro proyecto—.

Un proyecto en solo lectura no puede ingerir: no posee el lock de escritor y por
tanto no puede ni copiar fuentes ni confirmar filas. Se rechaza aqui, con un
error tipado, en vez de fallar mas tarde y de forma menos clara.
"""

from __future__ import annotations

from clipperkick.application.ingest import CasoDeUsoIngesta
from clipperkick.application.ingest.ports import (
    ANTIGUEDAD_TEMPORALES, DescargadorFuente, Notificador, SondaDetallada, sin_notificar,
)
from clipperkick.domain.ingest import ErrorIngesta
from clipperkick.infrastructure.project.persistence import ProyectoAbierto

from .artifacts import AlmacenArtefactosIngestaProyecto
from .download import DescargadorYtDlp
from .fingerprints import ServicioHuellaArchivo
from .probe import SondaFfprobeDetallada
from .repository import RepositorioSqliteFuentes
from .sources import AlmacenFuentesProyecto


def crear_caso_de_uso_ingesta(proyecto: ProyectoAbierto, *,
                              sonda: SondaDetallada | None = None,
                              descargador: DescargadorFuente | None = None,
                              notificar: Notificador = sin_notificar,
                              antiguedad_temporales: float = ANTIGUEDAD_TEMPORALES,
                              ) -> CasoDeUsoIngesta:
    """Ensambla la ingesta con los adaptadores reales del proyecto abierto."""
    if proyecto.solo_lectura:
        raise ErrorIngesta("Un proyecto abierto en solo lectura no puede ingerir fuentes.")
    raiz = proyecto.repositorio.raiz
    return CasoDeUsoIngesta(
        almacen_fuentes=AlmacenFuentesProyecto(raiz),
        # El servicio de huellas necesita un sitio propio donde medir si el
        # volumen mueve `ctime` al escribir. Se le da el staging del proyecto:
        # esta en el mismo volumen que las fuentes copiadas —que son las que
        # llegan a tener tamano— y es area nuestra, no de la persona.
        huellas=ServicioHuellaArchivo(directorio_prueba=raiz / "cache" / "ingest"),
        sonda=sonda if sonda is not None else SondaFfprobeDetallada(),
        artefactos=AlmacenArtefactosIngestaProyecto(raiz),
        repositorio=RepositorioSqliteFuentes(proyecto.repositorio),
        descargador=descargador if descargador is not None else DescargadorYtDlp(),
        notificar=notificar, antiguedad_temporales=antiguedad_temporales)
