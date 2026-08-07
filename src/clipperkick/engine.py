"""Motor de descarga, detección de momentos y exportación de clips."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


ES_WINDOWS = os.name == "nt"
SIN_CONSOLA = getattr(subprocess, "CREATE_NO_WINDOW", 0) if ES_WINDOWS else 0

RE_EBUR = re.compile(r"t:\s*([\d.]+).*?M:\s*(-?[\d.]+|-?inf|nan)")

FILTRO_VERTICAL = (
    "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
    "crop=1080:1920,gblur=sigma=25[bg];"
    "[0:v]scale=1080:-2[fg];"
    "[bg][fg]overlay=(W-w)/2:(H-h)/2[v]"
)

Log = Callable[[str], None]


class ErrorAmigable(Exception):
    """Error que puede mostrarse directamente a la persona usuaria."""


def buscar_fuente() -> str | None:
    """Devuelve una fuente negrita disponible para el rótulo del canal."""
    candidatas = (
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    )
    return next((ruta for ruta in candidatas if os.path.isfile(ruta)), None)


def escapar_texto(texto: str) -> str:
    """Escapa los caracteres con significado especial para drawtext."""
    return (
        texto.replace("\\", "")
        .replace("'", "")
        .replace("%", "")
        .replace(":", "\\:")
        .replace(",", "\\,")
    )


def construir_filtro(
    vertical: bool,
    nombre: str,
    con_logo: bool,
    fuente: str | None = None,
) -> str:
    """Construye el filtro de video para formato, logo y nombre del canal."""
    if vertical:
        cadena = FILTRO_VERTICAL
        tamano_fuente, margen_x, margen_y, alto_logo = 48, 40, 60, 110
    else:
        cadena = "[0:v]null[v]"
        tamano_fuente, margen_x, margen_y, alto_logo = 36, 30, 40, 80

    actual = "[v]"
    if con_logo:
        cadena += (
            f";[1:v]scale=-1:{alto_logo}[lg];"
            f"{actual}[lg]overlay=W-w-{margen_x}:{margen_y}[v2]"
        )
        actual = "[v2]"

    if nombre:
        fuente = fuente if fuente is not None else buscar_fuente()
        parte_fuente = (
            "fontfile='" + fuente.replace(":", "\\:") + "':" if fuente else ""
        )
        cadena += (
            f";{actual}drawtext={parte_fuente}"
            f"text='{escapar_texto(nombre)}':"
            f"fontsize={tamano_fuente}:fontcolor=white:"
            f"borderw=3:bordercolor=black:x={margen_x}:y={margen_y}[out]"
        )
    else:
        cadena += f";{actual}null[out]"
    return cadena


def formato_legible(segundos: float) -> str:
    total = int(segundos)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def formato_nombre(segundos: float) -> str:
    return formato_legible(segundos).replace(":", "-")


def elegir_picos(
    niveles: Sequence[float], numero_clips: int, duracion: int
) -> list[tuple[int, float]]:
    """Elige picos separados y devuelve los mejores primero."""
    if not niveles or numero_clips <= 0:
        return []

    cantidad = len(niveles)
    base = sorted(niveles)[cantidad // 2]
    puntajes = [nivel - base for nivel in niveles]
    margen = min(30, cantidad // 10)
    for indice in range(margen):
        puntajes[indice] = -999
        puntajes[cantidad - 1 - indice] = -999

    separacion = duracion + 30
    picos: list[tuple[int, float]] = []
    for _ in range(numero_clips):
        mejor = max(range(cantidad), key=puntajes.__getitem__)
        if puntajes[mejor] < 1.5:
            break
        picos.append((mejor, puntajes[mejor]))
        for indice in range(
            max(0, mejor - separacion), min(cantidad, mejor + separacion)
        ):
            puntajes[indice] = -999

    picos.sort(key=lambda pico: -pico[1])
    return picos


class Motor:
    """Orquesta las herramientas externas sin depender de la interfaz gráfica."""

    def __init__(self, log: Log | None = None):
        self.log = log or (lambda _mensaje: None)

    @staticmethod
    def _comprobar_herramienta(nombre: str, instalacion: str) -> None:
        if shutil.which(nombre) is None:
            raise ErrorAmigable(
                f"No encuentro '{nombre}'.\n\n"
                f"Instálalo con PowerShell:\n{instalacion}\n\n"
                "Luego cierra y vuelve a abrir esta aplicación."
            )

    def obtener_video(self, entrada: str, carpeta_temporal: str, hd: bool) -> str:
        if os.path.isfile(entrada):
            self.log(f"Usando archivo local: {os.path.basename(entrada)}")
            return entrada
        if not entrada.lower().startswith(("http://", "https://")):
            raise ErrorAmigable("La entrada no es un enlace ni un archivo válido.")

        self._comprobar_herramienta(
            "yt-dlp", 'python -m pip install -U "yt-dlp[default,curl-cffi]"'
        )
        altura = 1080 if hd else 720
        destino = os.path.join(carpeta_temporal, "vod.mp4")
        self.log(f"Descargando VOD ({altura}p)… Puede tardar varios minutos.")
        proceso = subprocess.Popen(
            [
                "yt-dlp",
                "-f",
                f"bv*[height<={altura}]+ba/b[height<={altura}]/b",
                "--merge-output-format",
                "mp4",
                "-o",
                destino,
                "--no-playlist",
                "--newline",
                entrada,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            errors="replace",
            creationflags=SIN_CONSOLA,
        )
        ultimo_porcentaje = -10.0
        if proceso.stdout is not None:
            for linea in proceso.stdout:
                coincidencia = re.search(r"\[download\]\s+([\d.]+)%", linea)
                if coincidencia:
                    porcentaje = float(coincidencia.group(1))
                    if porcentaje - ultimo_porcentaje >= 10:
                        self.log(f"   descarga: {porcentaje:.0f}%")
                        ultimo_porcentaje = porcentaje
        proceso.wait()
        if proceso.returncode != 0 or not os.path.isfile(destino):
            raise ErrorAmigable(
                "No se pudo descargar el VOD.\n\n"
                "Actualiza yt-dlp y verifica que el enlace abra en tu navegador."
            )
        return destino

    def analizar_audio(self, video: str) -> list[float]:
        self.log("Analizando el audio en busca de momentos intensos…")
        proceso = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-i",
                video,
                "-map",
                "0:a:0",
                "-af",
                "ebur128",
                "-f",
                "null",
                "-",
            ],
            stderr=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            text=True,
            errors="replace",
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
                por_segundo[segundo] = max(
                    volumen, por_segundo.get(segundo, -70.0), -70.0
                )
        proceso.wait()

        if len(por_segundo) < 30:
            detalle = "\n".join(linea for linea in ultimas_lineas if linea)
            sufijo = f"\n\nDetalle técnico:\n{detalle}" if detalle else ""
            raise ErrorAmigable(
                "El video no tiene una pista de audio utilizable o dura menos de "
                f"30 segundos.{sufijo}"
            )

        cantidad = max(por_segundo) + 1
        serie = [por_segundo.get(indice, -70.0) for indice in range(cantidad)]
        ventana = 7
        suavizada: list[float] = []
        for indice in range(cantidad):
            inicio = max(0, indice - ventana // 2)
            fin = min(cantidad, indice + ventana // 2 + 1)
            suavizada.append(sum(serie[inicio:fin]) / (fin - inicio))
        return suavizada

    def exportar(
        self,
        video: str,
        picos: Sequence[tuple[int, float]],
        duracion: int,
        carpeta_salida: str,
        vertical: bool,
        nombre_canal: str = "",
        logo: str = "",
    ) -> list[str]:
        os.makedirs(carpeta_salida, exist_ok=True)
        if logo and not os.path.isfile(logo):
            raise ErrorAmigable(f"No encuentro el logo:\n{logo}")

        listos: list[str] = []
        resumen: list[str] = []
        filtro = construir_filtro(vertical, nombre_canal, bool(logo))
        for posicion, (segundo, puntaje) in enumerate(picos, 1):
            inicio = max(0, segundo - int(duracion * 0.4))
            nombre = f"clip_{posicion:02d}_min_{formato_nombre(inicio)}.mp4"
            salida = os.path.join(carpeta_salida, nombre)
            self.log(
                f"Exportando {nombre} (momento {formato_legible(segundo)})…"
            )
            comando = [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-ss",
                str(inicio),
                "-i",
                video,
            ]
            if logo:
                comando += ["-i", logo]
            comando += [
                "-t",
                str(duracion),
                "-filter_complex",
                filtro,
                "-map",
                "[out]",
                "-map",
                "0:a?",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-movflags",
                "+faststart",
                salida,
            ]
            resultado = subprocess.run(
                comando,
                capture_output=True,
                text=True,
                errors="replace",
                creationflags=SIN_CONSOLA,
            )
            if resultado.returncode != 0:
                detalle = resultado.stderr.strip().splitlines()
                ultimo_error = detalle[-1] if detalle else "sin detalle de FFmpeg"
                raise ErrorAmigable(
                    f"No se pudo exportar {nombre}.\n\nFFmpeg: {ultimo_error}"
                )
            listos.append(salida)
            resumen.append(
                f"clip_{posicion:02d}: momento {formato_legible(segundo)} del VOD "
                f"(+{puntaje:.1f} dB sobre lo normal)"
            )

        ruta_resumen = Path(carpeta_salida, "resumen.txt")
        ruta_resumen.write_text(
            "Momentos detectados (mejor primero):\n\n"
            + "\n".join(resumen)
            + "\n",
            encoding="utf-8",
        )
        return listos

    def procesar(
        self,
        entrada: str,
        numero_clips: int,
        duracion: int,
        carpeta_salida: str,
        vertical: bool,
        hd: bool,
        nombre_canal: str = "",
        logo: str = "",
    ) -> list[str]:
        if numero_clips < 1 or not 15 <= duracion <= 120:
            raise ErrorAmigable("Revisa la cantidad y la duración de los clips.")
        if not carpeta_salida.strip():
            raise ErrorAmigable("Elige una carpeta de salida válida.")

        self._comprobar_herramienta("ffmpeg", "winget install Gyan.FFmpeg")
        with tempfile.TemporaryDirectory() as carpeta_temporal:
            video = self.obtener_video(entrada, carpeta_temporal, hd)
            niveles = self.analizar_audio(video)
            picos = elegir_picos(niveles, numero_clips, duracion)
            if not picos:
                raise ErrorAmigable(
                    "No se detectaron momentos destacados. "
                    "El audio puede ser demasiado silencioso o uniforme."
                )
            return self.exportar(
                video,
                picos,
                duracion,
                carpeta_salida,
                vertical,
                nombre_canal,
                logo,
            )
