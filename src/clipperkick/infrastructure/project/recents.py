"""Lista de proyectos recientes en un JSON del perfil del usuario.

Es preferencia de la persona, no estado del proyecto: por eso vive fuera de
cualquier carpeta `.clipsapp` y sobrevive a borrarlos todos.

Dos decisiones la hacen inofensiva. Un archivo ilegible **no** es un error de
arranque —recientes vacio es una degradacion aceptable, no poder abrir la
aplicacion no lo es—, y la escritura pasa por temporal + `replace`, de modo que
un cierre a mitad deja la lista anterior en vez de una lista truncada.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from clipperkick.application.project import ProyectoReciente


LIMITE = 12
NOMBRE = "recents.json"


def ruta_por_defecto() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home()))
    return base / "ClipperKick" / NOMBRE


def _clave(ruta: str) -> str:
    """Compara rutas como lo hace Windows: sin distinguir mayusculas ni forma."""
    return os.path.normcase(os.path.abspath(ruta))


class RecientesJson:
    def __init__(self, ruta: Path | None = None, limite: int = LIMITE) -> None:
        self.ruta = Path(ruta) if ruta is not None else ruta_por_defecto()
        self.limite = limite

    def listar(self) -> tuple[ProyectoReciente, ...]:
        entradas = []
        for cruda in self._leer():
            try:
                entradas.append(ProyectoReciente(str(cruda["ruta"]), str(cruda["nombre"]),
                                                 float(cruda.get("visto_en", 0.0))))
            except (KeyError, TypeError, ValueError):
                continue  # Una entrada corrupta no invalida a sus vecinas.
        entradas.sort(key=lambda entrada: entrada.visto_en, reverse=True)
        return tuple(entradas[: self.limite])

    def registrar(self, reciente: ProyectoReciente) -> None:
        clave = _clave(reciente.ruta)
        conservadas = [entrada for entrada in self.listar() if _clave(entrada.ruta) != clave]
        self._escribir([reciente, *conservadas][: self.limite])

    def olvidar(self, ruta: str) -> None:
        clave = _clave(ruta)
        self._escribir([entrada for entrada in self.listar() if _clave(entrada.ruta) != clave])

    def _leer(self) -> list[dict]:
        try:
            datos = json.loads(self.ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [entrada for entrada in datos if isinstance(entrada, dict)] if isinstance(datos, list) else []

    def _escribir(self, entradas: list[ProyectoReciente]) -> None:
        temporal = self.ruta.with_name(self.ruta.name + ".tmp")
        carga = [{"ruta": entrada.ruta, "nombre": entrada.nombre, "visto_en": entrada.visto_en}
                 for entrada in entradas]
        try:
            self.ruta.parent.mkdir(parents=True, exist_ok=True)
            temporal.write_text(json.dumps(carga, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporal, self.ruta)
        except OSError:
            # No poder recordar un proyecto jamas debe impedir abrirlo.
            temporal.unlink(missing_ok=True)


class RecientesEnMemoria:
    """Recientes que no tocan disco: sesiones efimeras y pruebas."""

    def __init__(self, limite: int = LIMITE) -> None:
        self.limite = limite
        self._entradas: list[ProyectoReciente] = []

    def listar(self) -> tuple[ProyectoReciente, ...]:
        return tuple(self._entradas[: self.limite])

    def registrar(self, reciente: ProyectoReciente) -> None:
        clave = _clave(reciente.ruta)
        self._entradas = [reciente, *(entrada for entrada in self._entradas
                                      if _clave(entrada.ruta) != clave)][: self.limite]

    def olvidar(self, ruta: str) -> None:
        clave = _clave(ruta)
        self._entradas = [entrada for entrada in self._entradas if _clave(entrada.ruta) != clave]
