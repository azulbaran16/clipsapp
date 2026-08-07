# Diseño de protección de `main` y versiones

## Decisión

El repositorio adopta una política equilibrada para un mantenedor único: pull request obligatorio sin aprobación externa, CI requerido y SemVer con releases automatizadas en GitHub.

## Protección

- La regla incluye administradores.
- El PR debe estar actualizado y todas las conversaciones deben resolverse.
- El check estable `Required checks` debe aprobarse.
- Se exige historial lineal; force-push y borrado quedan bloqueados.
- Se permiten squash y rebase, no merge commits.
- Las ramas fusionadas se eliminan automáticamente.

## Calidad

El CI prueba Python 3.10 y 3.13 sobre Windows. Cada variante valida la versión, ejecuta las pruebas y construye las distribuciones. Un job agregador produce el contexto requerido por la protección.

## Versiones y releases

`src/clipperkick/__init__.py` es la fuente única de la versión. Las etiquetas usan `vX.Y.Z`. Al publicar una etiqueta coincidente, GitHub Actions repite pruebas, crea wheel y source distribution, y genera una GitHub Release. No se publica en PyPI.

## Recuperación ante fallos

Un fallo de CI bloquea el merge. Una etiqueta que no coincide con el código o una prueba fallida bloquea la publicación. La protección se activa únicamente después de que el primer CI real haya producido correctamente el contexto requerido.
