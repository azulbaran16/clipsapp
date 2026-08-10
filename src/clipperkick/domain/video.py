"""Helpers puros para representar tiempos."""

from __future__ import annotations

def formato_legible(segundos: float) -> str:
    total = int(segundos)
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d}"


def formato_nombre(segundos: float) -> str:
    return formato_legible(segundos).replace(":", "-")
