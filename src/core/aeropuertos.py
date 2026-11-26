"""Base de aeropuertos con vuelos comerciales (OurAirports), para buscar por país o ciudad.

El archivo aeropuertos.json se genera con scripts/generar_aeropuertos.py.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

RUTA = Path(__file__).resolve().parent / "aeropuertos.json"
MAX_CANDIDATOS_PAIS = 15   # tope de aeropuertos preseleccionados al elegir un país (15 x 3 muestras = 45 consultas)


@lru_cache(maxsize=1)
def datos() -> dict:
    try:
        return json.loads(RUTA.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"paises": {}, "aeropuertos": [], "metros": {}}


@lru_cache(maxsize=1)
def _por_codigo() -> dict[str, list]:
    return {a[0]: a for a in datos()["aeropuertos"]}


def info(codigo: str) -> dict | None:
    a = _por_codigo().get(codigo.upper())
    if not a:
        return None
    return {"iata": a[0], "ciudad": a[1], "nombre": a[2], "pais": a[3], "tipo": a[4]}


def ciudad(codigo: str) -> str:
    i = info(codigo)
    return i["ciudad"] if i else codigo


def nombre_pais(iso: str | None) -> str:
    if not iso:
        return ""
    return datos()["paises"].get(iso.upper(), iso.upper())


def continente(iso: str) -> str:
    """AF, AN, AS, EU, NA, OC o SA."""
    return datos().get("continentes", {}).get((iso or "").upper(), "")


def existe(codigo: str) -> bool:
    return codigo.upper() in _por_codigo()


def del_pais(iso: str) -> list[dict]:
    return [info(a[0]) for a in datos()["aeropuertos"] if a[3] == iso.upper()]  # type: ignore[misc]


def candidatos_pais(iso: str) -> list[str]:
    """Aeropuertos que se preseleccionan al elegir un país: los grandes (y los medianos si hay pocos),
    ordenados por tráfico (lista `principales`) y no alfabéticamente."""
    todos = del_pais(iso)
    grandes = [a["iata"] for a in todos if a["tipo"] == "L"]
    if len(grandes) < 5:
        grandes += [a["iata"] for a in todos if a["tipo"] == "M"]
    rango = {c: i for i, c in enumerate(datos().get("principales", []))}
    grandes.sort(key=lambda c: rango.get(c, len(rango)))   # sort estable: el resto queda en su orden
    return grandes[:MAX_CANDIDATOS_PAIS]
