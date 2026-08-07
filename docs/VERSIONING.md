# Estrategia de versiones

ClipperKick usa versiones estables `MAJOR.MINOR.PATCH` y etiquetas Git con prefijo `v`, por ejemplo `v1.2.3`.

## Cuándo incrementar cada número

- `MAJOR`: cambios incompatibles en configuración, comportamiento documentado o formatos de salida.
- `MINOR`: funcionalidad nueva compatible con el uso existente.
- `PATCH`: correcciones, mejoras internas y cambios documentales que acompañen una publicación.

Mientras el proyecto no necesite distribuir versiones preliminares, no se usarán sufijos alpha, beta o release candidate.

## Fuente única

La versión se declara únicamente en `src/clipperkick/__init__.py`. Setuptools la obtiene dinámicamente al construir el paquete. `python scripts/check_version.py` valida el formato.

## Flujo de publicación

1. Crear una rama desde `main`.
2. Actualizar `__version__` y mover las novedades de `Unreleased` a una sección fechada de `CHANGELOG.md`.
3. Abrir un pull request y esperar el check obligatorio.
4. Fusionar mediante squash o rebase.
5. Crear y publicar una etiqueta anotada que coincida exactamente: `vX.Y.Z`.
6. La Action de release valida etiqueta, ejecuta pruebas, construye wheel y source distribution, y crea la GitHub Release.

Si la etiqueta no coincide con el código o las pruebas fallan, no se publica la release. PyPI queda fuera de este flujo hasta que exista una necesidad explícita.
