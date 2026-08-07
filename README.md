# ClipperKick

ClipperKick analiza el volumen de una grabación o VOD, detecta los momentos más intensos y genera clips listos para revisar o publicar. Puede producir video horizontal o vertical 9:16, con logo y nombre del canal opcionales.

## Requisitos

- Windows 10/11.
- Python 3.10 o posterior con Tkinter.
- FFmpeg disponible en `PATH`.
- `yt-dlp` para descargar VODs; no es necesario al trabajar con archivos locales.

Instalación recomendada en PowerShell:

```powershell
winget install Python.Python.3.13
winget install Gyan.FFmpeg
python -m pip install -e .
```

Después de instalar, puedes abrir `ClipperKick.pyw` con doble clic o ejecutar:

```powershell
clipperkick
```

## Uso

1. Elige una grabación local o pega el enlace de un VOD de Kick, Twitch o YouTube.
2. Define la cantidad, duración y formato de los clips.
3. Añade el nombre del canal o un logo si los necesitas.
4. Pulsa **Generar clips**.

Los resultados se guardan por defecto en `outputs/`, que no forma parte del código ni se registra en Git. Para obtener mejores resultados, usa grabaciones con una pista de audio clara; la detección mide cambios de intensidad, no el contenido de la conversación.

## Desarrollo y verificación

```powershell
python -m unittest discover -s tests -v
python -m compileall -q src ClipperKick.pyw
```

La lógica de procesamiento está en `src/clipperkick/engine.py` y la interfaz en `src/clipperkick/ui.py`. Las decisiones de estructura se documentan en `docs/plans/`.

## Privacidad

Los archivos locales se procesan en el equipo. Al usar un enlace, `yt-dlp` descarga temporalmente el VOD y la copia temporal se elimina al finalizar.
