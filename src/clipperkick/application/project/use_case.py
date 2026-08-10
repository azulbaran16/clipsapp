"""Crear, abrir y cerrar el proyecto de la sesion, y recordar cuales hubo.

La carcasa trabaja sobre un proyecto a la vez —es la mesa de trabajo descrita en
los Core Flows— y ese "a la vez" tiene consecuencias que no pueden vivir en la
vista:

- **Abrir cierra lo anterior antes de intentar nada.** El lock de escritor es del
  sistema operativo y no distingue procesos de handles: reabrir la misma carpeta
  sin soltar la anterior no falla, degrada a solo lectura, y la persona se
  encuentra un proyecto que no puede editar sin ninguna explicacion. Se paga el
  precio de que un fallo al abrir deje la sesion sin proyecto —estado explicito,
  recuperable y visible— a cambio de no producir nunca ese modo silencioso.
- **La reconciliacion ocurre al abrir, no cuando alguien la pide.** Es lo que
  convierte "hay un proyecto" en "se que le falta", y es la unica forma de que la
  primera pantalla ya pueda decir `requiere atencion`.
- **Recientes se registra despues de un exito.** Una carpeta que no abrio no es
  reciente: proponerla otra vez solo repetiria el fallo.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from .sessions import (CatalogoProyectos, ProyectoReciente, RepositorioRecientes,
                       SesionProyecto, SnapshotProyecto)


class CasoDeUsoProyectos:
    def __init__(self, catalogo: CatalogoProyectos, recientes: RepositorioRecientes,
                 reloj: Callable[[], float] = time.time) -> None:
        self.catalogo, self._recientes, self._reloj = catalogo, recientes, reloj
        self._sesion: SesionProyecto | None = None

    @property
    def abierto(self) -> bool:
        return self._sesion is not None

    def crear(self, ruta: str, nombre: str) -> SnapshotProyecto:
        nombre = nombre.strip()
        if not nombre:
            raise ValueError("Un proyecto necesita un nombre.")
        return self._adoptar(lambda: self.catalogo.crear(ruta, nombre))

    def abrir(self, ruta: str) -> SnapshotProyecto:
        return self._adoptar(lambda: self.catalogo.abrir(ruta))

    def cerrar(self) -> None:
        """Idempotente: cerrar dos veces no es un error, es el estado deseado."""
        sesion, self._sesion = self._sesion, None
        if sesion is not None:
            sesion.cerrar()

    def recientes(self) -> tuple[ProyectoReciente, ...]:
        return self._recientes.listar()

    def olvidar(self, ruta: str) -> None:
        self._recientes.olvidar(ruta)

    def _adoptar(self, obtener: Callable[[], SesionProyecto]) -> SnapshotProyecto:
        self.cerrar()
        sesion = obtener()
        try:
            snapshot = self._describir(sesion)
        except BaseException:
            # Una sesion que no se puede describir tampoco se puede usar, y
            # dejarla viva retendria el lock de una carpeta que nadie observa.
            sesion.cerrar()
            raise
        self._sesion = sesion
        self._recientes.registrar(ProyectoReciente(snapshot.ruta, snapshot.nombre, self._reloj()))
        return snapshot

    def _describir(self, sesion: SesionProyecto) -> SnapshotProyecto:
        informacion = sesion.informacion()
        reconciliacion = sesion.reconciliar()
        return SnapshotProyecto(
            sesion.ruta, informacion.project_id, informacion.nombre,
            informacion.schema_version, sesion.solo_lectura,
            reconciliacion.fuentes_faltantes, reconciliacion.artefactos_reconstruibles)
