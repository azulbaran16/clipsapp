"""Lanzador compatible con doble clic para ClipperKick."""

from pathlib import Path
import sys


RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ / "src"))

from clipperkick.ui import main  # noqa: E402


if __name__ == "__main__":
    main()
