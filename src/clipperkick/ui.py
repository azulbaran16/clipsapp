"""Interfaz gráfica de ClipperKick."""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .engine import ES_WINDOWS, ErrorAmigable, Motor


RAIZ_PROYECTO = Path(__file__).resolve().parents[2]


def ruta_configuracion() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home()))
    return base / "ClipperKick" / "config.json"


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ClipperKick — clips automáticos de tus directos")
        self.geometry("680x570")
        self.minsize(600, 500)
        self.cola: queue.Queue[tuple[str, object]] = queue.Queue()
        self.trabajando = False

        cuerpo = ttk.Frame(self, padding=14)
        cuerpo.pack(fill="both", expand=True)

        ttk.Label(
            cuerpo,
            text="Enlace del VOD (Kick, Twitch o YouTube) o archivo de video:",
        ).pack(anchor="w")
        fila_entrada = ttk.Frame(cuerpo)
        fila_entrada.pack(fill="x", pady=(2, 10))
        self.var_entrada = tk.StringVar()
        ttk.Entry(fila_entrada, textvariable=self.var_entrada).pack(
            side="left", fill="x", expand=True
        )
        ttk.Button(
            fila_entrada, text="Examinar…", command=self.examinar
        ).pack(side="left", padx=(6, 0))

        fila_opciones = ttk.Frame(cuerpo)
        fila_opciones.pack(fill="x", pady=(0, 10))
        ttk.Label(fila_opciones, text="Clips:").pack(side="left")
        self.var_clips = tk.IntVar(value=6)
        ttk.Spinbox(
            fila_opciones, from_=1, to=20, width=4, textvariable=self.var_clips
        ).pack(side="left", padx=(4, 16))
        ttk.Label(fila_opciones, text="Duración (s):").pack(side="left")
        self.var_duracion = tk.IntVar(value=45)
        ttk.Spinbox(
            fila_opciones,
            from_=15,
            to=120,
            increment=5,
            width=5,
            textvariable=self.var_duracion,
        ).pack(side="left", padx=(4, 16))
        self.var_vertical = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            fila_opciones, text="Vertical 9:16", variable=self.var_vertical
        ).pack(side="left", padx=(0, 12))
        self.var_hd = tk.BooleanVar(value=False)
        ttk.Checkbutton(fila_opciones, text="1080p", variable=self.var_hd).pack(
            side="left"
        )

        fila_marca = ttk.Frame(cuerpo)
        fila_marca.pack(fill="x", pady=(0, 10))
        ttk.Label(fila_marca, text="Canal:").pack(side="left")
        self.var_nombre = tk.StringVar()
        ttk.Entry(fila_marca, width=18, textvariable=self.var_nombre).pack(
            side="left", padx=(4, 16)
        )
        ttk.Label(fila_marca, text="Logo:").pack(side="left")
        self.var_logo = tk.StringVar()
        ttk.Entry(fila_marca, textvariable=self.var_logo).pack(
            side="left", fill="x", expand=True, padx=(4, 6)
        )
        ttk.Button(fila_marca, text="Elegir…", command=self.elegir_logo).pack(
            side="left"
        )

        fila_salida = ttk.Frame(cuerpo)
        fila_salida.pack(fill="x", pady=(0, 10))
        ttk.Label(fila_salida, text="Guardar en:").pack(side="left")
        self.var_salida = tk.StringVar(value=str(RAIZ_PROYECTO / "outputs"))
        ttk.Entry(fila_salida, textvariable=self.var_salida).pack(
            side="left", fill="x", expand=True, padx=(4, 6)
        )
        ttk.Button(
            fila_salida, text="Cambiar…", command=self.cambiar_salida
        ).pack(side="left")

        self.boton = ttk.Button(
            cuerpo, text="🎬  Generar clips", command=self.iniciar
        )
        self.boton.pack(fill="x", ipady=6, pady=(0, 10))

        self.texto = tk.Text(
            cuerpo,
            height=12,
            state="disabled",
            bg="#111111",
            fg="#7CFC9A",
            font=("Consolas", 9),
        )
        self.texto.pack(fill="both", expand=True)

        self.cargar_configuracion()
        self.escribir("Listo. Elige un video o pega el enlace de tu VOD.")
        self.after(150, self.revisar_cola)

    def cargar_configuracion(self) -> None:
        try:
            configuracion = json.loads(ruta_configuracion().read_text("utf-8"))
            self.var_nombre.set(configuracion.get("nombre", ""))
            self.var_logo.set(configuracion.get("logo", ""))
            if configuracion.get("salida"):
                self.var_salida.set(configuracion["salida"])
        except (OSError, ValueError, TypeError):
            pass

    def guardar_configuracion(self) -> None:
        ruta = ruta_configuracion()
        try:
            ruta.parent.mkdir(parents=True, exist_ok=True)
            ruta.write_text(
                json.dumps(
                    {
                        "nombre": self.var_nombre.get(),
                        "logo": self.var_logo.get(),
                        "salida": self.var_salida.get(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def escribir(self, mensaje: str) -> None:
        self.texto.configure(state="normal")
        self.texto.insert("end", mensaje + "\n")
        self.texto.see("end")
        self.texto.configure(state="disabled")

    def examinar(self) -> None:
        ruta = filedialog.askopenfilename(
            title="Elige tu grabación",
            filetypes=[
                ("Videos", "*.mp4 *.mkv *.mov *.flv *.ts"),
                ("Todos", "*.*"),
            ],
        )
        if ruta:
            self.var_entrada.set(ruta)

    def cambiar_salida(self) -> None:
        ruta = filedialog.askdirectory(title="¿Dónde guardo los clips?")
        if ruta:
            self.var_salida.set(ruta)

    def elegir_logo(self) -> None:
        ruta = filedialog.askopenfilename(
            title="Elige tu logo",
            filetypes=[
                ("Imágenes", "*.png *.jpg *.jpeg *.webp"),
                ("Todos", "*.*"),
            ],
        )
        if ruta:
            self.var_logo.set(ruta)

    def iniciar(self) -> None:
        if self.trabajando:
            return
        entrada = self.var_entrada.get().strip().strip('"')
        if not entrada:
            messagebox.showwarning(
                "Falta el video", "Pega el enlace del VOD o elige un archivo."
            )
            return
        try:
            numero_clips = self.var_clips.get()
            duracion = self.var_duracion.get()
        except tk.TclError:
            messagebox.showwarning(
                "Opciones inválidas", "Revisa la cantidad y la duración."
            )
            return

        salida = self.var_salida.get().strip().strip('"')
        if not salida:
            messagebox.showwarning(
                "Falta la carpeta", "Elige una carpeta para guardar los clips."
            )
            return

        self.trabajando = True
        self.guardar_configuracion()
        self.boton.configure(state="disabled", text="Trabajando…")
        threading.Thread(
            target=self.trabajo,
            args=(
                entrada,
                numero_clips,
                duracion,
                salida,
                self.var_vertical.get(),
                self.var_hd.get(),
                self.var_nombre.get().strip(),
                self.var_logo.get().strip().strip('"'),
            ),
            daemon=True,
        ).start()

    def trabajo(
        self,
        entrada: str,
        numero_clips: int,
        duracion: int,
        salida: str,
        vertical: bool,
        hd: bool,
        nombre: str,
        logo: str,
    ) -> None:
        motor = Motor(lambda mensaje: self.cola.put(("log", mensaje)))
        try:
            listos = motor.procesar(
                entrada,
                numero_clips,
                duracion,
                salida,
                vertical,
                hd,
                nombre,
                logo,
            )
            self.cola.put(("fin", (salida, len(listos))))
        except ErrorAmigable as error:
            self.cola.put(("error", str(error)))
        except Exception as error:  # La UI debe recuperarse incluso de fallos no previstos.
            self.cola.put(("error", f"Error inesperado:\n{error}"))

    def revisar_cola(self) -> None:
        try:
            while True:
                tipo, dato = self.cola.get_nowait()
                if tipo == "log":
                    self.escribir(str(dato))
                elif tipo == "fin":
                    carpeta, cantidad = dato  # type: ignore[misc]
                    self.escribir(
                        f"\n¡Listo! {cantidad} clips guardados en: {carpeta}"
                    )
                    self.terminar()
                    if ES_WINDOWS:
                        os.startfile(carpeta)  # type: ignore[attr-defined]
                elif tipo == "error":
                    mensaje = str(dato)
                    self.escribir("\n[ERROR] " + mensaje.replace("\n", " "))
                    self.terminar()
                    messagebox.showerror("ClipperKick", mensaje)
        except queue.Empty:
            pass
        self.after(150, self.revisar_cola)

    def terminar(self) -> None:
        self.trabajando = False
        self.boton.configure(state="normal", text="🎬  Generar clips")


def main() -> None:
    App().mainloop()
