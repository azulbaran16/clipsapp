"""Almacen de artefactos de analisis sobre la publicacion durable de T04."""

from __future__ import annotations

from pathlib import Path

from clipperkick.domain.ingest import ArchivoArtefacto, ErrorArtefactoIngesta
from clipperkick.domain.jobs import exigir_ruta_interna
from clipperkick.infrastructure.ingest.artifacts import (
    AlmacenArtefactosIngestaProyecto, digest_archivo,
)


class AlmacenArtefactosAnalisisProyecto(AlmacenArtefactosIngestaProyecto):
    """Anade archivos grandes y resolucion de rutas al almacen de T04.

    La publicacion, la verificacion de checksums y la adopcion tras un corte
    siguen siendo exactamente las de ingesta. Esta clase solo permite que una
    etapa escriba directamente un WAV en su staging sin cargarlo en memoria.
    """

    def ruta_en(self, directorio: str, ruta: str) -> str:
        destino = Path(directorio)
        for parte in exigir_ruta_interna(ruta, "ruta del artefacto de analisis"):
            destino = destino / parte
            if destino.is_symlink():
                raise ErrorArtefactoIngesta(
                    f"El artefacto intenta escribir a traves de un enlace: {ruta!r}.")
        destino.parent.mkdir(parents=True, exist_ok=True)
        return str(destino)

    def declarar(self, directorio: str, ruta: str) -> ArchivoArtefacto:
        destino = Path(self.ruta_en(directorio, ruta))
        if destino.is_symlink() or not destino.is_file():
            raise ErrorArtefactoIngesta(
                f"La etapa no produjo el archivo declarado {ruta!r}.")
        try:
            tamano = destino.stat().st_size
            checksum = digest_archivo(destino)
        except OSError as error:
            raise ErrorArtefactoIngesta(
                f"No se pudo verificar el archivo declarado {ruta!r}.") from error
        return ArchivoArtefacto(ruta=ruta, bytes=tamano, sha256=checksum)

    def ruta_publicada(self, clave: str, ruta: str) -> str:
        destino = self._publicado(clave)
        for parte in exigir_ruta_interna(ruta, "ruta publicada de analisis"):
            destino = destino / parte
            if destino.is_symlink():
                raise ErrorArtefactoIngesta(
                    f"El artefacto publicado atraviesa un enlace: {ruta!r}.")
        return str(destino)


__all__ = ["AlmacenArtefactosAnalisisProyecto"]
