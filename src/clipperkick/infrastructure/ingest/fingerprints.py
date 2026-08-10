"""Servicio de huellas sobre el sistema de archivos.

Implementa `ServicioHuellas` leyendo bytes reales. Todo lo que decide *que* se
lee vive en el dominio (`plan_parcial`, `componer_*`); aqui solo se lee y se
acumula. Esa separacion es la que hace que la huella sea reproducible entre
Windows y WSL: dos adaptadores distintos no pueden muestrear ventanas
distintas.

# Una pasada, pero no a ciegas

La huella completa se calcula en **una sola pasada**: recorrer el archivo para
la completa y volver a abrirlo para las ventanas duplica la lectura de un
archivo de gigabytes y, peor, abre una ventana en la que el archivo puede
cambiar entre ambas.

Una sola pasada no basta por si sola. Comparar solo *cuantos bytes se leyeron*
contra el tamano inicial acepta el caso mas facil de provocar y el mas dificil
de ver: una mutacion en sitio, del mismo tamano, sobre un tramo **ya leido**. El
resultado no es la huella del contenido viejo ni la del nuevo, sino la de una
mezcla que nunca existio en disco. Con el `mtime` restaurado —algo que cualquier
proceso puede hacer— tampoco queda rastro.

Por eso cada pasada va sellada. Antes y despues se toma la identidad y las
marcas de mutacion **del descriptor abierto** y **de la ruta**:

| Sistema | Identidad | Marca de mutacion |
| --- | --- | --- |
| POSIX | `st_dev` + `st_ino` | `st_ctime_ns`, **si se demuestra que distingue escrituras seguidas** |
| Windows | `st_dev` + `st_ino` (file id) | numero de secuencia USN de NTFS, y `ChangeTime` de `FILE_BASIC_INFO` como respaldo |

Lo de "si se demuestra" no es prudencia retorica, y la medida dice cosas que la
intuicion no: sobre ext4 y tmpfs, dos escrituras consecutivas comparten
`st_ctime_ns` —los tiempos de inodo de Linux vienen de un reloj de grano grueso,
de milisegundos enteros—, mientras que DrvFS, el sistema con el que WSL ve un
disco de Windows, si las distingue. Que el campo exista, y hasta que se mueva
cuando media una espera, no demuestra que sirva para lo que aqui se necesita.

Por eso la resolucion de `ctime` se **mide** una vez por volumen, escribiendo un
archivo propio en un directorio que aporta el cableado, con escrituras seguidas y
sin esperas. Es la misma pregunta que la huella se hace despues: ¿delataria esto
una reescritura inmediata?

En Windows el respaldo no basta por si solo, y conviene decir por que: los
tiempos del sistema avanzan a saltos de unos 15 ms, de modo que una reescritura
lo bastante rapida deja `LastWriteTime` **y** `ChangeTime` exactamente donde
estaban. El USN no depende del reloj —es un contador del volumen— y por eso es
la senal primaria alli donde existe.

Ninguna marca de metadatos es suficiente por si sola: en cualquier sistema puede
haber una mutacion dentro del mismo tic. Asi que el sello se acompana de una
comprobacion que no depende del reloj: **las ventanas muestreadas se releen y se
contrastan** contra lo que produjo la pasada, con el mismo descriptor y antes de
cerrarlo. Cuesta como mucho tres mebibytes y, para un archivo que cabe entero en
su ventana, es una verificacion completa del contenido.

Si algo del sello cambia, o si una ventana ya no da lo mismo, la pasada se
descarta: se reintenta un numero acotado de veces y, si no se estabiliza, se
devuelve `ErrorHuellaInestable`. Nunca se devuelve una huella mezclada solo
porque el numero de bytes cuadre.

# Cuando la evidencia no puede ser concluyente

Las dos comprobaciones se reparten el archivo: la relectura de ventanas cubre lo
muestreado y la marca monotonica cubre el resto. Si **ninguna** de las dos
alcanza a una parte del archivo —volumen sin senal monotonica fiable y archivo
mas grande que sus ventanas— no existe forma de saber si lo leido fue un solo
contenido, y ninguna cantidad de intentos lo cambia: lo que falta es la senal con
la que se decidiria, no la ocasion de mirarla.

En ese caso `completa()` se niega con `ErrorEvidenciaHuella` **antes de leer
nada**, y no reintenta. Es la respuesta conservadora que el ticket exige: una
identidad hibrida se propaga a la clave de materializacion, a los artefactos
derivados y a la deteccion de reemplazos, y sale mucho mas cara que un error. Un
archivo que cabe entero en su ventana sigue siendo valido en cualquier volumen,
porque alli la relectura *es* la verificacion completa.

La regla no se aplica a `parcial()`, y la asimetria es deliberada: la parcial no
afirma identidad —`HuellaFuente.identidad` se niega sin la completa— y solo sirve
para descartar rapido durante la reconciliacion, donde equivocarse cuesta un
sondeo de mas. Negarla dejaria sin reconciliacion barata a un volumen entero para
proteger una afirmacion que nunca hace.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import time
import uuid

from clipperkick.domain.ingest import (
    BLOQUE_LECTURA, TAMANO_VENTANA, ErrorEvidenciaHuella, ErrorHuellaIngesta, ErrorHuellaInestable,
    HuellaFuente, Ventana, componer_completa, componer_parcial, nuevo_acumulador, plan_parcial,
)


#: Intentos por huella. Cada intento es **una** pasada de SHA completa; se
#: reintenta porque una mutacion concurrente suele ser puntual (un `os.replace`
#: de otro proceso) y volver a leer la resuelve, pero se acota porque un archivo
#: en escritura continua no se estabilizara nunca y hay que decirlo.
INTENTOS_POR_DEFECTO = 3


# --------------------------------------------------------------------------- #
# Marca de mutacion dependiente del sistema
# --------------------------------------------------------------------------- #

if os.name == "nt":  # pragma: no cover - la rama contraria corre en POSIX
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class _FILE_BASIC_INFO(ctypes.Structure):
        _fields_ = [
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        ]

    _CLASE_INFO_BASICA = 0  # FileBasicInfo
    #: `FSCTL_READ_FILE_USN_DATA`. Devuelve el ultimo numero de secuencia que el
    #: journal del volumen asigno a este archivo. Es un contador, no un reloj.
    _FSCTL_LEER_USN = 0x000900EB
    #: Desplazamiento del campo `Usn` dentro de `USN_RECORD_V2`:
    #: RecordLength(4) + Major(2) + Minor(2) + FileRef(8) + ParentRef(8).
    _DESPLAZAMIENTO_USN = 24

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.DeviceIoControl.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _kernel32.DeviceIoControl.restype = wintypes.BOOL

    def _marca_de_secuencia(descriptor: int) -> int | None:
        """Numero de secuencia USN del archivo, si el volumen lleva journal.

        Es la unica senal de Windows que no depende del reloj: una reescritura
        dentro del mismo tic de 15 ms deja los tiempos intactos, pero nunca deja
        el USN intacto. Un volumen sin journal —una memoria FAT32, por ejemplo—
        devuelve `None` y la deteccion se apoya entonces en `ChangeTime` y en la
        relectura de ventanas.
        """
        try:
            handle = msvcrt.get_osfhandle(descriptor)
        except (OSError, ValueError):
            return None
        salida = ctypes.create_string_buffer(4096)
        devueltos = wintypes.DWORD()
        obtenido = _kernel32.DeviceIoControl(
            wintypes.HANDLE(handle), _FSCTL_LEER_USN, None, 0,
            salida, ctypes.sizeof(salida), ctypes.byref(devueltos), None)
        if not obtenido or devueltos.value < _DESPLAZAMIENTO_USN + 8:
            return None
        return int.from_bytes(
            salida.raw[_DESPLAZAMIENTO_USN:_DESPLAZAMIENTO_USN + 8], "little", signed=True)

    def _marca_de_cambio(descriptor: int) -> int | None:
        """`ChangeTime` del archivo abierto, en unidades de 100 ns.

        En Windows `st_ctime` es la *creacion*, no el cambio de metadatos: una
        escritura en sitio que restaure `LastWriteTime` no mueve ninguno de los
        tiempos que Python expone. `ChangeTime` si se mueve, y es la unica
        primitiva del sistema que distingue "nadie lo toco" de "lo reescribieron
        y borraron las huellas".

        Cualquier fallo devuelve `None`: la comprobacion se degrada a las demas
        senales del sello en vez de convertir un sistema de archivos exotico en
        un error de ingesta.
        """
        try:
            handle = msvcrt.get_osfhandle(descriptor)
        except (OSError, ValueError):
            return None
        info = _FILE_BASIC_INFO()
        obtenido = ctypes.windll.kernel32.GetFileInformationByHandleEx(
            wintypes.HANDLE(handle), ctypes.c_int(_CLASE_INFO_BASICA),
            ctypes.byref(info), wintypes.DWORD(ctypes.sizeof(info)))
        return int(info.ChangeTime) if obtenido else None
else:
    def _marca_de_cambio(_descriptor: int) -> int | None:
        """En POSIX la marca es `st_ctime_ns`, que ya viaja en el sello."""
        return None

    def _marca_de_secuencia(_descriptor: int) -> int | None:
        """POSIX no expone un contador equivalente; `st_ctime_ns` hace su papel."""
        return None


@dataclass(frozen=True)
class _Sello:
    """Identidad y marcas de mutacion de un archivo en un instante concreto."""

    dev: int
    ino: int
    tamano: int
    mtime_ns: int
    ctime_ns: int
    cambio: int | None = None
    secuencia: int | None = None

    @classmethod
    def de_estado(cls, estado: os.stat_result, cambio: int | None = None,
                  secuencia: int | None = None) -> "_Sello":
        return cls(dev=int(estado.st_dev), ino=int(estado.st_ino), tamano=int(estado.st_size),
                   mtime_ns=int(estado.st_mtime_ns), ctime_ns=int(estado.st_ctime_ns),
                   cambio=cambio, secuencia=secuencia)

    @property
    def identidad(self) -> tuple[int, int]:
        return (self.dev, self.ino)


def _sello_descriptor(archivo) -> _Sello:
    descriptor = archivo.fileno()
    return _Sello.de_estado(os.fstat(descriptor), _marca_de_cambio(descriptor),
                            _marca_de_secuencia(descriptor))


def _sello_ruta(ruta: str) -> _Sello:
    return _Sello.de_estado(os.stat(ruta))


#: Escrituras consecutivas de la sonda. Se hacen varias —sin esperar entre
#: ellas— porque lo que se mide no es si `ctime` acaba moviendose, sino si lo
#: hace *entre dos escrituras seguidas*: esa es exactamente la resolucion que
#: hace falta para delatar una mutacion dentro del mismo tic del reloj.
SONDEOS_RESOLUCION = 4
NOMBRE_SONDA = "senal-{}.probe"


def _medir_resolucion_ctime(directorio: Path) -> bool:
    """¿Distingue este volumen dos escrituras consecutivas por su `st_ctime_ns`?

    La pregunta no es si el campo se mueve alguna vez —con una espera de por
    medio se mueve en casi todas partes—, sino si se mueve **sin esperar**. Los
    tiempos de inodo de Linux vienen de un reloj de grano grueso, de milisegundos
    enteros, de modo que dos escrituras seguidas suelen compartir marca: sobre un
    volumen asi, `ctime` no puede delatar la reescritura que la huella teme, y
    darlo por bueno seria el fallback que el ticket rechaza.

    La sonda vive y muere aqui: nombre propio, borrado siempre, y ningun residuo
    que la limpieza tenga que aprender a reconocer.
    """
    sonda = directorio / NOMBRE_SONDA.format(uuid.uuid4().hex)
    try:
        with open(sonda, "wb") as archivo:
            archivo.write(b"0")
            archivo.flush()
            os.fsync(archivo.fileno())
            anterior = os.fstat(archivo.fileno()).st_ctime_ns
            for _ in range(SONDEOS_RESOLUCION):
                archivo.write(b"0")
                archivo.flush()
                os.fsync(archivo.fileno())
                actual = os.fstat(archivo.fileno()).st_ctime_ns
                if actual == anterior:
                    # Dos escrituras seguidas comparten marca: la resolucion no
                    # alcanza y una tercera tampoco lo cambiaria.
                    return False
                anterior = actual
        return True
    except OSError:
        return False
    finally:
        try:
            sonda.unlink(missing_ok=True)
        except OSError:
            pass


class ServicioHuellaArchivo:
    """`SourceFingerprintService` sobre archivos locales."""

    def __init__(self, ventana: int = TAMANO_VENTANA, bloque: int = BLOQUE_LECTURA,
                 intentos: int = INTENTOS_POR_DEFECTO,
                 directorio_prueba: str | Path | None = None) -> None:
        if isinstance(bloque, bool) or not isinstance(bloque, int) or bloque <= 0:
            raise ErrorHuellaIngesta("El bloque de lectura debe ser un entero positivo.")
        if isinstance(intentos, bool) or not isinstance(intentos, int) or intentos < 1:
            raise ErrorHuellaIngesta("El numero de intentos debe ser un entero positivo.")
        self._ventana = ventana
        self._bloque = bloque
        self._intentos = intentos
        #: Sitio propio donde medir la reactividad de `ctime`. Lo aporta el
        #: cableado, que si sabe que carpeta es del proyecto.
        self._directorio_prueba = Path(directorio_prueba) if directorio_prueba else None
        self._reactividad: dict[int, bool] = {}

    # ------------------------------------------------------------------ #
    # Sellado
    # ------------------------------------------------------------------ #

    def _abrir(self, ruta: str):
        try:
            if not os.path.isfile(ruta):
                raise ErrorHuellaIngesta(f"La fuente {ruta!r} no es un archivo.")
            return open(ruta, "rb")
        except OSError as error:
            raise ErrorHuellaIngesta(f"No se pudo abrir la fuente {ruta!r}.") from error

    def _exigir_estable(self, ruta: str, antes: _Sello, despues: _Sello,
                        ruta_antes: _Sello, ruta_despues: _Sello) -> None:
        """El archivo leido tiene que ser el mismo antes y despues, y no haber cambiado.

        Se comprueban dos cosas distintas y las dos hacen falta:

        - el **descriptor** no puede haber cambiado de tamano, de fecha ni de
          marca de cambio: eso descarta la mutacion en sitio;
        - la **ruta** tiene que seguir designando ese mismo archivo: eso descarta
          que alguien haya sustituido el path por otro archivo mientras leiamos
          el original, caso en el que la huella seria correcta pero de un
          contenido que ya no es el de esa fuente.
        """
        if antes != despues:
            raise ErrorHuellaInestable(
                f"La fuente {ruta!r} cambio mientras se calculaba su huella;"
                " no se publica una huella mezclada.")
        if ruta_antes.identidad != antes.identidad or ruta_despues.identidad != antes.identidad:
            raise ErrorHuellaInestable(
                f"La ruta {ruta!r} dejo de designar el archivo que se estaba leyendo.")

    def _reverificar_ventanas(self, archivo, ventanas, digests: list[str], ruta: str) -> None:
        """Relee las ventanas muestreadas y exige que sigan dando lo mismo.

        Es la mitad de la comprobacion que **no** depende del reloj, y por tanto
        la unica que detecta una mutacion ocurrida dentro del mismo tic del
        sistema. Se hace con el descriptor todavia abierto —no reabriendo la
        ruta— para que lo que se contrasta sea el mismo archivo que se leyo, y
        no lo que haya ahora donde estaba.

        El coste esta acotado por el plan: como mucho tres ventanas. Para un
        archivo que cabe entero en una, esto es una verificacion completa de su
        contenido.
        """
        for ventana, esperado in zip(ventanas, digests):
            if self._digest_ventana(archivo, ventana) != esperado:
                raise ErrorHuellaInestable(
                    f"El contenido de {ruta!r} cambio mientras se calculaba su huella;"
                    " no se publica una huella mezclada.")

    def _senal_fiable(self, sello: _Sello) -> bool:
        """¿Hay una marca que delate una escritura aunque el reloj no se mueva?

        En Windows es el USN: un contador del volumen, ajeno al reloj. Si el
        volumen no lleva journal no hay ninguna, porque `LastWriteTime` y
        `ChangeTime` comparten la granularidad de ~15 ms del sistema.

        En POSIX seria `st_ctime_ns` —lo mueve el nucleo en cada escritura y
        ningun proceso puede restaurarlo—, pero *que el campo exista no
        demuestra que reaccione*: sobre DrvFS, el sistema con el que WSL ve un
        disco de Windows, `st_ctime` es la fecha de **creacion** y no se mueve
        jamas al escribir. Dar por buena su mera presencia es exactamente el
        fallback que el ticket rechaza, asi que se comprueba midiendolo sobre el
        volumen, una vez por dispositivo y con un archivo propio.

        Es un metodo y no una funcion suelta para que una prueba pueda forzar el
        caso "volumen sin senal" sin depender de tener uno a mano.
        """
        if os.name == "nt":
            return sello.secuencia is not None
        return sello.ctime_ns > 0 and self._ctime_reacciona(sello.dev)

    def _ctime_reacciona(self, dev: int) -> bool:
        """Mide si `st_ctime_ns` distingue escrituras seguidas. Memorizado por dispositivo.

        La medida necesita un sitio donde escribir que **sea del mismo
        volumen**, y esa es una decision del llamador: aqui no se conoce ningun
        directorio propio, y usar el de la fuente significaria crear archivos en
        la carpeta de la persona. Sin ese sitio la respuesta es "no se puede
        demostrar", que es la conservadora.
        """
        if dev in self._reactividad:
            return self._reactividad[dev]
        veredicto = False
        directorio = self._directorio_prueba
        if directorio is not None:
            try:
                directorio.mkdir(parents=True, exist_ok=True)
                if os.stat(directorio).st_dev == dev:
                    veredicto = _medir_resolucion_ctime(directorio)
            except OSError:
                veredicto = False
        self._reactividad[dev] = veredicto
        return veredicto

    def _exigir_evidencia(self, ruta: str, sello: _Sello,
                          ventanas: tuple[Ventana, ...]) -> None:
        """Se niega antes de leer cuando el resultado no podria demostrarse.

        Cubierto = la relectura de ventanas verifica el archivo entero. Si no lo
        esta y ademas falta la marca monotonica, el tramo no muestreado queda sin
        ninguna comprobacion: aceptar seria afirmar una identidad que nadie puede
        respaldar. Se levanta `ErrorEvidenciaHuella` —que no se reintenta— y no
        se gasta ni una pasada de hashing en producir un dato que no valdria.
        """
        cubierto = sum(ventana.longitud for ventana in ventanas) >= sello.tamano
        if cubierto or self._senal_fiable(sello):
            return
        raise ErrorEvidenciaHuella(
            f"La fuente {ruta!r} mide {sello.tamano} bytes, sus ventanas no la cubren entera y"
            " este volumen no ofrece una marca de cambio fiable; no se puede demostrar que la"
            " huella describa un solo contenido.")

    def _con_reintentos(self, ruta: str, pasada) -> HuellaFuente:
        """Repite la pasada mientras el archivo no se estabilice, con limite.

        `ErrorEvidenciaHuella` se propaga sin reintentar: repetir con las mismas
        senales insuficientes no vuelve concluyente el resultado, solo tarda mas
        en decir lo mismo.
        """
        ultimo: ErrorHuellaInestable | None = None
        for _ in range(self._intentos):
            try:
                return pasada(ruta)
            except ErrorEvidenciaHuella:
                raise
            except ErrorHuellaInestable as error:
                ultimo = error
        raise ErrorHuellaInestable(
            f"La fuente {ruta!r} no se estabilizo en {self._intentos} intentos;"
            f" no se publica ninguna huella. ({ultimo})")

    # ------------------------------------------------------------------ #

    def parcial(self, ruta: str) -> HuellaFuente:
        """Muestrea solo las ventanas del plan. Es la lectura barata."""
        return self._con_reintentos(ruta, self._pasada_parcial)

    def _pasada_parcial(self, ruta: str) -> HuellaFuente:
        try:
            ruta_antes = _sello_ruta(ruta)
        except OSError as error:
            raise ErrorHuellaIngesta(f"No se pudo medir la fuente {ruta!r}.") from error
        archivo = self._abrir(ruta)
        try:
            antes = _sello_descriptor(archivo)
            ventanas = plan_parcial(antes.tamano, self._ventana)
            digests = [self._digest_ventana(archivo, ventana) for ventana in ventanas]
            self._reverificar_ventanas(archivo, ventanas, digests, ruta)
            despues = _sello_descriptor(archivo)
        except OSError as error:
            raise ErrorHuellaIngesta(f"No se pudo leer la fuente {ruta!r}.") from error
        finally:
            archivo.close()
        try:
            ruta_despues = _sello_ruta(ruta)
        except OSError as error:
            raise ErrorHuellaIngesta(f"No se pudo medir la fuente {ruta!r}.") from error
        self._exigir_estable(ruta, antes, despues, ruta_antes, ruta_despues)
        return HuellaFuente(tamano=antes.tamano,
                            parcial=componer_parcial(antes.tamano, ventanas, digests),
                            completa=None, mtime_ns=antes.mtime_ns)

    def _digest_ventana(self, archivo, ventana: Ventana) -> str:
        archivo.seek(ventana.desplazamiento)
        acumulador = nuevo_acumulador()
        restante = ventana.longitud
        while restante > 0:
            bloque = archivo.read(min(self._bloque, restante))
            if not bloque:
                raise ErrorHuellaInestable(
                    "La fuente se acorto mientras se calculaba su huella parcial.")
            acumulador.update(bloque)
            restante -= len(bloque)
        return acumulador.hexdigest()

    # ------------------------------------------------------------------ #

    def completa(self, ruta: str) -> HuellaFuente:
        """Recorre el archivo entero alimentando tambien las ventanas parciales."""
        return self._con_reintentos(ruta, self._pasada_completa)

    def _pasada_completa(self, ruta: str) -> HuellaFuente:
        try:
            ruta_antes = _sello_ruta(ruta)
        except OSError as error:
            raise ErrorHuellaIngesta(f"No se pudo medir la fuente {ruta!r}.") from error
        archivo = self._abrir(ruta)
        try:
            antes = _sello_descriptor(archivo)
            ventanas = plan_parcial(antes.tamano, self._ventana)
            # Antes de leer un solo byte: si el resultado no se va a poder
            # demostrar, producirlo solo sirve para tener algo que no se puede
            # usar.
            self._exigir_evidencia(ruta, antes, ventanas)
            total = nuevo_acumulador()
            parciales = [nuevo_acumulador() for _ in ventanas]
            leido = 0
            while True:
                bloque = archivo.read(self._bloque)
                if not bloque:
                    break
                total.update(bloque)
                inicio, fin = leido, leido + len(bloque)
                for acumulador, ventana in zip(parciales, ventanas):
                    desde = max(inicio, ventana.desplazamiento)
                    hasta = min(fin, ventana.fin)
                    if desde < hasta:
                        acumulador.update(bloque[desde - inicio:hasta - inicio])
                leido = fin
            if leido != antes.tamano:
                raise ErrorHuellaInestable(
                    f"La fuente {ruta!r} cambio de tamano mientras se calculaba su huella.")
            digests = [acumulador.hexdigest() for acumulador in parciales]
            self._reverificar_ventanas(archivo, ventanas, digests, ruta)
            despues = _sello_descriptor(archivo)
        except OSError as error:
            raise ErrorHuellaIngesta(f"No se pudo leer la fuente {ruta!r}.") from error
        finally:
            archivo.close()
        try:
            ruta_despues = _sello_ruta(ruta)
        except OSError as error:
            raise ErrorHuellaIngesta(f"No se pudo medir la fuente {ruta!r}.") from error
        self._exigir_estable(ruta, antes, despues, ruta_antes, ruta_despues)
        return HuellaFuente(
            tamano=antes.tamano,
            parcial=componer_parcial(antes.tamano, ventanas, digests),
            completa=componer_completa(antes.tamano, total.hexdigest()),
            mtime_ns=antes.mtime_ns)
