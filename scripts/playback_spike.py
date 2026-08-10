"""Harness reproducible del spike de reproduccion de T08.

Genera un corpus sintetico sin red, sondea QtMultimedia/libmpv/VLC y, cuando
QtMultimedia esta disponible, mide seek, fin de rango, cambio de playlist y
liberacion del archivo. La sincronizacion A/V percibida y la composicion visual
del overlay se declaran manuales: QMediaPlayer no expone un reloj de audio
independiente ni una captura fiable de superficies aceleradas.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from clipperkick.domain.playback import (  # noqa: E402
    ElementoPlaylist, EstadoReproduccion, RangoReproduccion,
)
from clipperkick.infrastructure.playback import QTMULTIMEDIA, sondear  # noqa: E402


def ejecutar(argumentos: list[str]) -> str:
    resultado = subprocess.run(argumentos, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace")
    if resultado.returncode:
        detalle = (resultado.stderr or resultado.stdout).strip()
        raise RuntimeError(f"fallo {resultado.returncode}: {' '.join(argumentos[:2])}: {detalle}")
    return resultado.stdout


def generar_fixtures(carpeta: Path) -> dict[str, Path]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg no esta en PATH; no se puede generar el corpus sintetico")
    carpeta.mkdir(parents=True, exist_ok=True)
    fixtures = {nombre: carpeta / f"{nombre}.mp4"
                for nombre in ("cfr", "vfr", "sync", "segmento-a", "segmento-b")}

    comun = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    codificar = ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]
    ejecutar(comun + [
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000:duration=4",
        "-shortest", *codificar, str(fixtures["cfr"]),
    ])
    ejecutar(comun + [
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=750:sample_rate=48000:duration=4",
        "-vf", "select=if(lt(t\\,2)\\,not(mod(n\\,2))\\,not(mod(n\\,3)))",
        "-fps_mode", "vfr", "-shortest", *codificar, str(fixtures["vfr"]),
    ])
    ejecutar(comun + [
        "-f", "lavfi", "-i",
        "color=black:size=320x180:rate=30:duration=4,"
        "drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable=lt(mod(t\\,1)\\,0.12)",
        "-f", "lavfi", "-i",
        "aevalsrc=if(lt(mod(t\\,1)\\,0.12)\\,0.8*sin(2*PI*1000*t)\\,0):s=48000:d=4",
        "-shortest", *codificar, str(fixtures["sync"]),
    ])
    for nombre, color, frecuencia in (("segmento-a", "red", 440),
                                      ("segmento-b", "blue", 880)):
        ejecutar(comun + [
            "-f", "lavfi", "-i", f"color={color}:size=320x180:rate=30:duration=1.5",
            "-f", "lavfi", "-i", f"sine=frequency={frecuencia}:sample_rate=48000:duration=1.5",
            "-shortest", *codificar, str(fixtures[nombre]),
        ])
    return fixtures


def describir_fixture(ruta: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return {"archivo": ruta.name, "error": "ffprobe no esta en PATH"}
    documento = json.loads(ejecutar([
        ffprobe, "-v", "error", "-show_entries",
        "format=duration:stream=index,codec_type,codec_name,r_frame_rate,avg_frame_rate,time_base",
        "-of", "json", str(ruta),
    ]))
    documento["archivo"] = ruta.name
    documento["bytes"] = ruta.stat().st_size
    return documento


def huella_instalacion(distribucion: str) -> dict[str, Any]:
    try:
        paquete = importlib.metadata.distribution(distribucion)
    except importlib.metadata.PackageNotFoundError:
        return {"instalado": False}
    total = 0
    contados = 0
    for relativo in paquete.files or ():
        ruta = Path(paquete.locate_file(relativo))
        try:
            if ruta.is_file():
                total += ruta.stat().st_size
                contados += 1
        except OSError:
            continue
    return {"instalado": True, "version": paquete.version,
            "archivos_contados": contados, "bytes_instalados": total}


class Oyente:
    def __init__(self) -> None:
        self.progresos: list[tuple[float, Any]] = []
        self.fallos: list[Any] = []

    def progreso(self, progreso) -> None:
        self.progresos.append((time.perf_counter(), progreso))

    def fallo(self, fallo) -> None:
        self.fallos.append(fallo)

    @property
    def ultimo(self):
        return self.progresos[-1][1] if self.progresos else None


class MedicionesQt:
    def __init__(self, app, fixtures: dict[str, Path], iteraciones: int) -> None:
        self.app, self.fixtures, self.iteraciones = app, fixtures, iteraciones

    def esperar(self, condicion: Callable[[], bool], timeout: float = 8.0) -> float:
        inicio = time.perf_counter()
        while not condicion() and time.perf_counter() - inicio < timeout:
            self.app.processEvents()
            time.sleep(0.005)
        if not condicion():
            raise TimeoutError(f"Qt no alcanzo el estado esperado en {timeout:.1f}s")
        return (time.perf_counter() - inicio) * 1000.0

    def nuevo(self, *, salida_video=None, con_audio: bool = False):
        from clipperkick.infrastructure.playback.qtmultimedia import AdaptadorQtMultimedia

        adaptador = AdaptadorQtMultimedia(salida_video, con_audio=con_audio)
        oyente = Oyente()
        adaptador.suscribir(oyente)
        return adaptador, oyente

    def cargar(self, ruta: Path, rango: RangoReproduccion | None = None):
        adaptador, oyente = self.nuevo()
        adaptador.cargar([ElementoPlaylist("fixture", str(ruta), rango, ruta.name)])
        carga_ms = self.esperar(lambda: oyente.ultimo is not None and oyente.ultimo.duracion > 0)
        if oyente.fallos:
            raise RuntimeError(oyente.fallos[-1].mensaje)
        return adaptador, oyente, carga_ms

    def seek(self, nombre: str) -> dict[str, Any]:
        adaptador, oyente, carga_ms = self.cargar(self.fixtures[nombre])
        latencias = []
        try:
            for destino in (0.5, 1.5, 2.75):
                anteriores = len(oyente.progresos)
                inicio = time.perf_counter()
                adaptador.buscar(destino)
                adaptador.reproducir()
                # setPosition puede emitir sincronicamente el destino aunque el
                # decoder aun no haya entregado nada. Se mide hasta que el reloj
                # vuelve a avanzar, no hasta esa confirmacion administrativa.
                self.esperar(lambda: any(
                    progreso.estado is EstadoReproduccion.REPRODUCIENDO
                    and progreso.posicion >= destino + 0.02
                    for _instante, progreso in oyente.progresos[anteriores:]))
                latencias.append(round((time.perf_counter() - inicio) * 1000.0, 3))
                adaptador.pausar()
        finally:
            adaptador.liberar()
            self.app.processEvents()
        ordenadas = sorted(latencias)
        return {"fixture": nombre, "carga_ms": round(carga_ms, 3),
                "seek_a_progreso_ms": latencias, "mediana_ms": ordenadas[len(ordenadas) // 2],
                "avance_requerido_ms": 20}

    def rango(self) -> dict[str, Any]:
        rango = RangoReproduccion(0.4, 1.4)
        adaptador, oyente, _carga = self.cargar(self.fixtures["cfr"], rango)
        inicio = time.perf_counter()
        try:
            adaptador.reproducir()
            self.esperar(lambda: oyente.ultimo is not None
                         and oyente.ultimo.estado is EstadoReproduccion.FINALIZADO)
            pared = (time.perf_counter() - inicio) * 1000.0
            observadas = [p.posicion for _t, p in oyente.progresos
                          if p.estado is not EstadoReproduccion.FINALIZADO]
            exceso = max(0.0, ((max(observadas) if observadas else rango.inicio) - rango.fin) * 1000)
            return {"rango_s": [rango.inicio, rango.fin], "pared_ms": round(pared, 3),
                    "exceso_observado_ms": round(exceso, 3)}
        finally:
            adaptador.liberar()
            self.app.processEvents()

    def playlist(self) -> dict[str, Any]:
        adaptador, oyente = self.nuevo()
        rango = RangoReproduccion(0.0, 1.2)
        elementos = [ElementoPlaylist(nombre, str(self.fixtures[nombre]), rango, nombre)
                     for nombre in ("segmento-a", "segmento-b")]
        adaptador.cargar(elementos)
        self.esperar(lambda: oyente.ultimo is not None and oyente.ultimo.duracion > 0)
        duracion_primero = rango.duracion
        inicio = time.perf_counter()
        try:
            adaptador.reproducir()
            self.esperar(lambda: oyente.ultimo is not None and oyente.ultimo.indice == 1
                         and oyente.ultimo.estado is EstadoReproduccion.REPRODUCIENDO,
                         timeout=duracion_primero + 5.0)
            pared = (time.perf_counter() - inicio) * 1000.0
            return {"duracion_primer_segmento_ms": round(duracion_primero * 1000, 3),
                    "cambio_observado_ms": round(pared, 3),
                    "exceso_sobre_duracion_ms": round(max(0.0, pared - duracion_primero * 1000), 3)}
        finally:
            adaptador.liberar()
            self.app.processEvents()

    def liberar(self) -> dict[str, Any]:
        ruta = self.fixtures["cfr"]
        resultados = {}
        for modo in ("vaciar_playlist", "liberar_backend"):
            latencias = []
            for vuelta in range(self.iteraciones):
                adaptador, _oyente, _carga = self.cargar(ruta)
                inicio = time.perf_counter()
                if modo == "vaciar_playlist":
                    adaptador.cargar(())
                else:
                    adaptador.liberar()
                liberada = ruta.with_name(f"{ruta.stem}.{modo}-{vuelta}{ruta.suffix}")
                ultimo_error = ""
                limite = time.perf_counter() + 3.0
                while time.perf_counter() < limite:
                    self.app.processEvents()
                    try:
                        ruta.rename(liberada)
                        liberada.rename(ruta)
                        latencias.append(round((time.perf_counter() - inicio) * 1000.0, 3))
                        break
                    except OSError as error:
                        ultimo_error = str(error)
                        time.sleep(0.01)
                else:
                    raise RuntimeError(f"el archivo siguio bloqueado: {ultimo_error}")
                if modo == "vaciar_playlist":
                    adaptador.liberar()
                    self.app.processEvents()
            resultados[modo] = {
                "iteraciones": self.iteraciones, "renombrados": len(latencias),
                "latencias_ms": latencias, "maximo_ms": max(latencias)}
        return resultados

    def overlay(self) -> dict[str, Any]:
        from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget
        from PySide6.QtMultimediaWidgets import QVideoWidget

        contenedor = QWidget()
        superficie = QVideoWidget(contenedor)
        overlay = QLabel("OVERLAY T08", superficie)
        overlay.move(8, 8)
        QVBoxLayout(contenedor).addWidget(superficie)
        contenedor.resize(360, 240)
        contenedor.show()
        adaptador, oyente = self.nuevo(salida_video=superficie)
        try:
            adaptador.cargar([ElementoPlaylist("overlay", str(self.fixtures["cfr"]))])
            self.esperar(lambda: oyente.ultimo is not None and oyente.ultimo.duracion > 0)
            return {"superficie_montada": superficie.parent() is contenedor,
                    "overlay_montado": overlay.parent() is superficie,
                    "medio_cargado": not bool(oyente.fallos),
                    "composicion_visual": "manual_requerida"}
        finally:
            adaptador.liberar()
            contenedor.close()
            self.app.processEvents()

    def manual(self, segundos: float) -> dict[str, Any]:
        """Muestra flashes + pulsos sincronizados bajo un overlay Qt."""
        from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget
        from PySide6.QtMultimediaWidgets import QVideoWidget

        contenedor = QWidget()
        contenedor.setWindowTitle("ClipsApp T08 — sync/overlay manual")
        superficie = QVideoWidget(contenedor)
        overlay = QLabel("OVERLAY T08 — cada flash debe coincidir con un pulso", superficie)
        overlay.setStyleSheet("color: #00ff88; background: rgba(0, 0, 0, 160); padding: 8px;")
        overlay.move(8, 8)
        QVBoxLayout(contenedor).addWidget(superficie)
        contenedor.resize(720, 440)
        contenedor.show()
        adaptador, oyente = self.nuevo(salida_video=superficie, con_audio=True)
        inicio = time.perf_counter()
        try:
            adaptador.cargar([ElementoPlaylist("sync", str(self.fixtures["sync"]))])
            self.esperar(lambda: oyente.ultimo is not None and oyente.ultimo.duracion > 0)
            adaptador.reproducir()
            while time.perf_counter() - inicio < segundos:
                self.app.processEvents()
                time.sleep(0.005)
            return {"estado": "observacion_humana_ejecutada", "segundos": segundos,
                    "instruccion": "confirmar pulso con flash y overlay visible sobre el video"}
        finally:
            adaptador.liberar()
            contenedor.close()
            self.app.processEvents()


def medir(nombre: str, funcion: Callable[[], dict[str, Any]], destino: dict[str, Any]) -> None:
    try:
        destino[nombre] = {"estado": "medido", **funcion()}
    except Exception as error:
        destino[nombre] = {"estado": "error", "detalle": str(error)}


def ejecutar_spike(carpeta: Path, iteraciones: int, manual_segundos: float = 0.0) -> dict[str, Any]:
    fixtures = generar_fixtures(carpeta)
    disponibilidades = [estado.__dict__ for estado in sondear()]
    resultado: dict[str, Any] = {
        "schema": 1,
        "capturado_utc": datetime.now(timezone.utc).isoformat(),
        "entorno": {"sistema": platform.platform(), "python": platform.python_version(),
                    "ffmpeg": ejecutar([shutil.which("ffmpeg"), "-version"]).splitlines()[0]},
        "disponibilidad": disponibilidades,
        "distribucion": {nombre: huella_instalacion(nombre)
                         for nombre in ("PySide6", "PySide6_Addons", "PySide6_Essentials",
                                        "shiboken6", "python-mpv", "python-vlc")},
        "fixtures": {nombre: describir_fixture(ruta) for nombre, ruta in fixtures.items()},
        "metricas": {},
        "observacion_manual": {
            "av_sync": "no_medido_automaticamente: verificar coincidencia perceptual entre patron y tono",
            "overlay_visual": "no_medido_automaticamente: verificar video visible bajo OVERLAY T08",
            "motivo": "Qt no expone un reloj de audio independiente ni captura portable de superficie acelerada",
        },
    }
    qt = next(estado for estado in disponibilidades if estado["nombre"] == QTMULTIMEDIA)
    if not qt["disponible"]:
        resultado["backend_elegido"] = None
        resultado["metricas"]["qtmultimedia"] = {
            "estado": "no_disponible", "detalle": qt["motivo"], "remedio": qt["remedio"]}
        return resultado

    if manual_segundos <= 0:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    mediciones = MedicionesQt(app, fixtures, iteraciones)
    qt_metricas: dict[str, Any] = {}
    for nombre, funcion in (("seek_cfr", lambda: mediciones.seek("cfr")),
                            ("seek_vfr", lambda: mediciones.seek("vfr")),
                            ("fin_de_rango", mediciones.rango),
                            ("playlist", mediciones.playlist),
                            ("liberacion_handle", mediciones.liberar),
                            ("overlay_qt", mediciones.overlay)):
        medir(nombre, funcion, qt_metricas)
    if manual_segundos > 0:
        qt_metricas["observacion_manual"] = mediciones.manual(manual_segundos)
    resultado["backend_elegido"] = QTMULTIMEDIA
    resultado["metricas"][QTMULTIMEDIA] = qt_metricas
    for candidato in ("libmpv", "vlc"):
        estado = next(item for item in disponibilidades if item["nombre"] == candidato)
        resultado["metricas"][candidato] = {
            "estado": "no_medido_dependencia_ausente" if not estado["disponible"] else "sin_driver_integrado",
            "detalle": estado["motivo"] or "el harness conserva el criterio, pero T08 no integra este backend",
        }
    return resultado


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, help="conserva el corpus en esta carpeta")
    parser.add_argument("--output", type=Path, help="escribe el resultado JSON en esta ruta")
    parser.add_argument("--iterations", type=int, default=5,
                        help="ciclos de carga/liberacion (default: 5)")
    parser.add_argument("--strict", action="store_true",
                        help="falla si una medicion automatica del backend elegido falla")
    parser.add_argument("--manual-seconds", type=float, default=0.0,
                        help="muestra el fixture sync/overlay durante N segundos")
    args = parser.parse_args()

    temporal = None
    carpeta = args.fixtures
    if carpeta is None:
        temporal = tempfile.TemporaryDirectory(prefix="clipsapp-playback-spike-")
        carpeta = Path(temporal.name)
    try:
        resultado = ejecutar_spike(carpeta, max(1, args.iterations), max(0.0, args.manual_seconds))
        serializado = json.dumps(resultado, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serializado + "\n", encoding="utf-8")
        print(serializado)
        errores = [metrica for backend in resultado["metricas"].values()
                   if isinstance(backend, dict)
                   for metrica in backend.values() if isinstance(metrica, dict)
                   and metrica.get("estado") == "error"]
        return 1 if args.strict and (resultado["backend_elegido"] is None or errores) else 0
    finally:
        if temporal is not None:
            temporal.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
