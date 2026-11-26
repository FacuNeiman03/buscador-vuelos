"""Compatibilidad con la versión anterior (acceso directo del Inicio de Windows creado antes del refactor).

El programa ahora vive en src/main.py. Este archivo solo lo reenvía para que nada se rompa;
al volver a ejecutar scripts\\instalar.bat el acceso directo pasa a apuntar a src\\main.py
y este archivo se puede borrar.
"""
import runpy
import sys
from pathlib import Path

sys.argv[0] = str(Path(__file__).resolve().parent / "src" / "main.py")
runpy.run_path(sys.argv[0], run_name="__main__")
