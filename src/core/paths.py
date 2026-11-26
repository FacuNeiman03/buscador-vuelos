"""Rutas absolutas del proyecto, resueltas siempre desde PROJECT_ROOT.

Ningún otro módulo arma rutas por su cuenta: todos importan desde acá, así el
programa funciona igual si se lo llama desde el Inicio de Windows, desde un .bat,
desde GitHub Actions o desde el servidor web, sin importar el directorio actual.
"""
from __future__ import annotations

import os
import re
import shutil
import unicodedata
from pathlib import Path

# src/core/paths.py -> src/core -> src -> raíz del proyecto
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

CONFIG_DIR = PROJECT_ROOT / "config"
# Las variables de entorno permiten aislar datos en tests o apuntar a un disco persistente en la nube.
DATA_DIR = Path(os.environ.get("VUELOS_DATA", PROJECT_ROOT / "data"))
LOGS_DIR = Path(os.environ.get("VUELOS_LOGS", PROJECT_ROOT / "logs"))
REPORTES_DIR = Path(os.environ.get("VUELOS_REPORTES", PROJECT_ROOT / "reportes"))
SRC_DIR = PROJECT_ROOT / "src"
TEMPLATES_DIR = SRC_DIR / "templates"

CONFIG_PATH = Path(os.environ.get("VUELOS_CONFIG", CONFIG_DIR / "config.yaml"))
DB_PATH = Path(os.environ.get("VUELOS_DB", DATA_DIR / "historial.db"))
ESTADO_PATH = DATA_DIR / "estado.json"
LOCK_PATH = DATA_DIR / ".corriendo.lock"
LOG_PATH = LOGS_DIR / "vuelos.log"
REPORTE_INDEX = REPORTES_DIR / "index.html"
PLANTILLA_REPORTE = TEMPLATES_DIR / "plantilla_reporte.html"
PLANTILLA_EMAIL = TEMPLATES_DIR / "email_alerta.html"
REQUIREMENTS = PROJECT_ROOT / "requirements.txt"

# Archivos que la versión anterior (monolítica) dejaba en la raíz -> nuevo destino.
_LEGADO = {
    PROJECT_ROOT / "historial.db": DB_PATH,
    PROJECT_ROOT / "estado.json": ESTADO_PATH,
    PROJECT_ROOT / "vuelos.log": LOG_PATH,
    PROJECT_ROOT / "config.yaml": CONFIG_DIR / "config.yaml",
}


def asegurar_directorios() -> None:
    for d in (CONFIG_DIR, DATA_DIR, LOGS_DIR, REPORTES_DIR):
        d.mkdir(parents=True, exist_ok=True)


def migrar_legado() -> list[str]:
    """Mueve a su nueva carpeta los archivos de la versión anterior que sigan en la raíz.

    Solo mueve si el destino todavía no existe (nunca pisa datos).
    """
    asegurar_directorios()
    movidos: list[str] = []
    if any(os.environ.get(v) for v in ("VUELOS_DATA", "VUELOS_DB", "VUELOS_CONFIG")):
        return movidos   # rutas personalizadas (tests / nube): no tocar archivos de la raíz
    for viejo, nuevo in _LEGADO.items():
        if viejo.exists() and not nuevo.exists():
            nuevo.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(viejo), str(nuevo))
            movidos.append(f"{viejo.name} -> {nuevo.relative_to(PROJECT_ROOT)}")
    return movidos


def slug(nombre: str) -> str:
    """'Bangkok con amigo' -> 'bangkok-con-amigo' (seguro para nombres de archivo y URLs)."""
    s = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "busqueda"


def reporte_busqueda(nombre: str) -> Path:
    return REPORTES_DIR / f"{slug(nombre)}.html"
