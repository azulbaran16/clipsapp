"""Lanzador compatible con doble clic para la carcasa PySide6.

Convive con `ClipperKick.pyw`: mientras dure la migracion ambas entradas abren
el mismo nucleo, una con Tkinter y otra con Qt.
"""

from pathlib import Path
import sys


RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / "src"))

from clipperkick.desktop.app import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
