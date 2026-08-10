"""Colocacion de los bytes de una fuente dentro de la carpeta del proyecto.

Tres reglas, heredadas de T02 y T03 porque el problema es el mismo:

1. **Nada se publica a medias.** Copiar o mover una fuente termina en un
   `os.replace` sobre un temporal ya sincronizado. Un corte deja el temporal, no
   una fuente truncada que la siguiente apertura tomaria por buena.
2. **Ninguna ruta se compone sin validarla.** Un nombre de archivo llega del
   mundo —de una URL, de un explorador, de un contenedor— y acaba siendo un
   tramo de `sources/`. Se normaliza con la gramatica de los dos sistemas antes
   de tocar disco.
3. **La limpieza nunca borra lo que no reconoce.** Solo se retiran los
   temporales que esta capa crea, y solo cuando su antiguedad los declara
   abandonados. No hay borrado recursivo de nada que no naciera aqui.

La tercera regla obliga a una decision de nombres que no es cosmetica. Mientras
los parciales vivieron en `sources/<final>.tmp`, "temporal" y "fuente publicada"
compartian namespace: una fuente que la persona llamo `capture.tmp` era
indistinguible de un residuo, y bastaba el umbral de antiguedad para borrarla.
Aqui los parciales viven **fuera de `sources/`**, en `cache/ingest/` y con una
gramatica que ningun nombre de usuario puede producir:

| Residuo | Nombre | Marca adicional |
| --- | --- | --- |
| staging de descarga | `stg-<32 hex>/` | archivo `.clipsapp-ingest` dentro |
| copia parcial | `parcial-<32 hex>.part` | ninguna: el nombre ya es exacto |

Con eso `sources/` deja de tener residuos y la limpieza **jamas** lo recorre: no
existe forma de que borre una fuente. Y dentro de `cache/ingest/`, un hijo que
no encaje exactamente en la gramatica —o un directorio sin su marcador— se deja
en paz por viejo que sea, porque solo puede haberlo puesto alguien que no es
esta capa.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import re
import shutil
import time
import unicodedata
import uuid

from clipperkick.application.ingest.ports import UbicacionFuente
from clipperkick.domain.ingest import ErrorFuenteIngesta, SourceAsset
from clipperkick.domain.jobs import es_componente_interno
from clipperkick.domain.jobs.identity import CARACTERES_PROHIBIDOS

from ..durability import sincronizar_archivo, sincronizar_directorio


DIRECTORIO_FUENTES = "sources"
DIRECTORIO_STAGING = "ingest"
NOMBRE_POR_DEFECTO = "fuente"

#: Gramatica exacta de los residuos que esta capa crea. Se comprueba con una
#: expresion anclada y no con un prefijo o un sufijo: `stg-loquesea` o
#: `algo.part` no los produce nadie de aqui, y tratarlos como propios seria
#: exactamente el fallo que esta separacion existe para evitar.
PREFIJO_STAGING = "stg-"
SUFIJO_PARCIAL = ".part"
PREFIJO_PARCIAL = "parcial-"
RE_STAGING = re.compile(r"^stg-[0-9a-f]{32}$")
RE_PARCIAL = re.compile(r"^parcial-[0-9a-f]{32}\.part$")
#: Marcador que un staging deja dentro de si mismo. Un directorio con el nombre
#: correcto pero sin marcador no se retira: el nombre lo puede imitar cualquiera,
#: el marcador solo lo escribe `staging()`.
NOMBRE_MARCADOR = ".clipsapp-ingest"
CONTENIDO_MARCADOR = b"clipsapp-ingest/1\n"

#: Como quedaron origen y destino tras colocar los bytes. Decide dos cosas
#: distintas —como compensar si la barrera falla, y si hay que retirar el
#: original despues— y por eso no basta con un booleano.
#:
#: - `renombrada`: el `os.replace` consumio el origen. Compensar es devolverlo,
#:   y se puede porque un rename solo ocurre dentro de un mismo volumen.
#: - `copiada`: hay copia y el origen sigue donde estaba; no es nuestro.
#: - `copiada_para_mover`: hay copia, el origen sigue y **hay que retirarlo**,
#:   pero solo despues de confirmar el destino.
COLOCACION_RENOMBRADA = "renombrada"
COLOCACION_COPIADA = "copiada"
COLOCACION_COPIADA_PARA_MOVER = "copiada_para_mover"
#: El nombre visible se recorta para que `<uuid>_<nombre>` quepa holgadamente en
#: el limite de componente que el dominio impone (128) y ademas deje sitio al
#: sufijo temporal durante la publicacion.
LONGITUD_NOMBRE = 80


def _nombre_seguro(nombre: str) -> str:
    """Nombre visible reducido a un componente de ruta valido en ambos sistemas."""
    candidato = unicodedata.normalize("NFC", nombre or "").replace("\\", "/").rsplit("/", 1)[-1]
    candidato = "".join("_" if caracter in CARACTERES_PROHIBIDOS else caracter
                        for caracter in candidato).strip(" .")
    if len(candidato) > LONGITUD_NOMBRE:
        tronco, punto, extension = candidato.rpartition(".")
        if punto and 0 < len(extension) <= 8:
            candidato = tronco[:LONGITUD_NOMBRE - len(extension) - 1].rstrip(" .") + "." + extension
        else:
            candidato = candidato[:LONGITUD_NOMBRE].rstrip(" .")
    if not candidato or not es_componente_interno(candidato):
        return NOMBRE_POR_DEFECTO
    return candidato


class AlmacenFuentesProyecto:
    """Implementa `AlmacenFuentes` sobre la carpeta abierta por T02."""

    def __init__(self, raiz: str | Path) -> None:
        self.raiz = Path(raiz)

    # -- rutas ---------------------------------------------------------- #

    @property
    def _fuentes(self) -> Path:
        return self.raiz / DIRECTORIO_FUENTES

    @property
    def _staging(self) -> Path:
        return self.raiz / "cache" / DIRECTORIO_STAGING

    def _interna(self, relativa: str) -> Path:
        """Unica puerta de una ruta relativa persistida a una ruta real."""
        candidata = Path(relativa)
        if candidata.is_absolute() or ".." in candidata.parts:
            raise ErrorFuenteIngesta(f"La ruta interna {relativa!r} no es valida.")
        resuelta = (self.raiz / candidata).resolve()
        raiz = self.raiz.resolve()
        if resuelta != raiz and raiz not in resuelta.parents:
            raise ErrorFuenteIngesta(f"La ruta interna {relativa!r} escapa del proyecto.")
        return resuelta

    # -- staging -------------------------------------------------------- #

    @contextmanager
    def staging(self) -> Iterator[str]:
        """Directorio temporal dentro del proyecto, retirado siempre al salir.

        Vive bajo `cache/` y no en el temporal del sistema por dos razones: una
        descarga de varios gigabytes no debe llenar el disco del sistema, y el
        `os.replace` que la publica solo es atomico dentro del mismo volumen.

        El marcador se escribe *antes* de ceder el control: si se escribiese
        despues, un corte entre el `mkdir` y el `yield` dejaria un directorio con
        forma de staging que la limpieza no reconoceria como propio y que por
        tanto nadie retiraria nunca.
        """
        destino = self._staging / f"{PREFIJO_STAGING}{uuid.uuid4().hex}"
        destino.mkdir(parents=True)
        (destino / NOMBRE_MARCADOR).write_bytes(CONTENIDO_MARCADOR)
        try:
            yield str(destino)
        finally:
            shutil.rmtree(destino, ignore_errors=True)

    def _parcial(self) -> Path:
        """Ruta de una copia a medias. Nunca dentro de `sources/`."""
        self._staging.mkdir(parents=True, exist_ok=True)
        return self._staging / f"{PREFIJO_PARCIAL}{uuid.uuid4().hex}{SUFIJO_PARCIAL}"

    # -- incorporacion -------------------------------------------------- #

    def normalizar(self, entrada: str) -> str:
        return str(self._exigir_archivo(entrada))

    def _exigir_archivo(self, entrada: str) -> Path:
        candidata = Path(entrada).expanduser()
        try:
            resuelta = candidata.resolve()
        except OSError as error:
            raise ErrorFuenteIngesta(f"No se pudo resolver la ruta {entrada!r}.") from error
        if not resuelta.is_file():
            raise ErrorFuenteIngesta(f"No encuentro la fuente {entrada!r}.")
        return resuelta

    def incorporar(self, origen: str, nombre: str = "", mover: bool = False) -> UbicacionFuente:
        """Deja los bytes dentro de `sources/` y devuelve su ruta relativa.

        La incorporacion es **una transaccion de sistema de archivos**, y la
        barrera de directorio forma parte de ella. Dejarla fuera del bloque
        protegido —como estaba— produce el peor desenlace posible: el `rename` ya
        ocurrio, el archivo final existe, pero el metodo sale por excepcion, de
        modo que el llamador no llega a crear su token de propiedad y `sources/`
        no se recorre nunca durante la limpieza. El resultado es una fuente
        huerfana que nadie referencia y nadie retira.

        Aqui, si la barrera falla, se compensa **antes** de propagar: la copia se
        borra y un movimiento devuelve los bytes a su origen, de forma que quien
        llama recibe un `ErrorFuenteIngesta` sabiendo que el mundo quedo como
        estaba.

        Con el `mover` entre volumenes hay una vuelta de tuerca. Alli el traslado
        se hace copiando, y borrar el origen antes de confirmar el destino deja
        una compensacion **imposible**: restaurar exigiria un `os.replace` entre
        dispositivos distintos, que es justo lo que `EXDEV` prohibe. Por eso el
        origen sobrevive hasta que el destino esta durablemente confirmado, y
        solo entonces se retira.
        """
        fuente = self._exigir_archivo(origen)
        visible = _nombre_seguro(nombre or fuente.name)
        relativa = f"{DIRECTORIO_FUENTES}/{uuid.uuid4().hex}_{visible}"
        destino = self._interna(relativa)
        destino.parent.mkdir(parents=True, exist_ok=True)
        colocacion = self._colocar(fuente, destino, mover, origen)
        try:
            sincronizar_directorio(destino.parent)
        except OSError as error:
            self._deshacer(destino, fuente, colocacion == COLOCACION_RENOMBRADA)
            raise ErrorFuenteIngesta(
                f"No se pudo confirmar la incorporacion de {origen!r} al proyecto.") from error
        pendiente = (self._retirar_origen(fuente)
                     if colocacion == COLOCACION_COPIADA_PARA_MOVER else None)
        return UbicacionFuente(ruta_absoluta=str(destino), ruta_relativa=relativa, nombre=visible,
                               origen_pendiente=pendiente)

    def _retirar_origen(self, fuente: Path) -> str | None:
        """Retira el original ya trasladado. Devuelve la ruta si no pudo.

        Falla despues de que el destino este confirmado, de modo que deshacer la
        incorporacion seria destruir trabajo bueno por un residuo. Pero tampoco
        se calla: devolver la ruta convierte "quedo una copia del original" en un
        hecho que el llamador puede diagnosticar, en vez de un exito ambiguo con
        basura invisible detras.
        """
        try:
            fuente.unlink()
        except OSError:
            return str(fuente)
        return None

    def _colocar(self, fuente: Path, destino: Path, mover: bool, origen: str) -> str:
        """Deja los bytes en `destino` y dice como, para poder compensarlo."""
        # El parcial vive en `cache/ingest/`, no junto al destino: mientras estuvo
        # en `sources/` compartia namespace con las fuentes publicadas y una
        # fuente llamada `algo.tmp` era indistinguible de un residuo.
        temporal = self._parcial()
        if mover and self._mismo_volumen(fuente, destino.parent):
            # Mover dentro del mismo volumen es una operacion de directorio: no
            # hay que copiar gigabytes ni sincronizar contenido que ya esta en
            # disco desde que lo escribio quien lo produjo.
            try:
                os.replace(fuente, destino)
            except OSError as error:
                raise ErrorFuenteIngesta(
                    f"No se pudo incorporar la fuente {origen!r} al proyecto.") from error
            return COLOCACION_RENOMBRADA
        try:
            shutil.copyfile(fuente, temporal)
            sincronizar_archivo(temporal)
            # El destino nace con el rename: mientras se copia, el nombre final
            # no existe y nadie puede tomar el temporal por una fuente.
            os.replace(temporal, destino)
        except OSError as error:
            # Solo se retira lo que esta rama creo. `destino` no se toca: si el
            # rename llego a ocurrir, ya no queda nada que pueda fallar aqui.
            temporal.unlink(missing_ok=True)
            raise ErrorFuenteIngesta(
                f"No se pudo incorporar la fuente {origen!r} al proyecto.") from error
        # El original **no** se toca todavia. Esta rama es la que se usa cuando
        # origen y destino viven en volumenes distintos, y alli borrarlo ahora
        # haria irreparable cualquier fallo posterior: la compensacion tendria
        # que devolver los bytes con un `os.replace` entre dispositivos, que es
        # exactamente lo que `EXDEV` impide.
        return COLOCACION_COPIADA_PARA_MOVER if mover else COLOCACION_COPIADA

    def _deshacer(self, destino: Path, fuente: Path, restaurar_origen: bool) -> None:
        """Revierte una incorporacion que no se pudo confirmar.

        Restaurar el origen —y no solo borrar el destino— importa en la rama
        `mover=True`: los bytes venian de un staging que el context manager va a
        retirar, y borrar el destino sin devolverlos perderia una descarga
        entera. Devolverlos deja al llamador exactamente donde estaba.

        Todo es el mejor esfuerzo y nada vuelve a levantar: el hecho que hay que
        contar es el fallo de la barrera, no el de la compensacion.
        """
        try:
            if restaurar_origen:
                os.replace(destino, fuente)
            else:
                destino.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            sincronizar_directorio(destino.parent)
        except OSError:
            pass

    def _mismo_volumen(self, origen: Path, destino: Path) -> bool:
        try:
            return origen.stat().st_dev == destino.stat().st_dev
        except OSError:
            return False

    def referenciar(self, origen: str) -> UbicacionFuente:
        """Fuente externa: se normaliza la ruta y no se toca el archivo jamas."""
        fuente = self._exigir_archivo(origen)
        return UbicacionFuente(ruta_absoluta=str(fuente), ruta_relativa=None, nombre=fuente.name)

    # -- localizacion --------------------------------------------------- #

    def resolver(self, fuente: SourceAsset) -> str:
        if fuente.ruta_relativa is not None:
            return str(self._interna(fuente.ruta_relativa))
        return str(fuente.ruta_externa)

    def disponible(self, fuente: SourceAsset) -> bool:
        try:
            return Path(self.resolver(fuente)).is_file()
        except ErrorFuenteIngesta:
            return False

    def retirar(self, ruta_relativa: str) -> None:
        """Retira un archivo que esta capa deposito. Nunca sale del proyecto."""
        destino = self._interna(ruta_relativa)
        if destino.is_symlink() or not destino.is_file():
            return
        destino.unlink(missing_ok=True)

    # -- limpieza ------------------------------------------------------- #

    def limpiar_abandonados(self, antiguedad: float,
                            ahora: float | None = None) -> tuple[str, ...]:
        """Retira staging y parciales abandonados. Nunca entra en `sources/`.

        Tres filtros, y los tres tienen que pasar antes de borrar nada:

        1. **Ubicacion.** Solo hijos directos de `cache/ingest/`. `sources/` no
           se recorre en ningun caso: alli no hay residuos que retirar.
        2. **Gramatica exacta.** `stg-<32 hex>` para el staging y
           `parcial-<32 hex>.part` para una copia a medias, mas el marcador
           dentro del directorio. Un hijo que no encaje es de otro y se queda,
           por viejo que sea.
        3. **Antiguedad real.** Se mide la actividad mas reciente del arbol —el
           propio directorio y sus hijos—, no solo la entrada del contenedor:
           un staging al que se le acaban de escribir gigabytes tiene el
           directorio viejo y el contenido nuevo.

        Un enlace simbolico nunca se retira —seguirlo permitiria borrar algo de
        fuera— y un error del sistema de archivos solo difiere la limpieza: no
        retirar basura es una molestia, borrar lo ajeno seria un fallo.
        """
        instante = time.time() if ahora is None else ahora
        retirados: list[str] = []
        for hijo in _hijos(self._staging):
            if hijo.is_symlink():
                continue
            if hijo.is_dir():
                if not RE_STAGING.match(hijo.name) or not _tiene_marcador(hijo):
                    continue
                if not _abandonado(_actividad(hijo), instante, antiguedad):
                    continue
                shutil.rmtree(hijo, ignore_errors=True)
                if not hijo.exists():
                    retirados.append(self._relativa(hijo))
            elif hijo.is_file():
                if not RE_PARCIAL.match(hijo.name):
                    continue
                if not _abandonado(_mtime(hijo), instante, antiguedad):
                    continue
                try:
                    hijo.unlink()
                except OSError:
                    continue
                retirados.append(self._relativa(hijo))
        return tuple(sorted(retirados))

    def _relativa(self, ruta: Path) -> str:
        try:
            return ruta.relative_to(self.raiz).as_posix()
        except ValueError:
            return ruta.as_posix()


def _hijos(directorio: Path) -> tuple[Path, ...]:
    try:
        return tuple(sorted(directorio.iterdir()))
    except OSError:
        return ()


def _tiene_marcador(directorio: Path) -> bool:
    """El marcador tiene que ser un archivo real y con nuestro contenido."""
    marcador = directorio / NOMBRE_MARCADOR
    try:
        return (not marcador.is_symlink() and marcador.is_file()
                and marcador.read_bytes() == CONTENIDO_MARCADOR)
    except OSError:
        return False


def _mtime(ruta: Path) -> float | None:
    try:
        return ruta.stat().st_mtime
    except OSError:
        return None


def _actividad(directorio: Path) -> float | None:
    """Instante de la ultima actividad observable dentro del arbol.

    Se recorre el contenido porque la entrada de un directorio solo cambia
    cuando se anaden o quitan entradas: un staging al que se le lleva una hora
    escribiendo el mismo archivo tiene el directorio "viejo" y el contenido
    recien tocado. Juzgarlo por el directorio lo declararia abandonado en plena
    descarga.
    """
    instantes = [valor for valor in (_mtime(directorio),) if valor is not None]
    try:
        for hijo in directorio.rglob("*"):
            if hijo.is_symlink():
                continue
            valor = _mtime(hijo)
            if valor is not None:
                instantes.append(valor)
    except OSError:
        return None
    return max(instantes) if instantes else None


def _abandonado(instante: float | None, ahora: float, antiguedad: float) -> bool:
    """Sin instante legible no se decide: no borrar es siempre lo reversible."""
    return instante is not None and (ahora - instante) >= antiguedad
