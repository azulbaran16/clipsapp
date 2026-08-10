"""Persistencia de candidatos sobre schema v6, sin ampliarlo en secreto.

La tabla `Moment` mantiene identidad, fuente, rango y score: es la referencia
estable que Draft ya conoce. El artefacto `candidates.json` conserva el ranking,
sus componentes, razones, posicion y configuracion. Las consultas combinan
ambas autoridades: el manifiesto decide pertenencia/orden del ranking y SQLite
confirma que el Moment materializado sigue existiendo.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
import sqlite3

from clipperkick.application.analysis.ports import (
    LIMITE_MAXIMO_PAGINA, NOMBRE_ARCHIVO_RANKING, OrdenMomentos, PaginaMomentos,
)
from clipperkick.domain.analysis import (
    ErrorArtefactoAnalisis, ErrorMomentoAnalisis, Momento, resultado_desde_documento,
)
from clipperkick.domain.ingest import ErrorIngesta, RegistroArtefacto, SourceAsset
from clipperkick.domain.project.errors import ErrorProyecto
from clipperkick.infrastructure.ingest.repository import RepositorioSqliteFuentes
from clipperkick.infrastructure.project.persistence import RepositorioSqliteProyecto

from .artifacts import AlmacenArtefactosAnalisisProyecto


class RepositorioSqliteAnalisis:
    def __init__(self, proyecto: RepositorioSqliteProyecto,
                 artefactos: AlmacenArtefactosAnalisisProyecto) -> None:
        self._proyecto = proyecto
        self._fuentes = RepositorioSqliteFuentes(proyecto)
        self._artefactos = artefactos

    @property
    def conexion(self) -> sqlite3.Connection:
        return self._proyecto.conexion

    def fuente(self, fuente_id: str) -> SourceAsset | None:
        for fuente in self._fuentes.listar():
            if fuente.id == fuente_id:
                return fuente
        return None

    def artefacto(self, clave: str) -> RegistroArtefacto | None:
        return self._fuentes.artefacto(clave)

    def registrar_artefacto(self, registro: RegistroArtefacto,
                            bytes_totales: int = 0) -> RegistroArtefacto:
        return self._fuentes.registrar_artefacto(registro, bytes_totales)

    def materializar(self, fuente_id: str, clave: str,
                     momentos: Sequence[Momento]) -> tuple[str, ...]:
        """Upsert sin borrar candidatos historicos ni romper Drafts.

        La membresia de *este* ranking vive en `candidates.json`; borrar aqui lo
        que el nuevo ranking no eligio destruiria Moments que pueden sostener un
        Draft. Si un rango reaparece conserva su id y actualiza el score.
        """
        if any(momento.fuente_id != fuente_id for momento in momentos):
            raise ErrorMomentoAnalisis(
                "No se pueden materializar candidatos de otra fuente.")
        try:
            with self._proyecto.transaccion() as tx:
                for momento in momentos:
                    tx.execute(
                        "INSERT INTO Moment (id, source_id, start_seconds, end_seconds, score)"
                        " VALUES (?, ?, ?, ?, ?)"
                        " ON CONFLICT(id) DO UPDATE SET source_id=excluded.source_id,"
                        " start_seconds=excluded.start_seconds, end_seconds=excluded.end_seconds,"
                        " score=excluded.score",
                        (momento.id, fuente_id, momento.inicio_segundos,
                         momento.fin_segundos, momento.score))
        except ErrorProyecto as error:
            raise ErrorMomentoAnalisis(
                f"No se pudieron materializar los candidatos de {clave!r}.") from error
        return tuple(momento.id for momento in momentos)

    def consultar(self, fuente_id: str | None = None, clave: str | None = None,
                  orden: OrdenMomentos = OrdenMomentos.SCORE, descendente: bool = True,
                  limite: int = 50, desplazamiento: int = 0,
                  estados: Sequence[str] = ()) -> PaginaMomentos:
        if isinstance(limite, bool) or not isinstance(limite, int) \
                or limite < 1 or limite > LIMITE_MAXIMO_PAGINA:
            raise ErrorMomentoAnalisis(
                f"El limite debe estar entre 1 y {LIMITE_MAXIMO_PAGINA}.")
        if isinstance(desplazamiento, bool) or not isinstance(desplazamiento, int) \
                or desplazamiento < 0:
            raise ErrorMomentoAnalisis("El desplazamiento no puede ser negativo.")
        clave_actual = clave or self._ultima_clave(fuente_id)
        if not clave_actual:
            return PaginaMomentos((), 0, desplazamiento, limite)
        momentos = list(self._momentos_del_artefacto(clave_actual))
        if fuente_id is not None:
            momentos = [momento for momento in momentos if momento.fuente_id == fuente_id]
        if estados:
            permitidos = frozenset(estados)
            momentos = [momento for momento in momentos if momento.estado in permitidos]
        reversa = bool(descendente)
        if orden is OrdenMomentos.SCORE:
            llave = lambda momento: (momento.score, -momento.inicio_segundos, momento.id)
        elif orden is OrdenMomentos.INICIO:
            llave = lambda momento: (momento.inicio_segundos, momento.id)
        elif orden is OrdenMomentos.POSICION:
            llave = lambda momento: (momento.posicion, momento.id)
        else:
            raise ErrorMomentoAnalisis(f"Orden de candidatos desconocido: {orden!r}.")
        momentos.sort(key=llave, reverse=reversa)
        total = len(momentos)
        pagina = tuple(momentos[desplazamiento:desplazamiento + limite])
        return PaginaMomentos(pagina, total, desplazamiento, limite)

    def momento(self, momento_id: str) -> Momento | None:
        filas = self._consultar(
            "SELECT materialization_key FROM AnalysisArtifact"
            " WHERE kind='candidates' AND state='available'"
            " ORDER BY rowid DESC")
        for (clave,) in filas:
            for momento in self._momentos_del_artefacto(str(clave or "")):
                if momento.id == momento_id:
                    return momento
        return None

    def _ultima_clave(self, fuente_id: str | None) -> str | None:
        sql = ("SELECT materialization_key FROM AnalysisArtifact"
               " WHERE kind='candidates' AND state='available'")
        parametros: tuple[object, ...] = ()
        if fuente_id is not None:
            sql += " AND source_id=?"
            parametros = (fuente_id,)
        sql += " ORDER BY rowid DESC LIMIT 1"
        filas = self._consultar(sql, parametros)
        return str(filas[0][0]) if filas and filas[0][0] else None

    def _momentos_del_artefacto(self, clave: str) -> tuple[Momento, ...]:
        registro = self.artefacto(clave)
        if registro is None or NOMBRE_ARCHIVO_RANKING not in registro.rutas:
            raise ErrorArtefactoAnalisis(
                f"No existe un manifiesto de candidatos para {clave!r}.")
        try:
            documento = self._artefactos.leer_verificado(
                clave, NOMBRE_ARCHIVO_RANKING,
                registro.checksums.get(NOMBRE_ARCHIVO_RANKING, ""))
            resultado = resultado_desde_documento(documento)
        except (ErrorIngesta, ErrorMomentoAnalisis, OSError) as error:
            raise ErrorArtefactoAnalisis(
                f"El manifiesto de candidatos {clave!r} no es legible.") from error
        materializados = {fila[0] for fila in self._consultar(
            "SELECT id FROM Moment WHERE source_id=?",
            (registro.fuente_id,))}
        return tuple(replace(momento, clave_analisis=clave)
                     for momento in resultado.momentos if momento.id in materializados)

    def _consultar(self, sql: str, parametros: Sequence[object] = ()) -> list[tuple]:
        try:
            return list(self.conexion.execute(sql, tuple(parametros)))
        except sqlite3.Error as error:
            raise ErrorMomentoAnalisis(
                "No se pudo consultar los candidatos del proyecto.") from error


__all__ = ["RepositorioSqliteAnalisis"]
