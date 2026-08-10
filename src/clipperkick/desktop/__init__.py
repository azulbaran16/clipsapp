"""Carcasa PySide6 de ClipsApp.

El paquete se importa sin Qt: `viewmodels` es Python puro y solo `app`, `qt` y
`views` requieren PySide6. Esa separacion es lo que permite que la suite de
pruebas —y el CI, que no instala Qt— ejerza la logica de la carcasa completa.

Coexiste con `clipperkick.ui` (Tkinter) durante toda la migracion: son dos
entradas distintas sobre el mismo nucleo, no dos versiones del mismo programa.
"""
