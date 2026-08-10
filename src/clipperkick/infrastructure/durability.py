"""Barreras de persistencia compartidas por los adaptadores de filesystem.

Un `os.replace` es atomico, pero atomico no es durable: garantiza que nadie vea
un estado intermedio, no que lo escrito sobreviva a un corte de energia. Para
que sobreviva hace falta ordenar barreras explicitas, y ese orden es el mismo
que T02 fijo para el marcador de migracion:

1. contenido de cada archivo a disco (`fsync` del descriptor);
2. contenido del directorio que los agrupa, antes de publicarlo;
3. el `replace` que publica;
4. la entrada del directorio padre, despues.

En Windows los pasos 2 y 4 no existen: no se puede abrir un directorio para
sincronizarlo. NTFS ordena los cambios de metadatos del rename respecto de los
datos ya volcados, de modo que con el paso 1 hecho el resultado equivale. La
funcion se queda como no-op documentado en vez de desaparecer, para que el orden
del protocolo sea el mismo en los dos sistemas y una prueba pueda observarlo.
"""

from __future__ import annotations

import os
from pathlib import Path


def sincronizar_archivo(ruta: Path) -> None:
    """Vuelca a disco el contenido de un archivo ya cerrado.

    Se abre en `rb+` y no en solo lectura porque en Windows `os.fsync` acaba en
    `_commit`, que exige un descriptor con permiso de escritura: sobre un handle
    de solo lectura falla con `EBADF`. Es el mismo modo que T02 usa para
    sincronizar la copia de respaldo de la base.
    """
    with ruta.open("rb+") as archivo:
        os.fsync(archivo.fileno())


def sincronizar_directorio(ruta: Path) -> None:
    """Vuelca la entrada de directorio. No-op en Windows, por diseno."""
    if os.name == "nt":
        return
    try:
        descriptor = os.open(str(ruta), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def escribir_bytes_durable(destino: Path, datos: bytes) -> None:
    """Escribe y sincroniza en el sitio; el llamador publica el directorio."""
    with destino.open("wb") as archivo:
        archivo.write(datos)
        archivo.flush()
        os.fsync(archivo.fileno())


def sincronizar_arbol(raiz: Path) -> None:
    """Sincroniza todos los archivos de un arbol y despues su propia entrada."""
    for hijo in sorted(raiz.rglob("*")):
        if hijo.is_file() and not hijo.is_symlink():
            sincronizar_archivo(hijo)
    for directorio in sorted((hijo for hijo in raiz.rglob("*") if hijo.is_dir()), reverse=True):
        sincronizar_directorio(directorio)
    sincronizar_directorio(raiz)
