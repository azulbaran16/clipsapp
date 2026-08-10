"""Adaptadores del flujo legado para yt-dlp, FFmpeg y el sistema de archivos."""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import ContextManager

from clipperkick.application.ports import Log
from clipperkick.domain import ErrorAmigable, formato_legible, formato_nombre

from .ffmpeg import buscar_fuente, construir_filtro


ES_WINDOWS = os.name == "nt"
SIN_CONSOLA = getattr(subprocess, "CREATE_NO_WINDOW", 0) if ES_WINDOWS else 0
RE_EBUR = re.compile(r"t:\s*([\d.]+).*?M:\s*(-?[\d.]+|-?inf|nan)")


def comprobar_herramienta(nombre: str, instalacion: str) -> None:
    if shutil.which(nombre) is None:
        raise ErrorAmigable(
            f"No encuentro '{nombre}'.\n\nInstálalo con PowerShell:\n{instalacion}\n\n"
            "Luego cierra y vuelve a abrir esta aplicación."
        )


class EspacioTemporalSistema:
    def crear(self) -> ContextManager[str]:
        return tempfile.TemporaryDirectory()


class HerramientasSistema:
    def comprobar_renderizador(self) -> None:
        comprobar_herramienta("ffmpeg", "winget install Gyan.FFmpeg")


class FuenteYtDlp:
    def obtener(self, entrada: str, carpeta_temporal: str, hd: bool, log: Log) -> str:
        if os.path.isfile(entrada):
            log(f"Usando archivo local: {os.path.basename(entrada)}")
            return entrada
        if not entrada.lower().startswith(("http://", "https://")):
            raise ErrorAmigable("La entrada no es un enlace ni un archivo válido.")
        comprobar_herramienta("yt-dlp", 'python -m pip install -U "yt-dlp[default,curl-cffi]"')
        altura = 1080 if hd else 720
        destino = os.path.join(carpeta_temporal, "vod.mp4")
        log(f"Descargando VOD ({altura}p)… Puede tardar varios minutos.")
        proceso = subprocess.Popen(
            ["yt-dlp", "-f", f"bv*[height<={altura}]+ba/b[height<={altura}]/b",
             "--merge-output-format", "mp4", "-o", destino, "--no-playlist", "--newline", entrada],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
            creationflags=SIN_CONSOLA,
        )
        ultimo_porcentaje = -10.0
        if proceso.stdout is not None:
            for linea in proceso.stdout:
                coincidencia = re.search(r"\[download\]\s+([\d.]+)%", linea)
                if coincidencia:
                    porcentaje = float(coincidencia.group(1))
                    if porcentaje - ultimo_porcentaje >= 10:
                        log(f"   descarga: {porcentaje:.0f}%")
                        ultimo_porcentaje = porcentaje
        proceso.wait()
        if proceso.returncode != 0 or not os.path.isfile(destino):
            raise ErrorAmigable("No se pudo descargar el VOD.\n\nActualiza yt-dlp y verifica que el enlace abra en tu navegador.")
        return destino


class AnalizadorFfmpeg:
    def analizar(self, video: str, log: Log) -> list[float]:
        log("Analizando el audio en busca de momentos intensos…")
        proceso = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-nostats", "-i", video, "-map", "0:a:0", "-af", "ebur128", "-f", "null", "-"],
            stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True, errors="replace",
            creationflags=SIN_CONSOLA,
        )
        por_segundo: dict[int, float] = {}
        ultimas_lineas: deque[str] = deque(maxlen=5)
        if proceso.stderr is not None:
            for linea in proceso.stderr:
                ultimas_lineas.append(linea.strip())
                coincidencia = RE_EBUR.search(linea)
                if not coincidencia:
                    continue
                tiempo = float(coincidencia.group(1))
                try:
                    volumen = float(coincidencia.group(2))
                except ValueError:
                    volumen = -70.0
                if math.isnan(volumen) or math.isinf(volumen):
                    volumen = -70.0
                segundo = int(tiempo)
                por_segundo[segundo] = max(volumen, por_segundo.get(segundo, -70.0), -70.0)
        proceso.wait()
        if len(por_segundo) < 30:
            detalle = "\n".join(linea for linea in ultimas_lineas if linea)
            sufijo = f"\n\nDetalle técnico:\n{detalle}" if detalle else ""
            raise ErrorAmigable("El video no tiene una pista de audio utilizable o dura menos de 30 segundos." + sufijo)
        cantidad = max(por_segundo) + 1
        serie = [por_segundo.get(indice, -70.0) for indice in range(cantidad)]
        ventana = 7
        return [sum(serie[max(0, indice - ventana // 2):min(cantidad, indice + ventana // 2 + 1)]) /
                (min(cantidad, indice + ventana // 2 + 1) - max(0, indice - ventana // 2))
                for indice in range(cantidad)]


class ExportadorFfmpeg:
    def exportar(self, video: str, picos: Sequence[tuple[int, float]], duracion: int,
                 carpeta_salida: str, vertical: bool, nombre_canal: str, logo: str,
                 log: Log) -> list[str]:
        os.makedirs(carpeta_salida, exist_ok=True)
        if logo and not os.path.isfile(logo):
            raise ErrorAmigable(f"No encuentro el logo:\n{logo}")
        listos: list[str] = []
        resumen: list[str] = []
        filtro = construir_filtro(vertical, nombre_canal, bool(logo), buscar_fuente())
        for posicion, (segundo, puntaje) in enumerate(picos, 1):
            inicio = max(0, segundo - int(duracion * 0.4))
            nombre = f"clip_{posicion:02d}_min_{formato_nombre(inicio)}.mp4"
            salida = os.path.join(carpeta_salida, nombre)
            log(f"Exportando {nombre} (momento {formato_legible(segundo)})…")
            comando = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(inicio), "-i", video]
            if logo:
                comando += ["-i", logo]
            comando += ["-t", str(duracion), "-filter_complex", filtro, "-map", "[out]", "-map", "0:a?",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-c:a", "aac", "-b:a", "160k",
                        "-movflags", "+faststart", salida]
            resultado = subprocess.run(comando, capture_output=True, text=True, errors="replace", creationflags=SIN_CONSOLA)
            if resultado.returncode != 0:
                detalle = resultado.stderr.strip().splitlines()
                ultimo_error = detalle[-1] if detalle else "sin detalle de FFmpeg"
                raise ErrorAmigable(f"No se pudo exportar {nombre}.\n\nFFmpeg: {ultimo_error}")
            listos.append(salida)
            resumen.append(f"clip_{posicion:02d}: momento {formato_legible(segundo)} del VOD (+{puntaje:.1f} dB sobre lo normal)")
        Path(carpeta_salida, "resumen.txt").write_text(
            "Momentos detectados (mejor primero):\n\n" + "\n".join(resumen) + "\n", encoding="utf-8")
        return listos


def crear_adaptadores_compatibilidad() -> tuple[FuenteYtDlp, AnalizadorFfmpeg, ExportadorFfmpeg, EspacioTemporalSistema, HerramientasSistema]:
    return FuenteYtDlp(), AnalizadorFfmpeg(), ExportadorFfmpeg(), EspacioTemporalSistema(), HerramientasSistema()
