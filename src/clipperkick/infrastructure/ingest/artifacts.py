"""Almacen de artefactos de ingesta, sobre el almacen durable de T03.

No hay un segundo protocolo de publicacion: esta clase **compone** el
`AlmacenArtefactosProyecto` de T03 y solo traduce vocabulario. Lo que aporta es
la parte que el ticket exige y el almacen generico no tenia por que conocer:

- el productor **declara** el checksum de cada archivo, y la declaracion viaja
  en el manifiesto para que la publicacion la verifique antes de mover nada;
- el directorio publicado se nombra con la **clave de materializacion**, de modo
  que "buscar un hit de cache" y "buscar el artefacto" son la misma operacion, y
  la adopcion de una publicacion previa —identidad, linaje y bytes— la resuelve
  el almacen de T03 sin cambios;
- ningun intento bajo `cache/jobs/` se retira automaticamente: ahi es donde
  `cuarentena()` preserva la evidencia de un fallo, y barrerla por antiguedad
  destruiria justo lo que explica por que un sondeo no termino.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path

from clipperkick.domain.ingest import (
    ArchivoArtefacto, ErrorArtefactoIngesta, ManifiestoArtefacto, digest_bloque,
    es_clave_materializacion, etiquetar_checksum, nuevo_acumulador,
)
from clipperkick.domain.jobs import ArchivoSalida, ErrorValidacionSalida, ManifiestoSalida

from ..durability import escribir_bytes_durable
from ..jobs.artifacts import NOMBRE_PUBLICACION, AlmacenArtefactosProyecto


#: Prefijo del "intento" con el que la ingesta ocupa el staging de un trabajo.
#: El almacen le anade un sufijo aleatorio: mientras el nombre fue fijo,
#: `cache/jobs/<clave>/sondeo` era una ruta predecible que cada ingesta visitaba
#: y borraba, y eso bastaba para redirigir el borrado con un ancestro enlazado.
NOMBRE_INTENTO = "sondeo"
BLOQUE_DIGEST = 1 << 20


def digest_archivo(ruta: Path) -> str:
    """SHA-256 del contenido, por bloques: un artefacto no cabe en memoria."""
    acumulador = nuevo_acumulador()
    with ruta.open("rb") as archivo:
        for bloque in iter(lambda: archivo.read(BLOQUE_DIGEST), b""):
            acumulador.update(bloque)
    return acumulador.hexdigest()


class AlmacenArtefactosIngestaProyecto:
    """Implementa `AlmacenArtefactosIngesta` reutilizando el almacen de T03."""

    def __init__(self, raiz: str | Path,
                 almacen: AlmacenArtefactosProyecto | None = None) -> None:
        self.raiz = Path(raiz)
        self._almacen = almacen if almacen is not None else AlmacenArtefactosProyecto(self.raiz)

    # ------------------------------------------------------------------ #

    def _publicado(self, clave: str) -> Path:
        self._exigir_clave(clave)
        return self.raiz / "artifacts" / clave

    def _exigir_clave(self, clave: str) -> str:
        if not es_clave_materializacion(clave):
            raise ErrorArtefactoIngesta(
                f"{clave!r} no es una clave de materializacion valida.")
        return clave

    # ------------------------------------------------------------------ #

    def preparar(self, clave: str) -> str:
        return self._almacen.preparar(self._exigir_clave(clave), NOMBRE_INTENTO)

    def escribir(self, directorio: str, ruta: str, datos: bytes) -> ArchivoArtefacto:
        """Escribe el archivo ya sincronizado y devuelve lo que se declarara.

        El checksum se calcula sobre los bytes que se acaban de escribir y no
        sobre el archivo releido: releer mediria lo que hay, que es justo lo que
        la publicacion tiene que comprobar despues de forma independiente.
        """
        destino = Path(directorio) / Path(*ruta.replace("\\", "/").split("/"))
        destino.parent.mkdir(parents=True, exist_ok=True)
        escribir_bytes_durable(destino, datos)
        return ArchivoArtefacto(ruta=ruta, bytes=len(datos), sha256=digest_bloque(datos))

    def publicar(self, clave: str, directorio: str,
                 manifiesto: ManifiestoArtefacto) -> tuple[str, ...]:
        """Traduce el manifiesto y delega la publicacion durable en T03."""
        self._exigir_clave(clave)
        if manifiesto.linaje is None:
            raise ErrorArtefactoIngesta("Un artefacto de ingesta se publica con su linaje.")
        if manifiesto.linaje.clave != clave:
            raise ErrorArtefactoIngesta(
                f"El linaje declara la clave {manifiesto.linaje.clave!r} y se publica en {clave!r}.")
        salida = ManifiestoSalida(
            archivos=tuple(ArchivoSalida(archivo.ruta, archivo.bytes)
                           for archivo in manifiesto.archivos),
            metadata=manifiesto.metadata_publicable())
        try:
            return self._almacen.publicar(
                clave, directorio, salida, attempt_id=NOMBRE_INTENTO, clave=clave,
                salidas=tuple(archivo.ruta for archivo in manifiesto.archivos),
                stage=manifiesto.linaje.stage,
                version_contrato=manifiesto.linaje.version_contrato)
        except ErrorValidacionSalida as error:
            raise ErrorArtefactoIngesta(str(error)) from error

    def descartar(self, directorio: str) -> None:
        self._almacen.descartar(directorio)

    def cuarentena(self, directorio: str) -> str | None:
        return self._almacen.cuarentena(directorio)

    # ------------------------------------------------------------------ #

    def presentes(self, clave: str) -> Mapping[str, str]:
        """Lo que hay *ahora* publicado bajo la clave, con su digest real.

        Es deliberadamente una lectura de disco y no de la base: la pregunta que
        responde es "¿siguen estando estos bytes?", y contestarla con la fila
        que afirma que si la convertiria en una tautologia.
        """
        raiz = self._publicado(clave)
        if not raiz.is_dir():
            return {}
        encontrados: dict[str, str] = {}
        for hijo in sorted(raiz.rglob("*")):
            if hijo.is_symlink() or not hijo.is_file():
                continue
            relativa = hijo.relative_to(raiz).as_posix()
            if relativa == NOMBRE_PUBLICACION:
                continue  # sidecar de identidad: describe la publicacion, no es parte de ella
            encontrados[relativa] = digest_archivo(hijo)
        return encontrados

    def leer_documento(self, clave: str, ruta: str) -> Mapping[str, object]:
        """Lectura sin verificar. Solo para diagnostico: nunca para reutilizar.

        Quien decide reutilizar usa `leer_verificado`. La diferencia no es de
        estilo: entre comprobar los digests con `presentes()` y volver a abrir el
        archivo cabe una escritura, y lo que acabaria parseandose no serian los
        bytes que se aprobaron.
        """
        return self._parsear(clave, self._leer(clave, ruta))

    def leer_verificado(self, clave: str, ruta: str,
                        checksum: str) -> Mapping[str, object]:
        """Lee **una vez**, verifica esos bytes y parsea esos mismos bytes.

        La verificacion y el parseo comparten el `bytes` que devolvio la unica
        lectura, de modo que no existe instante en el que lo aprobado y lo
        interpretado puedan ser distintos. Si el digest no coincide se levanta un
        error tipado: quien lo recibe vuelve a producir el artefacto en vez de
        confiar en el que encontro.
        """
        if not checksum:
            raise ErrorArtefactoIngesta(
                f"El artefacto {clave!r} no declara checksum de {ruta!r}; no se puede reutilizar.")
        datos = self._leer(clave, ruta)
        if etiquetar_checksum(digest_bloque(datos)) != etiquetar_checksum(checksum):
            raise ErrorArtefactoIngesta(
                f"Los bytes publicados de {ruta!r} en {clave!r} ya no coinciden con su checksum.")
        return self._parsear(clave, datos)

    def _leer(self, clave: str, ruta: str) -> bytes:
        destino = self._publicado(clave) / Path(*ruta.replace("\\", "/").split("/"))
        try:
            return destino.read_bytes()
        except OSError as error:
            raise ErrorArtefactoIngesta(
                f"El artefacto publicado en {clave!r} no se puede leer.") from error

    def _parsear(self, clave: str, datos: bytes) -> Mapping[str, object]:
        try:
            documento = json.loads(datos.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise ErrorArtefactoIngesta(
                f"El artefacto publicado en {clave!r} no se puede leer.") from error
        if not isinstance(documento, Mapping):
            raise ErrorArtefactoIngesta(f"El artefacto publicado en {clave!r} no es un objeto.")
        return documento

    # ------------------------------------------------------------------ #

    def limpiar_abandonados(self, antiguedad: float,
                            ahora: float | None = None) -> tuple[str, ...]:
        """Ya no barre nada bajo `cache/jobs/`. Devuelve siempre vacio.

        Este barrido retiraba el staging de un sondeo abandonado, y desde F07 esa
        gramatica —`<clave>/sondeo-<32 hex>`— es **exactamente** donde
        `cuarentena()` preserva la evidencia de un intento fallido. Mientras la
        cuarentena se movia a `_cuarentena`, el nombre estaba excluido del
        barrido; al dejar de moverla, la exclusion dejo de aplicarle y cada
        ingesta borraba, pasada una hora, lo unico que explicaba el fallo.

        Se podria haber devuelto la exclusion con un marcador o un rename, pero
        ambas cosas vuelven a escribir sobre rutas re-resueltas, que es el patron
        que F06 y F07 retiraron. La respuesta consistente es no barrer: un
        intento bajo `cache/` es area reconstruible, y preferir una fuga a
        destruir evidencia es la misma eleccion que ya gobierna el resto.

        El metodo sobrevive porque el puerto lo declara y el caso de uso lo
        llama; lo que se retira es su capacidad de borrar. Los temporales
        propios de `cache/ingest/` y las copias a medias los sigue limpiando
        `AlmacenFuentesProyecto`, donde la propiedad y la gramatica siguen siendo
        inequivocas.
        """
        return ()
