# Diseño de limpieza y separación de ClipperKick

## Decisión

`C:\Repositorios\ClipsApp` contendrá únicamente la aplicación ClipperKick. Los recursos de OBS, branding y material gráfico se conservarán en el proyecto hermano `C:\Repositorios\StreamerAssets`.

## Estado inicial

- Una aplicación Tkinter monolítica (`ClipperKick.pyw`) que usa FFmpeg y, para URLs, `yt-dlp`.
- Salidas generadas, caché de Python, fotogramas de revisión y recursos gráficos mezclados con el código.
- Una colección exportada de OBS (`Andres16Z.json`) con ocho rutas absolutas a `StreamPack`.
- Sin repositorio Git, manifiesto de proyecto, pruebas automatizadas ni documentación Markdown.
- Texto con mojibake visible en la aplicación y en las instrucciones.

## Estructura objetivo

```text
ClipsApp/
  ClipperKick.pyw          # lanzador compatible con doble clic
  src/clipperkick/
    engine.py              # descarga, análisis y exportación
    ui.py                  # interfaz Tkinter
  tests/                   # pruebas del motor y utilidades
  docs/plans/              # decisiones de diseño
  README.md
  pyproject.toml
  .gitignore

StreamerAssets/
  obs/                     # colección y paquete consumido por OBS
  branding/                # piezas finales
  fonts/                   # fuentes del paquete visual
  previews/                # referencias y vistas previas útiles
  archive/                 # iteraciones conservadas, no operativas
```

## Comportamiento

El algoritmo de detección por intensidad de audio se conserva. El motor queda desacoplado de Tkinter para poder probarlo. Se corrigen textos, validaciones y reporte de fallos de FFmpeg. La salida predeterminada será `outputs/`, ignorada por Git, para que los resultados no se confundan con fuentes.

## Migración y limpieza

Se actualizarán las rutas del JSON de OBS al nuevo proyecto. Se eliminarán únicamente artefactos regenerables inequívocos (`__pycache__`, fotogramas de revisión y capturas auxiliares); los diseños intermedios se archivarán. Antes de retirar o recomprimir recursos grandes se comprobará si están referenciados.

## Verificación

- Parseo del código y del JSON de OBS.
- Pruebas unitarias de nombres, filtros, selección de picos y errores.
- Exportación corta contra un clip existente.
- Comprobación de existencia de todas las rutas locales del JSON.
- Comparación del tamaño y contenido antes/después.
