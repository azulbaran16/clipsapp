"""Sondeo detallado con ffprobe.

Reutiliza la frontera de proceso que T01 ya definio (`Ejecutor`,
`BuscadorHerramienta`, `ejecutar_comando`) en vez de abrir otra: duplicarla
significaria dos formas de lanzar el mismo binario, dos formas de fingirlo en
pruebas y dos sitios donde arreglar el dia que haya que pasar un argumento mas.

Dos responsabilidades y ninguna mas:

1. **Devolver el documento crudo.** Normalizar es una regla de dominio
   versionada; si este adaptador normalizase, dos proveedores podrian producir
   metadatas distintas bajo la misma version de contrato.
2. **Declarar su version.** La version del proveedor entra en la clave de
   materializacion: actualizar FFmpeg tiene que invalidar los sondeos viejos, y
   la unica forma de que eso ocurra sin que nadie se acuerde es preguntarsela al
   binario. Cuando el numero no se puede leer se usa el digest de su propia
   salida `-version`, que cambia igual al cambiar de binario y nunca es una
   constante que finja saber algo.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re
import shutil

from clipperkick.domain.ingest import ErrorMediaDanada, ErrorMetadataMedia

from ..probe import BuscadorHerramienta, Ejecutor, ejecutar_comando


HERRAMIENTA = "ffprobe"
PREFIJO_VERSION = "ffprobe/"
RE_VERSION = re.compile(r"ffprobe version (\S+)")
#: `-show_error` hace que un archivo ilegible conteste con un objeto `error` en
#: vez de con un JSON vacio: la diferencia entre "no hay flujos" y "no se pudo
#: abrir" es exactamente la que separa un medio raro de uno danado.
ARGUMENTOS_SONDEO = ("-v", "error", "-show_error", "-show_format", "-show_streams", "-of", "json")


class SondaFfprobeDetallada:
    """Implementa `SondaDetallada` sin filtrar detalles de ffprobe hacia arriba."""

    def __init__(self, ejecutor: Ejecutor = ejecutar_comando,
                 buscar_herramienta: BuscadorHerramienta = shutil.which) -> None:
        self._ejecutor = ejecutor
        self._buscar_herramienta = buscar_herramienta
        self._version: str | None = None

    # ------------------------------------------------------------------ #

    def _ejecutable(self) -> str:
        ejecutable = self._buscar_herramienta(HERRAMIENTA)
        if ejecutable is None:
            raise ErrorMediaDanada(
                "No encuentro 'ffprobe'. Instala FFmpeg y vuelve a intentarlo.")
        return ejecutable

    def version(self) -> str:
        """Version del proveedor, memorizada por instancia.

        Se memoriza porque entra en cada clave de materializacion y volver a
        lanzar el proceso por cada fuente convertiria una ingesta de veinte
        archivos en veinte procesos extra sin aportar nada: el binario no cambia
        a mitad de sesion.
        """
        if self._version is not None:
            return self._version
        ejecutable = self._ejecutable()
        try:
            resultado = self._ejecutor((ejecutable, "-version"))
        except OSError as error:
            raise ErrorMediaDanada("No se pudo consultar la version de ffprobe.") from error
        salida = (resultado.salida or "") + (resultado.error or "")
        coincidencia = RE_VERSION.search(salida)
        if coincidencia:
            self._version = PREFIJO_VERSION + coincidencia.group(1)
        else:
            digest = hashlib.sha256(salida.encode("utf-8", errors="replace")).hexdigest()[:16]
            self._version = f"{PREFIJO_VERSION}sha256:{digest}"
        return self._version

    # ------------------------------------------------------------------ #

    def describir(self, ruta: str) -> Mapping[str, object]:
        """Documento crudo del sondeo. Un archivo ilegible es un error tipado."""
        ejecutable = self._ejecutable()
        try:
            resultado = self._ejecutor((ejecutable, *ARGUMENTOS_SONDEO, ruta))
        except OSError as error:
            raise ErrorMediaDanada(
                f"No se pudo iniciar la inspeccion de {ruta!r}.") from error
        if resultado.codigo_salida != 0:
            detalle = (resultado.error or "").strip().splitlines()
            raise ErrorMediaDanada(
                f"El archivo no se pudo inspeccionar: {detalle[-1] if detalle else 'sin detalle'}")
        try:
            documento = json.loads(resultado.salida)
        except (ValueError, TypeError) as error:
            raise ErrorMetadataMedia("La inspeccion no devolvio un documento legible.") from error
        if not isinstance(documento, Mapping):
            raise ErrorMetadataMedia("La inspeccion no devolvio un objeto.")
        error_declarado = documento.get("error")
        if isinstance(error_declarado, Mapping):
            # ffprobe puede terminar con codigo cero y aun asi declarar que no
            # supo abrir el archivo. Tratar eso como exito produciria una
            # metadata sin flujos y un diagnostico que culpa al normalizador.
            raise ErrorMediaDanada(
                f"El archivo no se pudo abrir: {error_declarado.get('string', 'sin detalle')}")
        return documento
