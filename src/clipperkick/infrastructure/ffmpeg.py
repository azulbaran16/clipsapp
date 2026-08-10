"""Detalles de compilacion FFmpeg del backend de compatibilidad."""

from __future__ import annotations

import os


FILTRO_VERTICAL = (
    "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
    "crop=1080:1920,gblur=sigma=25[bg];"
    "[0:v]scale=1080:-2[fg];"
    "[bg][fg]overlay=(W-w)/2:(H-h)/2[v]"
)


def buscar_fuente() -> str | None:
    candidatas = (
        "C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    )
    return next((ruta for ruta in candidatas if os.path.isfile(ruta)), None)


def escapar_texto(texto: str) -> str:
    return (texto.replace("\\", "").replace("'", "").replace("%", "")
            .replace(":", "\\:").replace(",", "\\,"))


def construir_filtro(vertical: bool, nombre: str, con_logo: bool,
                     fuente: str | None = None) -> str:
    """Compila el filtergraph legado de FFmpeg sin cambiar su salida."""
    if vertical:
        cadena = FILTRO_VERTICAL
        tamano_fuente, margen_x, margen_y, alto_logo = 48, 40, 60, 110
    else:
        cadena = "[0:v]null[v]"
        tamano_fuente, margen_x, margen_y, alto_logo = 36, 30, 40, 80
    actual = "[v]"
    if con_logo:
        cadena += f";[1:v]scale=-1:{alto_logo}[lg];{actual}[lg]overlay=W-w-{margen_x}:{margen_y}[v2]"
        actual = "[v2]"
    if nombre:
        parte_fuente = "fontfile='" + fuente.replace(":", "\\:") + "':" if fuente else ""
        cadena += (f";{actual}drawtext={parte_fuente}text='{escapar_texto(nombre)}':"
                   f"fontsize={tamano_fuente}:fontcolor=white:borderw=3:bordercolor=black:"
                   f"x={margen_x}:y={margen_y}[out]")
    else:
        cadena += f";{actual}null[out]"
    return cadena
