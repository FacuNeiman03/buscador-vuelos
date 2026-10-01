"""Qué búsquedas tocan ahora (modo programado de la nube).

GitHub Actions dispara el workflow cada hora. Este módulo decide, sin dependencias pesadas
(solo PyYAML + sqlite3), qué búsquedas corresponde correr:

  * activa y dentro de su ventana (buscar_desde / buscar_hasta);
  * ya es su hora del día ('hora', o general.hora si no tiene; hora de Argentina);
  * hoy todavía no corrió bien, y no se intentó ya MAX_INTENTOS_DIA veces (si Google bloquea,
    no se insiste cada hora).

Si GitHub atrasa o se saltea una ejecución, la siguiente la recupera (es "hora >=", no "hora ==").

Uso en el workflow:   python -m src.core.programacion   -> escribe hay=true|false en $GITHUB_OUTPUT
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import sys

from .config import AppConfig, ConfigError, cargar_config
from .paths import DB_PATH

MAX_INTENTOS_DIA = 2
OK = (1, 2)   # COMPLETA, PARCIAL (ver database.py)


def _corridas_de_hoy(nombre: str, hoy: dt.date) -> tuple[int, int]:
    """(corridas buenas, intentos totales) de esa búsqueda hoy."""
    if not DB_PATH.exists():
        return 0, 0
    try:
        con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        try:
            filas = con.execute("SELECT completa FROM corridas WHERE busqueda=? AND substr(inicio,1,10)=?",
                                (nombre, hoy.isoformat())).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return 0, 0
    return sum(1 for (c,) in filas if c in OK), len(filas)


def pendientes(cfg: AppConfig, ahora: dt.datetime | None = None) -> list[dict]:
    ahora = ahora or dt.datetime.now()
    hoy = ahora.date()
    out = []
    for b in cfg.activas_en(hoy):
        if ahora.hour < b["hora"]:
            continue
        buenas, intentos = _corridas_de_hoy(b["nombre"], hoy)
        if buenas == 0 and intentos < MAX_INTENTOS_DIA:
            out.append(b)
    return out


def proxima_ejecucion(b: dict, ahora: dt.datetime | None = None) -> str | None:
    """Texto para la UI: cuándo vuelve a correr sola (None si está pausada o su ventana ya terminó)."""
    ahora = ahora or dt.datetime.now()
    if not b.get("activa", True):
        return None
    dia = ahora.date()
    if b.get("buscar_desde") and dia < b["buscar_desde"]:
        dia = b["buscar_desde"]
    elif ahora.hour >= b["hora"]:
        buenas, intentos = _corridas_de_hoy(b["nombre"], dia)
        if not buenas and intentos < MAX_INTENTOS_DIA and not (b.get("buscar_hasta") and dia > b["buscar_hasta"]):
            return "en la próxima hora"
        dia += dt.timedelta(days=1)
    if b.get("buscar_hasta") and dia > b["buscar_hasta"]:
        return None
    return f"{dia:%d/%m/%Y} {b['hora']:02d}:00"


def main() -> int:
    try:
        cfg = cargar_config()
    except ConfigError as e:
        print(f"Config inválido: {e}", file=sys.stderr)
        return 2
    nombres = [b["nombre"] for b in pendientes(cfg)]
    print(json.dumps({"ahora": dt.datetime.now().isoformat(timespec="minutes"), "pendientes": nombres},
                     ensure_ascii=False))
    salida = os.environ.get("GITHUB_OUTPUT")
    if salida:
        with open(salida, "a", encoding="utf-8") as f:
            f.write(f"hay={'true' if nombres else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
