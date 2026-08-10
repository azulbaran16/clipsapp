"""Almacen de artefactos: staging temporal, validacion y publicacion atomica.

El worker nunca escribe donde se lee. Produce en `cache/jobs/<job>/<intento>/`,
declara un manifiesto, y solo el coordinador —tras comprobar que lo declarado
existe y mide lo que dice— mueve el resultado a `artifacts/<job>/`. La ultima
operacion es un unico `os.replace` de directorio: o el artefacto esta completo o
no esta, nunca a medias.

Del plan se toma literalmente una frase: *nunca se asume que la existencia de un
archivo temporal equivale a exito*. Cada intento estrena directorio con sufijo
aleatorio, de modo que un residuo anterior no se puede confundir con trabajo
bueno **ni hace falta borrarlo** para preparar el siguiente; un fallo lo conserva
donde esta para poder diagnosticarlo.

# Que se certifica, y sobre que bytes

Validar en el directorio del intento y publicar despues deja una ventana: nada
ata los bytes medidos a los bytes publicados, y una escritura en ese hueco
publica contenido que contradice su propio checksum y su propio sidecar.

Reordenar no basta, y conviene decir por que. Mover los archivos al staging
acerca la medida a la publicacion, pero el archivo movido **sigue siendo el
mismo inode**: un descriptor que el worker se quedo abierto escribe sobre el
contenido publicado por mucho que la ruta haya cambiado de sitio. Y un staging
de nombre deterministico es, ademas, un blanco al que apuntar.

Por eso la publicacion es un **sellado**, no un traslado:

1. cada salida declarada se **copia** a un archivo nuevo dentro de un staging de
   nombre imprevisible. La copia estrena inode: los descriptores que el
   productor conserve apuntan al original y no pueden alcanzarla;
2. se sincroniza el contenido y el arbol —todo lo que toca los bytes ocurre
   aqui—;
3. **despues** se mide, se digiere una sola vez y se contrasta contra los
   checksums declarados;
4. el sidecar se construye con esos mismos digests;
5. entre el digest y el `rename` no queda mas que escribir ese sidecar y
   confirmar la entrada del staging: ninguna operacion vuelve a tocar los bytes
   certificados.

El original nunca se retira antes de tiempo: el intento conserva su salida, de
modo que un fallo de validacion manda a cuarentena algo que todavia explica por
que fallo.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import uuid

from clipperkick.domain.ingest import CLAVE_CHECKSUMS, etiquetar_checksum
from clipperkick.domain.jobs import (
    ArchivoSalida, ErrorIdentificadorJob, ErrorValidacionSalida, ManifiestoSalida,
    exigir_componente_interno, exigir_ruta_interna,
)

from ..durability import (
    escribir_bytes_durable, sincronizar_archivo, sincronizar_arbol, sincronizar_directorio,
)


DIRECTORIO_TRABAJOS = "jobs"
DIRECTORIO_CUARENTENA = "_cuarentena"
SUFIJO_PUBLICACION = ".publicando"
#: Sidecar que identifica *que* se publico. Viaja dentro del directorio y por
#: tanto se instala con el mismo `os.replace` que los archivos: no existe un
#: estado en el que haya artefactos publicados sin su identidad.
NOMBRE_PUBLICACION = "_publicacion.json"
FORMATO_PUBLICACION = "clipsapp-publicacion/2"
#: Marcador de **identidad** de un sellado en curso. No confiere permiso para
#: retirar nada: solo permite que el inventario distinga un residuo propio de un
#: directorio ajeno con nombre parecido. Vive dentro del staging y se retira
#: justo antes del rename, de modo que jamas forma parte de lo publicado.
NOMBRE_SELLADO = "_sellado.json"
FORMATO_SELLADO = "clipsapp-sellado/1"
LONGITUD_SUFIJO_SELLADO = 32
#: Bloque de lectura del digest. Un artefacto de video no cabe en memoria.
BLOQUE_DIGEST = 1 << 20


def _lstat(ruta: Path) -> os.stat_result | None:
    """`lstat` sin seguir enlaces, o `None` si no se puede medir."""
    try:
        return os.lstat(ruta)
    except OSError:
        return None


def _es_reparse(estado: os.stat_result) -> bool:
    """¿Es un punto de reanalisis? En Windows, `is_symlink()` no basta.

    Una *junction* no es un symlink para Python: `Path.is_symlink()` devuelve
    `False` y `lstat` la presenta como un directorio corriente. Lo unico que la
    delata es el atributo de reparse, y sin mirarlo un ancestro redirigido
    convierte cualquier limpieza en un borrado fuera del proyecto. Medido en la
    corrida: `is_symlink=False`, `FILE_ATTRIBUTE_REPARSE_POINT=True`.
    """
    if stat.S_ISLNK(estado.st_mode):
        return True
    atributos = getattr(estado, "st_file_attributes", 0)
    return bool(atributos & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _es_directorio_real(estado: os.stat_result | None) -> bool:
    return estado is not None and stat.S_ISDIR(estado.st_mode) and not _es_reparse(estado)


def _es_archivo_real(estado: os.stat_result | None) -> bool:
    return estado is not None and stat.S_ISREG(estado.st_mode) and not _es_reparse(estado)


def _sufijo_de_sellado(nombre: str, job: str) -> str | None:
    """Sufijo aleatorio de un sellado de `job`, o `None` si no lo es.

    La gramatica se exige entera —`<job>.publicando-<32 hex>`— y no como
    prefijo. Un prefijo acepta `<job>.publicando-not-owned`, que es exactamente
    el directorio ajeno que la limpieza no debe tocar: el nombre se parece, pero
    esta capa nunca lo pudo escribir.
    """
    esperado = f"{job}{SUFIJO_PUBLICACION}-"
    if not nombre.startswith(esperado):
        return None
    return _sufijo_hexadecimal(nombre[len(esperado):])


def _sufijo_hexadecimal(sufijo: str) -> str | None:
    if len(sufijo) != LONGITUD_SUFIJO_SELLADO:
        return None
    return sufijo if all(caracter in "0123456789abcdef" for caracter in sufijo) else None


def escribir_marca(ruta: Path, job_id: str) -> None:
    """Escribe el marcador de identidad de un sellado.

    El marcador no autoriza a nadie a borrar nada. Su unica funcion es decir
    que este directorio lo escribio esta capa y para que job, de modo que el
    inventario pueda distinguir un residuo propio de un directorio ajeno con el
    nombre parecido.

    Se crea con `O_EXCL`: si ya hay algo con ese nombre —un enlace, un archivo
    de otro— no se sobrescribe, se falla.
    """
    descriptor = os.open(ruta, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(descriptor, "wb") as archivo:
        archivo.write(json.dumps({"formato": FORMATO_SELLADO, "job_id": job_id,
                                  "pid": os.getpid()},
                                 ensure_ascii=False, sort_keys=True).encode("utf-8"))
        archivo.flush()
        os.fsync(archivo.fileno())


def copiar_sellado(origen: Path, destino: Path) -> None:
    """Copia una salida a un archivo nuevo, del que solo responde el coordinador.

    Copiar y no mover es la diferencia entre certificar bytes que alguien puede
    seguir escribiendo y certificar bytes propios. Un `os.replace` cambia la
    ruta pero conserva el inode: el descriptor que el worker dejo abierto sigue
    llegando al mismo contenido, ahora ya dentro del area de publicacion. La
    copia estrena inode y corta ese vinculo.

    El precio es leer y escribir el artefacto una vez mas. Es el unico modo de
    que la promesa "lo publicado es lo que se digirio" no dependa de que nadie
    se haya guardado un handle.
    """
    shutil.copyfile(origen, destino)


def _validar_relativa(ruta: str) -> Path:
    """Ruta declarada en un manifiesto, juzgada con la gramatica de los dos SO.

    No se usa `Path` para decidir: en POSIX una ruta con sintaxis de Windows
    (`C:` mas contrabarra, o una UNC) es un nombre relativo perfectamente valido,
    y el proyecto sobreviviria hasta abrirse en Windows ya convertido en una
    ruta absoluta fuera de su carpeta. `exigir_ruta_interna` parte por
    ambos separadores y rechaza unidades, UNC, NUL, `.` y `..` con independencia
    de donde corra.
    """
    try:
        return Path(*exigir_ruta_interna(ruta, "ruta de salida"))
    except ErrorIdentificadorJob as error:
        raise ErrorValidacionSalida(
            f"La salida declara una ruta que no es interna: {ruta!r}") from error


def _checksums_declarados(manifiesto: ManifiestoSalida) -> dict[str, str]:
    """Digests que el productor afirma haber escrito, ya normalizados.

    El almacen calcula el digest de lo que encuentra para poder identificar la
    publicacion; eso certifica *lo que hay*, no *lo que la etapa dijo producir*.
    La diferencia importa cuando una etapa escribe un archivo truncado o el
    sistema de archivos devuelve bytes que nadie escribio: sin una afirmacion
    previa contra la que contrastar, ambos casos se publican sin ruido.

    Declarar checksums es opcional —un manifiesto sin la clave se valida como
    siempre—, pero declarar el de un archivo que no se publica es un error: casi
    siempre significa que la etapa cambio de salidas y el manifiesto no.
    """
    crudo = manifiesto.metadata.get(CLAVE_CHECKSUMS)
    if crudo is None:
        return {}
    if not isinstance(crudo, Mapping):
        raise ErrorValidacionSalida("La salida declara checksums que no son un objeto.")
    declarados: dict[str, str] = {}
    for ruta, digest in crudo.items():
        if not isinstance(ruta, str) or not isinstance(digest, str) or not digest:
            raise ErrorValidacionSalida("La salida declara un checksum sin ruta o sin valor.")
        declarados[_validar_relativa(ruta).as_posix()] = etiquetar_checksum(digest)
    return declarados


def _sin_enlaces(base: Path, relativa: Path) -> Path:
    """Recorre componente a componente rechazando cualquier enlace."""
    candidata = base
    for parte in relativa.parts:
        candidata = candidata / parte
        if candidata.is_symlink():
            raise ErrorValidacionSalida(f"La salida declara un enlace: {relativa.as_posix()!r}")
    return candidata


def _medir_y_digerir(base: Path, manifiesto: ManifiestoSalida) -> list[list[object]]:
    """Mide, digiere y contrasta cada archivo declarado en **una sola lectura**.

    Devuelve lo que identifica una publicacion: ruta, tamano y contenido. El
    tamano por si solo no identifica nada —dos salidas distintas de la misma
    longitud se tomarian por la misma publicacion y el intento nuevo se
    descartaria en silencio adoptando bytes ajenos—, de modo que el digest es lo
    que convierte esa coincidencia en una discrepancia detectable.

    Que la comprobacion del checksum declarado y la construccion de la huella
    compartan lectura no es una optimizacion: son la misma pregunta hecha una
    vez. Con dos lecturas, el sidecar podria describir unos bytes y la
    validacion haber aprobado otros.
    """
    pendientes = _checksums_declarados(manifiesto)
    filas: list[list[object]] = []
    for archivo in manifiesto.archivos:
        relativa = _validar_relativa(archivo.ruta)
        clave = relativa.as_posix()
        candidata = _sin_enlaces(base, relativa)
        if not candidata.is_file():
            raise ErrorValidacionSalida(f"La salida declara un archivo ausente: {clave!r}")
        with candidata.open("rb") as flujo:
            resumen = hashlib.sha256()
            tamano = 0
            for bloque in iter(lambda: flujo.read(BLOQUE_DIGEST), b""):
                resumen.update(bloque)
                tamano += len(bloque)
        if tamano != archivo.bytes:
            raise ErrorValidacionSalida(
                f"La salida declara {archivo.bytes} bytes para {clave!r} y mide {tamano}.")
        digest = resumen.hexdigest()
        esperado = pendientes.pop(clave, None)
        if esperado is not None and etiquetar_checksum(digest) != esperado:
            raise ErrorValidacionSalida(
                f"El contenido de {clave!r} no coincide con el checksum declarado.")
        filas.append([clave, archivo.bytes, digest])
    if pendientes:
        raise ErrorValidacionSalida(
            f"La salida declara checksum de archivos que no publica: {sorted(pendientes)!r}")
    return sorted(filas)


def _linaje(stage: str, version_contrato: str, clave: str) -> dict[str, str]:
    """Quien produjo la publicacion, no solo que contiene.

    Dos etapas distintas —o dos versiones de la misma— pueden producir bytes
    identicos por casualidad; el linaje impide que una adopte el artefacto de la
    otra y lo presente como propio.
    """
    return {"stage": stage, "version_contrato": version_contrato,
            "clave_materializacion": clave}


def _declaradas(salidas: Sequence[str]) -> set[str]:
    """Normaliza el contrato de salidas de la etapa a claves comparables."""
    return {Path(*exigir_ruta_interna(salida, "salida declarada")).as_posix()
            for salida in salidas}


class AlmacenArtefactosProyecto:
    """Adaptador sobre la carpeta del proyecto abierta por T02."""

    def __init__(self, raiz: str | Path) -> None:
        self.raiz = Path(raiz)
        #: Intentos que **esta** instancia creo. Es la unica prueba de propiedad
        #: que no exige volver a leer disco, y por tanto la unica que autoriza a
        #: `descartar` a borrar recursivamente.
        self._preparados: set[str] = set()

    # -- frontera unica de rutas derivadas de identificadores ----------- #

    def _interna(self, ancla: Path, *componentes: str) -> Path:
        """Compone una ruta a partir de identificadores y prueba que no escapa.

        Es la unica puerta por la que un `job_id` o un `attempt_id` se convierten
        en ruta. Primero se valida la *forma* —sin tocar disco, de modo que un
        identificador hostil no llega a tener efecto alguno que deshacer— y
        despues se comprueba la contencion real: un enlace intermedio podria
        sacar del proyecto una ruta cuyos componentes eran todos inocentes.
        """
        destino = ancla
        for componente in componentes:
            destino = destino / exigir_componente_interno(componente, "identificador de ruta")
        raiz = self.raiz.resolve()
        resuelta = destino.resolve()
        if resuelta != raiz and raiz not in resuelta.parents:
            raise ErrorIdentificadorJob(
                f"La ruta derivada de {componentes!r} queda fuera del proyecto.")
        return destino

    # ------------------------------------------------------------------ #

    @property
    def _staging(self) -> Path:
        return self.raiz / "cache" / DIRECTORIO_TRABAJOS

    def preparar(self, job_id: str, attempt_id: str) -> str:
        """Estrena el directorio de un intento. Nunca reutiliza ni borra otro.

        El nombre lleva 32 hexadecimales al azar por la misma razon que el
        staging de publicacion: un nombre determinista es un blanco. Mientras
        `cache/jobs/<job>/<intento>` fue predecible, preparar un intento tenia
        que retirar el residuo anterior —y ese `rmtree` se ejecutaba sobre una
        ruta re-resuelta despues de validarla, que es exactamente el patron que
        la decision de recuperacion declara insuficiente: basta redirigir un
        ancestro para que el borrado caiga fuera del proyecto.

        Con un nombre estrenado el problema se disuelve en vez de vigilarse. No
        hay colision posible, luego no hay nada que retirar, luego no hay
        borrado que redirigir. `exist_ok=False` lo deja escrito como invariante:
        si alguna vez colisionase, se falla en vez de barrer.

        El precio es que un corte deja un intento huerfano bajo `cache/`, que es
        area reconstruible y ya se declaro fuga aceptable en F06.
        """
        destino = self._interna(self._staging, job_id, f"{attempt_id}-{uuid.uuid4().hex}")
        destino.mkdir(parents=True, exist_ok=False)
        # Propiedad en memoria: es lo unico que despues autoriza a `descartar` a
        # borrarlo. Una ruta que solo llega por argumento no prueba nada.
        self._preparados.add(str(destino))
        return str(destino)

    # ------------------------------------------------------------------ #

    def _revisar_declaraciones(self, directorio: str, manifiesto: ManifiestoSalida,
                               salidas: Sequence[str]) -> Path:
        """Juzga el manifiesto *como documento*, sin leer un solo byte.

        Se separa de la medida porque decide cosas distintas: aqui se rechaza lo
        que ni siquiera se debe mover —una ruta que escapa, un nombre reservado,
        una salida fuera de contrato, un enlace—, y eso tiene que ocurrir antes
        de tocar nada. La lectura del contenido llega despues, ya sobre el
        staging privado.

        `salidas` es el contrato de la `StageDefinition`. Si la etapa declara que
        produce ciertos archivos, publicar otros distintos convertiria el
        contrato en documentacion: habria dos fuentes de verdad sobre lo que una
        etapa produce y solo una de ellas seria la real. Un contrato vacio
        significa "sin restriccion", no "cualquier cosa vale por descuido".
        """
        base = Path(directorio).resolve()
        if not base.is_dir():
            raise ErrorValidacionSalida("El intento no dejo su directorio de trabajo.")
        permitidas = _declaradas(salidas)
        vistas: set[str] = set()
        for archivo in manifiesto.archivos:
            relativa = _validar_relativa(archivo.ruta)
            clave = relativa.as_posix()
            if relativa.parts[0] == NOMBRE_PUBLICACION:
                raise ErrorValidacionSalida(
                    f"La salida no puede declarar el nombre reservado {NOMBRE_PUBLICACION!r}.")
            if clave in vistas:
                raise ErrorValidacionSalida(f"La salida declara dos veces {clave!r}.")
            if permitidas and clave not in permitidas:
                raise ErrorValidacionSalida(
                    f"La etapa no declara {clave!r} entre sus salidas {sorted(permitidas)!r}.")
            vistas.add(clave)
            # Cada componente se examina por separado: un enlace se rechaza
            # antes de resolverlo, porque seguirlo permitiria publicar —o medir—
            # algo que vive fuera del proyecto. Comprobar solo el ultimo tramo
            # dejaria pasar `enlace/salida.json`.
            if not _sin_enlaces(base, relativa).is_file():
                raise ErrorValidacionSalida(f"La salida declara un archivo ausente: {clave!r}")
        return base

    def validar(self, directorio: str, manifiesto: ManifiestoSalida,
                salidas: Sequence[str] = ()) -> tuple[ArchivoSalida, ...]:
        """Comprueba que lo declarado existe, es interno, mide lo dicho y esta en contrato.

        Sigue siendo la puerta publica —el coordinador la usa como comprobacion
        temprana sobre el directorio del intento— pero ya **no** es la que
        certifica lo que se publica: eso ocurre dentro de `publicar`, sobre el
        staging privado, para que entre medir y renombrar no quepa una escritura.
        """
        base = self._revisar_declaraciones(directorio, manifiesto, salidas)
        _medir_y_digerir(base, manifiesto)
        return manifiesto.archivos

    # ------------------------------------------------------------------ #

    def publicar(self, job_id: str, directorio: str, manifiesto: ManifiestoSalida,
                 attempt_id: str = "", clave: str = "", salidas: Sequence[str] = (),
                 stage: str = "", version_contrato: str = "") -> tuple[str, ...]:
        """Publica el conjunto entero, o adopta el que ya estaba si es el mismo.

        Publicar y confirmar en SQLite son dos operaciones y el proceso puede
        morir entre ambas: el rename ya ocurrio, la transaccion no. Al reabrir,
        el job se recupera y su siguiente intento vuelve aqui con un destino que
        *ya existe*. Rechazarlo dejaria el trabajo permanentemente fallido con su
        resultado intacto al lado, que es la peor combinacion posible.

        Por eso la publicacion lleva identidad durable —job, clave de
        materializacion y huella del manifiesto— dentro del propio directorio.
        Si lo que hay coincide, se **adopta** y se devuelve como publicado sin
        mover nada; si difiere, no se pisa: se conserva para diagnostico y el
        intento falla con un error tipado.
        """
        # Las declaraciones se revisan siempre y antes de mover: son el unico
        # juicio que puede emitirse sin leer contenido, y decide que archivos
        # tienen derecho a entrar en el staging de publicacion.
        origen = self._revisar_declaraciones(directorio, manifiesto, salidas)
        final = self._interna(self.raiz / "artifacts", job_id)
        publicados = tuple((Path("artifacts") / job_id / _validar_relativa(archivo.ruta)).as_posix()
                           for archivo in manifiesto.archivos)
        if final.exists():
            # No se mueve nada: se compara lo que hay publicado con lo que este
            # intento dice traer. Aqui medir sobre el directorio del intento es
            # correcto porque esos bytes no van a publicarse.
            registro = self._registro(job_id, attempt_id, clave, stage, version_contrato,
                                      manifiesto, _medir_y_digerir(origen, manifiesto))
            self._adoptar(final, registro, manifiesto)
            self.descartar(directorio)
            return publicados
        # Nombre imprevisible y **estrenado**: 32 hexadecimales al azar no
        # colisionan con ningun residuo, de modo que publicar no necesita
        # retirar nada de lo que ya hubiera en disco. Ese es todo el motivo por
        # el que esta capa puede dejar de barrer: no compite por el nombre.
        sufijo = uuid.uuid4().hex
        staging = final.with_name(f"{final.name}{SUFIJO_PUBLICACION}-{sufijo}")
        try:
            staging.mkdir(parents=True)
            # El marcador identifica el residuo si esta publicacion no termina.
            # Ya no habilita a nadie a borrarlo: solo permite que el inventario
            # lo distinga de un directorio ajeno con el nombre parecido.
            escribir_marca(staging / NOMBRE_SELLADO, job_id)
            copiadas: list[Path] = []
            for archivo in manifiesto.archivos:
                relativa = _validar_relativa(archivo.ruta)
                destino = staging / relativa
                destino.parent.mkdir(parents=True, exist_ok=True)
                # Copia, no movimiento: la publicacion no puede compartir inode
                # con lo que el productor todavia puede escribir.
                copiar_sellado(origen / relativa, destino)
                copiadas.append(relativa)
            for relativa in copiadas:
                # Barrera 1: el contenido esta en disco antes de que nada lo
                # publique. Un rename durable sobre datos que aun viven en cache
                # publicaria un directorio con archivos vacios tras un corte.
                sincronizar_archivo(staging / relativa)
            # Barrera 2: la estructura del staging. Va **antes** del digest a
            # proposito: es la ultima operacion que recorre y toca los archivos,
            # y hacerla despues dejaria un hueco entre lo medido y lo publicado
            # justo del tamano de ese recorrido.
            sincronizar_arbol(staging)
            # Los bytes ya son nuestros, estan volcados y nadie mas conoce esta
            # ruta: medirlos, digerirlos y contrastarlos con lo declarado es
            # medir exactamente lo que el rename va a publicar.
            registro = self._registro(job_id, attempt_id, clave, stage, version_contrato,
                                      manifiesto, _medir_y_digerir(staging, manifiesto))
            # La identidad entra en el directorio *antes* del rename, de modo que
            # se publica con el, no despues, y se construye con los digests que
            # acaba de producir la misma lectura que valido el contenido.
            escribir_bytes_durable(
                staging / NOMBRE_PUBLICACION,
                json.dumps(registro, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            # El marcador de propiedad sale antes del rename: lo que se publica
            # es lo que la etapa declaro, ni un archivo mas. A partir de aqui el
            # directorio ya no se anuncia como sellado, y una limpieza
            # concurrente que lo mirase lo dejaria en paz por falta de marcador,
            # que es la direccion segura durante los microsegundos que faltan.
            (staging / NOMBRE_SELLADO).unlink(missing_ok=True)
            # Barrera 3: la entrada del staging, que acaba de estrenar el
            # sidecar. Solo toca metadatos del directorio; los archivos
            # certificados ya no se vuelven a abrir.
            sincronizar_directorio(staging)
            os.replace(staging, final)
            # Barrera 4: la entrada de directorio que acaba de aparecer.
            sincronizar_directorio(final.parent)
        except BaseException:
            # Se retira **el directorio que esta invocacion acaba de crear**, y
            # cuya ruta no ha salido de esta funcion: la propiedad es inequivoca
            # y no hace falta demostrarla leyendo disco. El intento conserva su
            # salida —lo que la cuarentena necesita para explicar el fallo— y una
            # publicacion previa discordante nunca se toca.
            shutil.rmtree(staging, ignore_errors=True)
            raise
        self.descartar(directorio)
        return publicados

    def _registro(self, job_id: str, attempt_id: str, clave: str, stage: str,
                  version_contrato: str, manifiesto: ManifiestoSalida,
                  archivos: list[list[object]]) -> dict[str, object]:
        return {"formato": FORMATO_PUBLICACION, "job_id": job_id, "attempt_id": attempt_id,
                "clave_materializacion": clave, "archivos": archivos,
                "linaje": _linaje(stage, version_contrato, clave),
                "metadata": dict(manifiesto.metadata)}

    def _adoptar(self, final: Path, registro: dict[str, object],
                 manifiesto: ManifiestoSalida) -> None:
        """Acepta una publicacion previa solo si es exactamente esta."""
        sidecar = final / NOMBRE_PUBLICACION
        try:
            anterior = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ErrorValidacionSalida(
                f"El destino de {registro['job_id']!r} ya existe y no declara que contiene;"
                " se conserva sin tocar para diagnostico.") from error
        if not isinstance(anterior, dict) or anterior.get("formato") != FORMATO_PUBLICACION:
            raise ErrorValidacionSalida(
                f"El destino de {registro['job_id']!r} tiene una identidad ilegible.")
        # `metadata` entra en la comparacion: describe *que son* los bytes
        # publicados (tipo de artefacto, de que checkpoint se reanudo...). Adoptar
        # con metadata distinta registraria una descripcion nueva sobre bytes
        # viejos, que es una forma silenciosa de mentir sobre el artefacto.
        # `archivos` ya incluye el digest y `linaje` dice quien lo produjo, de
        # modo que la comparacion cubre identidad, contenido y procedencia.
        discrepantes = [campo for campo in ("job_id", "clave_materializacion", "archivos",
                                            "linaje", "metadata")
                        if anterior.get(campo) != registro[campo]]
        if discrepantes:
            raise ErrorValidacionSalida(
                f"El destino de {registro['job_id']!r} contiene otra publicacion"
                f" (difiere en {discrepantes!r}); no se sobrescribe.")
        # Coincide el sidecar: falta comprobar que los bytes *publicados* son
        # todavia los que ese sidecar describe. Un sidecar intacto sobre archivos
        # alterados es exactamente el caso que la adopcion no debe aceptar.
        try:
            publicada = _medir_y_digerir(final, manifiesto)
        except (OSError, ErrorIdentificadorJob, ErrorValidacionSalida) as error:
            raise ErrorValidacionSalida(
                f"La publicacion previa de {registro['job_id']!r} no se puede leer;"
                " se conserva sin tocar para diagnostico.") from error
        if publicada != registro["archivos"]:
            raise ErrorValidacionSalida(
                f"El destino de {registro['job_id']!r} contiene bytes distintos de los que"
                " declara su intento; no se sobrescribe ni se adopta.")

    # ------------------------------------------------------------------ #

    def cuarentena(self, directorio: str) -> str | None:
        """Preserva **en sitio** el temporal de un intento fallido.

        Antes se movia a `cache/jobs/_cuarentena/<job>/<intento>`, y ese destino
        deterministico obligaba a las dos operaciones que este corte retira: un
        `rmtree` para hacerle sitio y un `os.replace` hacia una ruta re-resuelta.
        Con un ancestro redirigido, ambas escribian y borraban fuera del
        proyecto —medido—, y la funcion devolvia ademas una ruta que afirmaba
        estar dentro.

        Ahora no hace falta mover nada: desde que cada intento estrena nombre,
        el directorio fallido **ya esta aislado**. Nadie lo va a reutilizar ni a
        confundir con trabajo bueno, que era todo lo que la cuarentena
        conseguia. Asi que se conserva donde esta y se devuelve su ruta, que es
        lo que el diagnostico necesitaba desde el principio.

        Deja de retirarlo de la propiedad en memoria: un intento en cuarentena
        no se descarta despues, se mira.
        """
        origen = Path(directorio)
        if not _es_directorio_real(_lstat(origen)):
            return None
        self._preparados.discard(str(origen))
        return str(origen)

    def descartar(self, directorio: str) -> None:
        """Retira un intento **solo** si esta instancia lo creo.

        Una ruta que llega por argumento no prueba nada: pudo salir de esta
        funcion, de un corte anterior o de quien quiera escribir en `cache/`.
        Borrarla recursivamente es la operacion mas cara de equivocar, asi que
        se exige la unica evidencia que no depende de leer disco —haberla
        creado en este proceso, en este objeto— y ante cualquier otra cosa se
        preserva.

        Preservar de mas solo deja un directorio reconstruible bajo `cache/`.
        """
        if str(directorio) not in self._preparados:
            return
        self._preparados.discard(str(directorio))
        shutil.rmtree(Path(directorio), ignore_errors=True)

    def residuos(self, job_id: str = "") -> tuple[dict[str, object], ...]:
        """Inventario **no destructivo** de sellados abandonados.

        T04 no borra automaticamente arboles de publicacion que ya estaban en
        disco, y la razon esta escrita en la decision de recuperacion: Python no
        ofrece una cadena portable de operaciones relativas a handles para
        recorrer y borrar un arbol sin volver a resolver su ruta. Revalidar y
        despues llamar a `shutil.rmtree` deja siempre una ventana en la que el
        nombre puede apuntar a otro objeto; anadir comprobaciones la estrecha,
        pero no demuestra que se recorre lo que se midio.

        Asi que en vez de borrar, se **cuenta**. Este metodo solo lee: describe
        lo que hay para que una accion de mantenimiento explicita —con
        confirmacion de la persona— pueda decidir. Un residuo listado aqui no es
        un artefacto valido ni se presenta como tal: `publicados()` sigue viendo
        unicamente `artifacts/<job>/`.

        `job_id` vacio inventaria todos los jobs.
        """
        raiz = self.raiz / "artifacts"
        try:
            hijos = sorted(raiz.iterdir())
        except OSError:
            return ()
        encontrados: list[dict[str, object]] = []
        for hijo in hijos:
            descripcion = self._describir_residuo(hijo, job_id)
            if descripcion is not None:
                encontrados.append(descripcion)
        return tuple(encontrados)

    def _describir_residuo(self, hijo: Path, job_id: str) -> dict[str, object] | None:
        """Describe un hijo si tiene forma de sellado. Nunca escribe ni borra."""
        nombre = hijo.name
        separador = nombre.rfind(SUFIJO_PUBLICACION + "-")
        if separador < 0:
            return None
        job = nombre[:separador]
        if _sufijo_de_sellado(nombre, job) is None:
            return None
        if job_id and job != job_id:
            return None
        estado = _lstat(hijo)
        marcador = _lstat(hijo / NOMBRE_SELLADO) if _es_directorio_real(estado) else None
        return {"ruta": hijo.relative_to(self.raiz).as_posix(), "job_id": job,
                "directorio_real": _es_directorio_real(estado),
                # Un marcador propio distingue "esto lo dejamos nosotros" de un
                # directorio ajeno que se le parece. No autoriza nada: solo
                # explica al mantenimiento posterior de que esta mirando.
                "marcador_propio": self._marcador_es_nuestro(hijo, marcador)}

    def _marcador_es_nuestro(self, hijo: Path, marcador: os.stat_result | None) -> bool:
        if not _es_archivo_real(marcador):
            return False
        try:
            datos = json.loads((hijo / NOMBRE_SELLADO).read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return False
        return isinstance(datos, dict) and datos.get("formato") == FORMATO_SELLADO

    def publicados(self, job_id: str) -> tuple[str, ...]:
        raiz = self._interna(self.raiz / "artifacts", job_id)
        if not raiz.is_dir():
            return ()
        return tuple(sorted(hijo.relative_to(self.raiz).as_posix()
                            for hijo in raiz.rglob("*") if hijo.is_file()))


def rutas_de(manifiesto: ManifiestoSalida) -> Sequence[str]:
    return tuple(archivo.ruta for archivo in manifiesto.archivos)
