"""Orquestacion del analisis basico por artefactos materializados.

La aplicacion no conoce FFmpeg, SQLite ni rutas internas del proyecto. Solo
encadena tres contratos independientes y confirma cada salida antes de usarla
como entrada de la siguiente etapa. La separacion de claves es la garantia de
que un reranking no vuelve a decodificar ni a medir el audio.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace

from clipperkick.domain.analysis import (
    CODIGO_AUDIO_PREPARADO, CODIGO_AUDIO_REUTILIZADO, CODIGO_CANDIDATOS_PARCIALES,
    CODIGO_ENRIQUECIMIENTO_OMITIDO, CODIGO_MOMENTOS_MATERIALIZADOS,
    CODIGO_RANKING_COMPLETO, CODIGO_RANKING_REUTILIZADO, CODIGO_RASGOS_MEDIDOS,
    CODIGO_RASGOS_REUTILIZADOS, CODIGO_SIN_AUDIO, CODIGO_SIN_CANDIDATOS,
    DiagnosticoAnalisis, ErrorAnalisis, ErrorArtefactoAnalisis, ErrorAudioAusente,
    ErrorCancelacionAnalisis, EstadoAnalisis, Momento, ResultadoRanking, SenalExterna,
    SENALES_BASICAS, medicion_canonica, normalizar_medicion, rankear, resultado_desde_documento,
    serie_desde_documento,
)
from clipperkick.domain.ingest import (
    LinajeArtefacto, ManifiestoArtefacto, RegistroArtefacto, canonicalizar,
    clave_materializacion, digest_bloque, reutilizable,
)

from .ports import (
    ANTIGUEDAD_TEMPORALES, FORMATO_RANKING, FORMATO_RASGOS, NOMBRE_ARCHIVO_AUDIO,
    NOMBRE_ARCHIVO_RANKING, NOMBRE_ARCHIVO_RASGOS, NOMBRE_STAGE_AUDIO,
    NOMBRE_STAGE_RANKING, NOMBRE_STAGE_RASGOS, TIPO_ARTEFACTO_AUDIO,
    TIPO_ARTEFACTO_RANKING, TIPO_ARTEFACTO_RASGOS, VERSION_CONTRATO_AUDIO,
    VERSION_CONTRATO_RANKING, VERSION_CONTRATO_RASGOS, VERSION_RANKER_NUCLEO,
    AlmacenArtefactosAnalisis, Cancelacion, ExtractorAudio, LocalizadorFuente,
    MedidorSonoridad, Notificador, Progreso, ProveedorSenalOpcional,
    RepositorioAnalisis, ResultadoAnalisis, SolicitudAnalisis, nunca_cancelado,
    sin_notificar, sin_progreso,
)


class CasoDeUsoAnalisis:
    """Ejecuta audio -> rasgos -> ranking y materializa Moments."""

    def __init__(self, *, localizador: LocalizadorFuente, extractor: ExtractorAudio,
                 medidor: MedidorSonoridad, artefactos: AlmacenArtefactosAnalisis,
                 repositorio: RepositorioAnalisis,
                 senales_opcionales: Sequence[ProveedorSenalOpcional] = (),
                 notificar: Notificador = sin_notificar,
                 progreso: Progreso = sin_progreso,
                 antiguedad_temporales: float = ANTIGUEDAD_TEMPORALES) -> None:
        self._localizador = localizador
        self._extractor = extractor
        self._medidor = medidor
        self._artefactos = artefactos
        self._repositorio = repositorio
        self._senales = tuple(senales_opcionales)
        self._notificar = notificar
        self._progreso = progreso
        self._antiguedad = antiguedad_temporales

    def ejecutar(self, solicitud: SolicitudAnalisis,
                 cancelado: Cancelacion | None = None) -> ResultadoAnalisis:
        vigilar = cancelado if cancelado is not None else nunca_cancelado
        diagnosticos: list[DiagnosticoAnalisis] = []
        retirados = (self._artefactos.limpiar_abandonados(self._antiguedad)
                     if solicitud.limpiar_temporales else ())
        fuente = self._repositorio.fuente(solicitud.fuente_id)
        if fuente is None:
            raise ErrorAnalisis(f"No existe la fuente {solicitud.fuente_id!r}.")
        if not self._localizador.disponible(fuente):
            raise ErrorAnalisis(f"La fuente {solicitud.fuente_id!r} no esta disponible.")
        ruta_fuente = self._localizador.resolver(fuente)

        try:
            clave_audio, ruta_audio, audio_reutilizado, registro_audio = self._audio(
                fuente.id, fuente.huella.identidad, ruta_fuente, solicitud, diagnosticos, vigilar)
        except ErrorAudioAusente:
            self._emitir(diagnosticos, CODIGO_SIN_AUDIO, {"fuente": fuente.id})
            vacio = ResultadoRanking(estado=EstadoAnalisis.SIN_CANDIDATOS)
            return ResultadoAnalisis(
                fuente_id=fuente.id, clave_audio="", clave_rasgos="", clave_ranking="",
                ranking=vacio, diagnosticos=tuple(diagnosticos),
                temporales_retirados=tuple(retirados))

        clave_rasgos, serie, rasgos_reutilizados, registro_rasgos = self._rasgos(
            fuente.id, ruta_audio, registro_audio, solicitud, diagnosticos, vigilar)
        senales, omitidas = self._obtener_senales(
            ruta_fuente, ruta_audio, solicitud, diagnosticos)
        clave_ranking, ranking, ranking_reutilizado, registro_ranking = self._ranking(
            fuente.id, serie, registro_rasgos, solicitud, senales, omitidas,
            diagnosticos, vigilar)
        momentos = tuple(replace(momento, clave_analisis=clave_ranking)
                         for momento in ranking.momentos)
        ranking = replace(ranking, momentos=momentos)
        materializados = self._repositorio.materializar(fuente.id, clave_ranking, momentos)
        self._emitir(diagnosticos, CODIGO_MOMENTOS_MATERIALIZADOS,
                     {"fuente": fuente.id, "cantidad": len(materializados),
                      "clave": clave_ranking})
        self._progreso(3.0, 3.0)
        return ResultadoAnalisis(
            fuente_id=fuente.id, clave_audio=clave_audio, clave_rasgos=clave_rasgos,
            clave_ranking=clave_ranking, ranking=ranking, momentos=momentos,
            audio_reutilizado=audio_reutilizado, rasgos_reutilizados=rasgos_reutilizados,
            ranking_reutilizado=ranking_reutilizado,
            artefactos=tuple(
                f"artifacts/{registro.clave}/{ruta}"
                for registro in (registro_audio, registro_rasgos, registro_ranking)
                for ruta in registro.rutas),
            senales_omitidas=tuple(sorted(set(omitidas) | set(ranking.senales_ausentes))),
            diagnosticos=tuple(diagnosticos),
            temporales_retirados=tuple(retirados))

    # ------------------------------------------------------------------ #

    def _audio(self, fuente_id: str, huella: str, ruta_fuente: str,
               solicitud: SolicitudAnalisis, diagnosticos: list[DiagnosticoAnalisis],
               cancelado: Cancelacion) -> tuple[str, str, bool, RegistroArtefacto]:
        proveedor = self._extractor.version()
        entradas = {"fuente": huella}
        clave = clave_materializacion(
            stage=NOMBRE_STAGE_AUDIO, version_contrato=VERSION_CONTRATO_AUDIO,
            version_proveedor=proveedor, entradas=entradas,
            config=solicitud.extraccion.como_documento())
        linaje = self._linaje(NOMBRE_STAGE_AUDIO, VERSION_CONTRATO_AUDIO,
                              proveedor, clave, entradas)
        registro = self._repositorio.artefacto(clave)
        if registro is not None and reutilizable(
                registro, linaje, self._artefactos.presentes(clave)):
            self._emitir(diagnosticos, CODIGO_AUDIO_REUTILIZADO, {"clave": clave})
            self._progreso(1.0, 3.0)
            return (clave, self._artefactos.ruta_publicada(clave, NOMBRE_ARCHIVO_AUDIO),
                    True, registro)
        self._exigir_vigente(cancelado)
        directorio = self._artefactos.preparar(clave)
        try:
            destino = self._artefactos.ruta_en(directorio, NOMBRE_ARCHIVO_AUDIO)
            self._extractor.extraer(ruta_fuente, destino, solicitud.extraccion, cancelado)
            self._exigir_vigente(cancelado)
            archivo = self._artefactos.declarar(directorio, NOMBRE_ARCHIVO_AUDIO)
            manifiesto = ManifiestoArtefacto(
                tipo=TIPO_ARTEFACTO_AUDIO, archivos=(archivo,), linaje=linaje,
                metadata={"config": solicitud.extraccion.como_documento()})
            self._artefactos.publicar(clave, directorio, manifiesto)
        except BaseException:
            self._artefactos.cuarentena(directorio)
            raise
        registro = self._registrar(clave, TIPO_ARTEFACTO_AUDIO, fuente_id,
                                    linaje, (archivo,))
        self._emitir(diagnosticos, CODIGO_AUDIO_PREPARADO, {"clave": clave})
        self._progreso(1.0, 3.0)
        return clave, self._artefactos.ruta_publicada(clave, NOMBRE_ARCHIVO_AUDIO), False, registro

    def _rasgos(self, fuente_id: str, ruta_audio: str, audio: RegistroArtefacto,
                solicitud: SolicitudAnalisis, diagnosticos: list[DiagnosticoAnalisis],
                cancelado: Cancelacion):
        proveedor = self._medidor.version()
        entradas = {"audio": self._checksum(audio, NOMBRE_ARCHIVO_AUDIO)}
        clave = clave_materializacion(
            stage=NOMBRE_STAGE_RASGOS, version_contrato=VERSION_CONTRATO_RASGOS,
            version_proveedor=proveedor, entradas=entradas,
            config=solicitud.rasgos.como_documento())
        linaje = self._linaje(NOMBRE_STAGE_RASGOS, VERSION_CONTRATO_RASGOS,
                              proveedor, clave, entradas)
        registro = self._repositorio.artefacto(clave)
        if registro is not None and reutilizable(
                registro, linaje, self._artefactos.presentes(clave)):
            try:
                documento = self._leer(registro, NOMBRE_ARCHIVO_RASGOS)
                serie = serie_desde_documento(self._objeto(documento, "serie"))
            except ErrorAnalisis:
                pass
            else:
                self._emitir(diagnosticos, CODIGO_RASGOS_REUTILIZADOS, {"clave": clave})
                self._progreso(2.0, 3.0)
                return clave, serie, True, registro
        self._exigir_vigente(cancelado)
        crudo = medicion_canonica(self._medidor.medir(ruta_audio, solicitud.rasgos, cancelado))
        serie = normalizar_medicion(
            crudo, solicitud.rasgos.paso_segundos, solicitud.rasgos.piso_lufs,
            solicitud.rasgos.ventana_suavizado)
        documento = canonicalizar({
            "formato": FORMATO_RASGOS, "linaje": linaje.como_documento(),
            "medicion": crudo, "serie": serie.como_documento(),
            "config": solicitud.rasgos.como_documento(),
        }).encode("utf-8")
        registro = self._publicar_documento(
            clave, TIPO_ARTEFACTO_RASGOS, fuente_id, linaje,
            NOMBRE_ARCHIVO_RASGOS, documento, cancelado)
        self._emitir(diagnosticos, CODIGO_RASGOS_MEDIDOS,
                     {"clave": clave, "muestras": len(serie)})
        self._progreso(2.0, 3.0)
        return clave, serie, False, registro

    def _ranking(self, fuente_id: str, serie, rasgos: RegistroArtefacto,
                 solicitud: SolicitudAnalisis, senales: Sequence[SenalExterna],
                 omitidas: tuple[str, ...], diagnosticos: list[DiagnosticoAnalisis],
                 cancelado: Cancelacion):
        entradas = {"rasgos": self._checksum(rasgos, NOMBRE_ARCHIVO_RASGOS)}
        for senal in senales:
            entradas[f"senal:{senal.nombre}"] = digest_bloque(
                canonicalizar({"version": senal.version, "valores": senal.valores,
                               "paso_segundos": senal.paso_segundos}).encode("utf-8"))
        clave = clave_materializacion(
            stage=NOMBRE_STAGE_RANKING, version_contrato=VERSION_CONTRATO_RANKING,
            version_proveedor=VERSION_RANKER_NUCLEO, entradas=entradas,
            config=solicitud.ranking.como_documento())
        linaje = self._linaje(NOMBRE_STAGE_RANKING, VERSION_CONTRATO_RANKING,
                              VERSION_RANKER_NUCLEO, clave, entradas)
        registro = self._repositorio.artefacto(clave)
        if registro is not None and reutilizable(
                registro, linaje, self._artefactos.presentes(clave)):
            try:
                ranking = resultado_desde_documento(
                    self._leer(registro, NOMBRE_ARCHIVO_RANKING))
            except ErrorAnalisis:
                pass
            else:
                self._emitir(diagnosticos, CODIGO_RANKING_REUTILIZADO, {"clave": clave})
                return clave, ranking, True, registro
        self._exigir_vigente(cancelado)
        ranking = rankear(serie, solicitud.ranking, fuente_id, senales)
        momentos = tuple(replace(momento, clave_analisis=clave) for momento in ranking.momentos)
        ranking = replace(ranking, momentos=momentos)
        documento = canonicalizar({
            "formato": FORMATO_RANKING, "linaje": linaje.como_documento(),
            **ranking.como_documento(solicitud.ranking.como_documento()),
        }).encode("utf-8")
        registro = self._publicar_documento(
            clave, TIPO_ARTEFACTO_RANKING, fuente_id, linaje,
            NOMBRE_ARCHIVO_RANKING, documento, cancelado)
        codigo = CODIGO_RANKING_COMPLETO
        if ranking.estado is EstadoAnalisis.SIN_CANDIDATOS:
            codigo = CODIGO_SIN_CANDIDATOS
        elif ranking.estado is EstadoAnalisis.PARCIAL:
            codigo = CODIGO_CANDIDATOS_PARCIALES
        self._emitir(diagnosticos, codigo,
                     {"clave": clave, "cantidad": len(ranking.momentos)})
        return clave, ranking, False, registro

    # ------------------------------------------------------------------ #

    def _obtener_senales(self, ruta_fuente: str, ruta_audio: str,
                         solicitud: SolicitudAnalisis,
                         diagnosticos: list[DiagnosticoAnalisis],
                         ) -> tuple[tuple[SenalExterna, ...], tuple[str, ...]]:
        presentes: list[SenalExterna] = []
        omitidas: list[str] = []
        for proveedor in self._senales:
            nombre = type(proveedor).__name__
            try:
                nombre = proveedor.nombre()
                if solicitud.ranking.peso_de(nombre) <= 0:
                    continue
                presentes.append(proveedor.senal(ruta_fuente, ruta_audio))
            except Exception:
                omitidas.append(nombre)
                self._emitir(diagnosticos, CODIGO_ENRIQUECIMIENTO_OMITIDO,
                             {"senal": nombre})
        esperadas = {nombre for nombre, peso in solicitud.ranking.pesos.items()
                     if peso > 0 and nombre not in SENALES_BASICAS}
        ausentes = esperadas - {senal.nombre for senal in presentes} - set(omitidas)
        for nombre in sorted(ausentes):
            omitidas.append(nombre)
            self._emitir(diagnosticos, CODIGO_ENRIQUECIMIENTO_OMITIDO,
                         {"senal": nombre})
        return tuple(presentes), tuple(sorted(set(omitidas)))

    def _publicar_documento(self, clave: str, tipo: str, fuente_id: str,
                            linaje: LinajeArtefacto, nombre: str, datos: bytes,
                            cancelado: Cancelacion) -> RegistroArtefacto:
        directorio = self._artefactos.preparar(clave)
        try:
            archivo = self._artefactos.escribir(directorio, nombre, datos)
            self._exigir_vigente(cancelado)
            self._artefactos.publicar(
                clave, directorio,
                ManifiestoArtefacto(tipo=tipo, archivos=(archivo,), linaje=linaje))
        except BaseException:
            self._artefactos.cuarentena(directorio)
            raise
        return self._registrar(clave, tipo, fuente_id, linaje, (archivo,))

    def _registrar(self, clave: str, tipo: str, fuente_id: str,
                   linaje: LinajeArtefacto, archivos) -> RegistroArtefacto:
        registro = RegistroArtefacto(
            clave=clave, tipo=tipo, rutas=tuple(a.ruta for a in archivos),
            checksums={a.ruta: a.sha256 for a in archivos}, linaje=linaje,
            fuente_id=fuente_id)
        return self._repositorio.registrar_artefacto(
            registro, sum(archivo.bytes for archivo in archivos))

    def _leer(self, registro: RegistroArtefacto, nombre: str) -> Mapping[str, object]:
        try:
            return self._artefactos.leer_verificado(
                registro.clave, nombre, self._checksum(registro, nombre))
        except Exception as error:
            raise ErrorArtefactoAnalisis(
                f"El artefacto {registro.clave!r} no se puede reutilizar.") from error

    @staticmethod
    def _checksum(registro: RegistroArtefacto, nombre: str) -> str:
        checksum = registro.checksums.get(nombre, "")
        if not checksum:
            raise ErrorArtefactoAnalisis(
                f"El artefacto {registro.clave!r} no declara checksum de {nombre!r}.")
        return checksum

    @staticmethod
    def _objeto(documento: Mapping[str, object], nombre: str) -> Mapping[str, object]:
        valor = documento.get(nombre)
        if not isinstance(valor, Mapping):
            raise ErrorArtefactoAnalisis(f"El artefacto no declara {nombre!r}.")
        return valor

    @staticmethod
    def _linaje(stage: str, contrato: str, proveedor: str, clave: str,
                entradas: Mapping[str, str]) -> LinajeArtefacto:
        return LinajeArtefacto(stage=stage, version_contrato=contrato,
                               version_proveedor=proveedor, clave=clave,
                               entradas=dict(entradas))

    @staticmethod
    def _exigir_vigente(cancelado: Cancelacion) -> None:
        if cancelado():
            raise ErrorCancelacionAnalisis(
                "El analisis se cancelo antes de publicar la etapa en curso.")

    def _emitir(self, diagnosticos: list[DiagnosticoAnalisis], codigo: str,
                datos: Mapping[str, object]) -> None:
        diagnostico = DiagnosticoAnalisis(codigo, datos)
        diagnosticos.append(diagnostico)
        self._notificar(diagnostico)


__all__ = ["CasoDeUsoAnalisis"]
