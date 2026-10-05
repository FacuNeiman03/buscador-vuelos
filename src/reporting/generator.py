"""Genera reportes/index.html (todas las búsquedas) y reportes/<slug>.html (una por búsqueda).

El índice SIEMPRE se arma leyendo historial.db, así que correr `--busqueda China`
ya no borra los resultados de Cipolletti ni de ninguna otra búsqueda.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import sqlite3
from pathlib import Path

from ..core import database as db
from ..core.config import AppConfig
from ..core.paths import PLANTILLA_REPORTE, REPORTE_INDEX, REPORTES_DIR, reporte_busqueda, slug
from ..core.config_editor import form_desde_meta
from ..core.programacion import proxima_ejecucion
from .metrics import datos_busqueda, descripcion_rango, texto_ruta

log = logging.getLogger("vuelos")
MARCADOR = "/*__DATOS__*/null"


def json_seguro(obj) -> str:
    """JSON apto para incrustar dentro de <script>: no puede cerrar la etiqueta ni romper el JS."""
    return (json.dumps(obj, ensure_ascii=False, default=str)
            .replace("</", "<\\/").replace("<!--", "<\\!--")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def _escribir_atomico(ruta: Path, contenido: str) -> None:
    tmp = ruta.with_suffix(ruta.suffix + ".tmp")
    tmp.write_text(contenido, encoding="utf-8")
    os.replace(tmp, ruta)


def _sin_datos(meta: dict) -> dict:
    ida_vuelta = meta.get("tipo", "ida_vuelta") == "ida_vuelta"
    return {
        "nombre": meta["nombre"], "activa": meta["activa"], "en_config": True, "sin_datos": True,
        "ruta": texto_ruta(meta),
        "ida_vuelta": ida_vuelta, "pasajeros": meta.get("pasajeros", 1),
    }


def recolectar(con: sqlite3.Connection, cfg: AppConfig) -> list[dict]:
    """Todas las búsquedas del config (activas primero, después pausadas), tengan o no precios todavía.

    Solo se muestran precios que caen dentro del rango de fechas VIGENTE de cada búsqueda.
    Las búsquedas borradas del config no aparecen (su historial queda en la base).
    """
    datos = []
    for meta in sorted(cfg.busquedas, key=lambda b: not b["activa"]):
        d = datos_busqueda(con, meta["nombre"], meta)
        if d is None:
            d = _sin_datos(meta)
            d["con_historial"] = bool(db.corridas_utiles(con, meta["nombre"]))
        d["slug"] = slug(meta["nombre"])
        d["rango"] = descripcion_rango(meta)
        d["config"] = form_desde_meta(meta)
        d["proxima"] = proxima_ejecucion(meta)
        datos.append(d)
    return datos


def _render(plantilla: str, busquedas: list[dict], enlaces: list[dict], modo: str, g: dict) -> str:
    payload = {
        "busquedas": busquedas,
        "enlaces": enlaces,
        "modo": modo,
        "generado": dt.datetime.now().strftime("%d/%m/%Y %H:%M"),
        "repo": os.environ.get("GITHUB_REPOSITORY", ""),
        "url_reporte": g.get("url_reporte", ""),
        "hora_default": g.get("hora", 6),
    }
    if MARCADOR not in plantilla:
        raise RuntimeError(f"La plantilla no tiene el marcador {MARCADOR}")
    return plantilla.replace(MARCADOR, json_seguro(payload))


def generar_reportes(con: sqlite3.Connection, cfg: AppConfig) -> tuple[Path, bool, list[dict]]:
    """Devuelve (ruta de index.html, hay_novedad, datos)."""
    REPORTES_DIR.mkdir(parents=True, exist_ok=True)
    plantilla = PLANTILLA_REPORTE.read_text(encoding="utf-8")
    datos = recolectar(con, cfg)
    enlaces = [{"nombre": d["nombre"], "slug": d["slug"], "activa": d["activa"]} for d in datos]

    _escribir_atomico(REPORTE_INDEX, _render(plantilla, datos, enlaces, "indice", cfg.general))
    vigentes = {REPORTE_INDEX.name}
    for d in datos:
        ruta = reporte_busqueda(d["nombre"])
        _escribir_atomico(ruta, _render(plantilla, [d], enlaces, "individual", cfg.general))
        vigentes.add(ruta.name)

    # limpia reportes individuales de búsquedas que ya no existen
    for viejo in REPORTES_DIR.glob("*.html"):
        if viejo.name not in vigentes:
            try:
                viejo.unlink()
            except OSError:
                pass

    log.info(f"Reportes: {REPORTE_INDEX} (+{len(datos)} individuales)")
    return REPORTE_INDEX, any(d.get("novedad") for d in datos), datos
