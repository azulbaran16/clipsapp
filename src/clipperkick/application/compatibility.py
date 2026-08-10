"""Caso de uso que preserva el flujo de la interfaz Tkinter actual."""

from __future__ import annotations

from collections.abc import Callable

from clipperkick.domain import ErrorAmigable, SolicitudClips, elegir_picos

from .ports import (AnalizadorAudio, EspacioTemporal, ExportadorClips,
                    FuenteVideo, VerificadorHerramientas)


class CasoDeUsoCompatibilidad:
    def __init__(self, fuente: FuenteVideo, analizador: AnalizadorAudio,
                 exportador: ExportadorClips, espacio_temporal: EspacioTemporal,
                 herramientas: VerificadorHerramientas,
                 log: Callable[[str], None] | None = None) -> None:
        self.fuente = fuente
        self.analizador = analizador
        self.exportador = exportador
        self.espacio_temporal = espacio_temporal
        self.herramientas = herramientas
        self.log = log or (lambda _mensaje: None)

    def procesar(self, solicitud: SolicitudClips) -> list[str]:
        if solicitud.numero_clips < 1 or not 15 <= solicitud.duracion <= 120:
            raise ErrorAmigable("Revisa la cantidad y la duración de los clips.")
        if not solicitud.carpeta_salida.strip():
            raise ErrorAmigable("Elige una carpeta de salida válida.")
        self.herramientas.comprobar_renderizador()
        with self.espacio_temporal.crear() as carpeta_temporal:
            video = self.fuente.obtener(solicitud.entrada, carpeta_temporal, solicitud.hd, self.log)
            niveles = self.analizador.analizar(video, self.log)
            picos = elegir_picos(niveles, solicitud.numero_clips, solicitud.duracion)
            if not picos:
                raise ErrorAmigable("No se detectaron momentos destacados. El audio puede ser demasiado silencioso o uniforme.")
            return self.exportador.exportar(video, picos, solicitud.duracion, solicitud.carpeta_salida,
                                            solicitud.vertical, solicitud.nombre_canal, solicitud.logo, self.log)
