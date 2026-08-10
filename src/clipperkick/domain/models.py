"""Modelos de entrada del flujo de clips."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SolicitudClips:
    entrada: str
    numero_clips: int
    duracion: int
    carpeta_salida: str
    vertical: bool
    hd: bool
    nombre_canal: str = ""
    logo: str = ""


@dataclass(frozen=True)
class InformacionMedia:
    """Datos tecnicos independientes de la herramienta que los obtiene."""

    duracion_segundos: float
    ancho: int
    alto: int
    tiene_audio: bool
