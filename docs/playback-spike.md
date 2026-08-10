# Spike de reproducción de T08

## Decisión

T08 integra **QtMultimedia** detrás de `PlaybackPort`. No se adopta como una
afirmación de superioridad universal sobre libmpv o VLC: es el backend que pudo
probarse de forma real en el entorno Windows disponible, comparte el runtime de
la carcasa PySide6, monta directamente `QVideoWidget` y cumplió los checks
automatizables de seek, rangos, playlist y liberación de archivos.

Si la observación manual de sync/overlay falla en otro equipo, o el proxy
segmentado futuro exige transiciones más estrechas, el puerto permite sustituir
el adaptador. El siguiente spike debe instalar **binding y runtime nativo** de
libmpv/VLC antes de compararlos; no se deben rellenar sus columnas con valores
estimados.

## Evidencia capturada

Resultado completo y legible por máquina:
[`evidence/playback-spike-windows.json`](evidence/playback-spike-windows.json).

La corrida del 10 de agosto de 2026 usó Windows 11, Python 3.13.7, PySide6 /
Qt 6.11.1 y FFmpeg 8.0. QtMultimedia estuvo disponible; `python-mpv` y
`python-vlc` no estaban instalados, por lo que no existen métricas inventadas
para esos dos candidatos.

| Criterio automatizable | QtMultimedia | libmpv | VLC |
| --- | --- | --- | --- |
| CFR / VFR | Carga y seek a progreso medidos | No medido: dependencia ausente | No medido: dependencia ausente |
| Rango 0.4–1.4 s | Fin y exceso de posición medidos; valor exacto en el JSON | No medido | No medido |
| Cambio entre segmentos | Medido con playlist de dos rangos | No medido | No medido |
| Overlay Qt | Superficie, overlay y medio montados; inspección visual pendiente | No medido | No medido |
| Cierre Windows | 5/5 tras vaciar playlist y 5/5 tras `liberar()` permitieron renombrar | No medido | No medido |
| Distribución | PySide6 ya es el extra desktop; el JSON registra su huella instalada | Binding y DLL nativa ausentes | Binding y libVLC ausentes |

La latencia de seek registrada significa **`setPosition` hasta que el reloj de
reproducción vuelve a avanzar 20 ms**. No es la señal síncrona de
`setPosition`, que daría una cifra artificialmente cercana a cero. La primera
búsqueda de cada fixture incluye el arranque en frío; el JSON conserva cada
muestra y la mediana, en vez de ocultar esa diferencia.

## Lo que la automatización no afirma

`QMediaPlayer` no expone un reloj de audio independiente con el que este harness
pueda calcular deriva A/V, y una captura offscreen no demuestra la composición
real de una superficie acelerada. Por eso A/V sync y overlay visual quedan
marcados como `no_medido_automaticamente`, no como aprobados.

El modo manual genera un flash y un pulso de audio simultáneos una vez por
segundo y coloca un label Qt sobre el video. La persona debe confirmar que pulso
y flash coinciden y que el label permanece visible sobre la imagen.

## Reproducción

Desde la raíz del repositorio, sin red:

```powershell
python -m pip install -e ".[desktop]"
python scripts/playback_spike.py --iterations 5 --strict --output playback-result.json
```

Para conservar los fixtures e inspeccionar A/V sync y overlay en una ventana
real durante ocho segundos:

```powershell
python scripts/playback_spike.py --fixtures build/playback-spike --iterations 5 --manual-seconds 8
```

El harness genera H.264/AAC CFR, VFR, un fixture de sync y dos segmentos con el
FFmpeg presente en `PATH`; `ffprobe` deja codecs, duración, frame rates y time
base en el JSON. Si falta una dependencia, registra el motivo y el remedio y
continúa con la evidencia disponible. `--strict` solo devuelve error cuando el
backend elegido no está disponible o falla una medición automatizada.

## Riesgos abiertos y fallback

- Falta completar la observación humana de A/V sync y overlay en una pantalla y
  dispositivo de audio reales.
- La huella de distribución es la instalación local completa de PySide6, no una
  proyección de un instalador de producción; empaquetado sigue fuera de T08.
- libmpv y VLC siguen siendo candidatos de fallback deliberados, no fallbacks
  automáticos. Si QtMultimedia falla el corpus manual, se instala cada runtime,
  se implementa su adaptador al mismo `PlaybackPort` y se repite exactamente el
  mismo harness antes de cambiar la decisión.
