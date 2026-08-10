"""Navegacion de la carcasa: recientes, nuevo/abrir proyecto y workspace.

El view model no sabe que hay ventanas. Sabe que existe un estado publicable y
que ciertas ordenes lo cambian, a veces despues de un trabajo que no puede
ocurrir en el hilo de la interfaz. Todo lo demas —que widget muestra que cosa—
es de la vista.

Dos reglas gobiernan el comportamiento y ambas existen por un fallo concreto:

- **Mientras algo esta en curso no se acepta otra orden.** Sin el cerrojo, dos
  aperturas simultaneas competirian por el mismo lock de escritor y la segunda
  degradaria a solo lectura o cerraria la sesion que la primera acaba de abrir.
- **Un fallo no deja al usuario en una pantalla vacia.** Si abrir falla, la
  carcasa vuelve a recientes con el motivo escrito, porque la accion siguiente
  —elegir otro proyecto, localizar el que se movio— esta ahi.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from clipperkick.application.project import CasoDeUsoProyectos, ProyectoReciente, SnapshotProyecto
from clipperkick.domain import ErrorAmigable

from .observable import Emisor
from .ports import Ejecutor


class Pantalla(str, Enum):
    RECIENTES = "recientes"
    WORKSPACE = "workspace"


@dataclass(frozen=True)
class EstadoCarcasa:
    pantalla: Pantalla = Pantalla.RECIENTES
    recientes: tuple[ProyectoReciente, ...] = ()
    proyecto: SnapshotProyecto | None = None
    ocupado: bool = False
    actividad: str = ""
    mensaje: str = ""

    @property
    def acepta_ordenes(self) -> bool:
        return not self.ocupado


class ViewModelCarcasa:
    def __init__(self, casos_de_uso: CasoDeUsoProyectos, ejecutor: Ejecutor) -> None:
        self._casos_de_uso, self._ejecutor = casos_de_uso, ejecutor
        self.estado: Emisor[EstadoCarcasa] = Emisor(EstadoCarcasa())

    # -- consultas ---------------------------------------------------------- #

    def refrescar_recientes(self) -> None:
        self._publicar(recientes=self._casos_de_uso.recientes())

    def olvidar(self, ruta: str) -> None:
        if not self.estado.valor.acepta_ordenes:
            return
        self._casos_de_uso.olvidar(ruta)
        self.refrescar_recientes()

    # -- ordenes ------------------------------------------------------------ #

    def crear(self, ruta: str, nombre: str) -> None:
        self._trabajar(f"Creando “{nombre.strip()}”…",
                       lambda: self._casos_de_uso.crear(ruta, nombre))

    def abrir(self, ruta: str) -> None:
        self._trabajar("Abriendo el proyecto…", lambda: self._casos_de_uso.abrir(ruta))

    def cerrar_proyecto(self) -> None:
        """Suelta el proyecto en el mismo ejecutor que lo abrio.

        La conexion SQLite pertenece al hilo de apertura; cerrarla directamente
        desde el hilo de la vista violaria esa afinidad y conservaria el lock.
        """
        if not self.estado.valor.acepta_ordenes:
            return
        self._publicar(ocupado=True, actividad="Cerrando el proyecto…", mensaje="")
        self._ejecutor.enviar(self._casos_de_uso.cerrar, self._cerrado, self._fallido)

    def preparar_cierre_aplicacion(self) -> None:
        """Encola el cierre aun si una apertura sigue en curso.

        El ejecutor dedicado conserva el orden: primero termina la operacion ya
        enviada y despues cierra la sesion que esta haya adoptado.
        """
        self._ejecutor.enviar(self._casos_de_uso.cerrar, lambda _resultado: None,
                              lambda _error: None)

    def _cerrado(self, _resultado=None) -> None:
        self._publicar(pantalla=Pantalla.RECIENTES, proyecto=None, mensaje="",
                       ocupado=False, actividad="", recientes=self._casos_de_uso.recientes())

    # -- interno ------------------------------------------------------------ #

    def _trabajar(self, actividad: str, tarea) -> None:
        if not self.estado.valor.acepta_ordenes:
            return
        self._publicar(ocupado=True, actividad=actividad, mensaje="")
        self._ejecutor.enviar(tarea, self._resuelto, self._fallido)

    def _resuelto(self, snapshot: SnapshotProyecto) -> None:
        self._publicar(pantalla=Pantalla.WORKSPACE, proyecto=snapshot, ocupado=False,
                       actividad="", mensaje=self._aviso(snapshot),
                       recientes=self._casos_de_uso.recientes())

    def _fallido(self, error: BaseException) -> None:
        self._publicar(pantalla=Pantalla.RECIENTES, proyecto=None, ocupado=False,
                       actividad="", mensaje=self._explicar(error),
                       recientes=self._casos_de_uso.recientes())

    @staticmethod
    def _aviso(snapshot: SnapshotProyecto) -> str:
        avisos = []
        if snapshot.requiere_atencion:
            avisos.append(f"{snapshot.fuentes_faltantes} fuente(s) sin localizar.")
        if snapshot.artefactos_reconstruibles:
            avisos.append(f"{snapshot.artefactos_reconstruibles} artefacto(s) se regeneraran.")
        if snapshot.solo_lectura:
            avisos.append("Otra instancia lo tiene abierto: solo lectura.")
        return " ".join(avisos)

    @staticmethod
    def _explicar(error: BaseException) -> str:
        """Un error tipado ya esta redactado para la persona; el resto, no.

        Mostrar el `repr` de una excepcion inesperada no ayuda a nadie, pero
        ocultarla del todo deja un fallo sin rastro: se antepone el impacto y se
        conserva el detalle detras de el.
        """
        if isinstance(error, (ErrorAmigable, ValueError)):
            return str(error)
        return f"No se pudo completar la operacion: {error}"

    def _publicar(self, **cambios) -> None:
        self.estado.emitir(replace(self.estado.valor, **cambios))
