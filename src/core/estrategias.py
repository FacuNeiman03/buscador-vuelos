"""Estrategias para bajar el precio más allá del "ida y vuelta" que arma Google.

1) Dos pasajes de solo ida  (estrategia: solo_ida / mixta)
   Se consulta cada fecha de IDA y cada fecha de VUELTA por separado (una consulta por día y ruta)
   y se arma la mejor combinación que respete la duración. Ventajas:
     * ida y vuelta pueden ser de aerolíneas distintas (típico ahorro en vuelos nacionales y low cost);
     * se puede salir por EZE y volver a AEP;
     * cuesta MENOS consultas: días_ida + días_vuelta en vez de días_ida × duraciones,
       y cubre todas las duraciones posibles "gratis".
   En `mixta`, además se consulta el ida y vuelta real en las mejores fechas, porque en vuelos
   largos el pasaje ida y vuelta suele ser más barato que dos solo ida.

2) Escala armada por separado  (escala_separada: true)
   Para las mejores fechas se prueba llegar a un hub (ej. MAD) con un pasaje y seguir con otro.
   Se respeta un tiempo mínimo de conexión (hay que pasar migraciones y volver a despachar),
   y se avisa que la aerolínea no protege la conexión.

Este módulo tiene solo lógica pura (sin red ni base), fácil de testear.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from . import aeropuertos
from .config import lista_duraciones

# Hubs sugeridos por continente del destino (desde Argentina). Se pueden cambiar por búsqueda.
HUBS_POR_CONTINENTE = {
    "EU": ["MAD", "BCN", "LIS", "GRU"],
    "AS": ["MAD", "IST", "DXB", "GRU"],
    "AF": ["MAD", "IST", "GRU"],
    "OC": ["SCL", "AKL", "GRU"],
    "NA": ["PTY", "BOG", "LIM", "GRU"],
    "SA": ["GRU", "SCL", "LIM"],
}

AVISO_ESCALA = ("Pasajes separados: si el primer vuelo se atrasa, la aerolínea del segundo no te espera ni te "
                "reubica. En la escala pasás migraciones y volvés a despachar: revisá si necesitás visa de ese país.")


def hubs_sugeridos(destinos: list[str], origenes: list[str]) -> list[str]:
    paises_dest = {aeropuertos.info(d)["pais"] for d in destinos if aeropuertos.info(d)}
    paises_orig = {aeropuertos.info(o)["pais"] for o in origenes if aeropuertos.info(o)}
    continentes = [aeropuertos.continente(p) for p in paises_dest]
    cont = max(set(continentes), key=continentes.count) if continentes else "EU"
    return [h for h in HUBS_POR_CONTINENTE.get(cont, HUBS_POR_CONTINENTE["EU"])
            if aeropuertos.info(h) and aeropuertos.info(h)["pais"] not in paises_dest | paises_orig]


# ============================================================================
# Fechas para consultar tramos de solo ida
# ============================================================================
@dataclass
class PlanTramos:
    idas: list[dt.date]
    vueltas: list[dt.date]
    base: set[int]            # duraciones "pedidas"
    flex: set[int] = field(default_factory=set)   # duraciones extra por tolerancia


def plan_tramos(reglas, b: dict) -> PlanTramos:
    """Qué días consultar de ida y de vuelta para cubrir todas las combinaciones válidas."""
    base = set(lista_duraciones(b))
    tol = int(b.get("tolerancia_dias", 0) or 0)
    flex = {d for d in range(max(1, min(base) - tol), max(base) + tol + 1)} - base if tol else set()
    durs = base | flex
    if reglas.fijas:
        pares = [(f["ida"], f["vuelta"]) for f in b["fechas"] if f["vuelta"]]
        idas = {i + dt.timedelta(days=k) for i, _ in pares for k in ((-1, 0, 1) if tol else (0,))}
        vueltas = {v + dt.timedelta(days=k) for _, v in pares for k in range(-tol, tol + 1)}
    else:
        idas, vueltas = set(), set()
        d = reglas.desde
        while d <= reglas.hasta:
            for dur in durs:
                v = d + dt.timedelta(days=dur)
                if reglas.valida(d, v):
                    idas.add(d)
                    vueltas.add(v)
            d += dt.timedelta(days=1)
    idas = {i for i in idas if any(reglas.valida(i, v, en_ventana=not reglas.fijas) for v in vueltas)}
    vueltas = {v for v in vueltas if any(reglas.valida(i, v, en_ventana=not reglas.fijas) for i in idas)}
    return PlanTramos(sorted(idas), sorted(vueltas), base, flex)


# ============================================================================
# Armado de opciones con varios pasajes
# ============================================================================
def tramo(sentido: str, origen: str, destino: str, fecha: dt.date, op: dict, link: str) -> dict:
    return {"sentido": sentido, "origen": origen, "destino": destino, "fecha": fecha.isoformat(),
            "precio": op["precio"], "aerolineas": op["aerolineas"], "escalas": op["escalas"],
            "duracion_min": op["duracion_min"], "salida": op["salida"], "llegada": op["llegada"],
            "ruta": op["ruta"], "link": link}


def _dt(s: str) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(s, "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


def emparejar(primero: list[dict], segundo: list[dict], min_h: float, max_h: float) -> tuple[dict, dict] | None:
    """Mejor par de tramos (cada uno = dict de `tramo`) con conexión entre min_h y max_h horas.
    Llegada del 1ro y salida del 2do están en hora local del mismo aeropuerto (el hub)."""
    mejor, clave = None, None
    for a in primero:
        llega = _dt(a["llegada"])
        if not llega:
            continue
        for b in segundo:
            sale = _dt(b["salida"])
            if not sale:
                continue
            horas = (sale - llega).total_seconds() / 3600
            if min_h <= horas <= max_h:
                k = (a["precio"] + b["precio"], a["escalas"] + b["escalas"])
                if clave is None or k < clave:
                    mejor, clave = (a, b), k
    return mejor


def duracion_total(tramos: list[dict]) -> int:
    """Minutos de viaje de una parte (ida o vuelta) armada con varios pasajes: duración de cada vuelo
    + tiempo de conexión en el hub. Llegada y salida en el hub están en la misma hora local, así que
    la cuenta es exacta aunque los aeropuertos de origen y destino tengan otro huso horario."""
    total = sum(t["duracion_min"] or 0 for t in tramos)
    for a, b in zip(tramos, tramos[1:]):
        llega, sale = _dt(a["llegada"]), _dt(b["salida"])
        if llega and sale and sale > llega:
            total += int((sale - llega).total_seconds() // 60)
    return total


def opcion_compuesta(tipo: str, tramos_ida: list[dict], tramos_vuelta: list[dict]) -> dict:
    """Arma una fila de `precios` (formato de opción) a partir de varios pasajes."""
    todos = tramos_ida + tramos_vuelta
    aer_ida = " + ".join(dict.fromkeys(t["aerolineas"] for t in tramos_ida))
    aer_vta = " + ".join(dict.fromkeys(t["aerolineas"] for t in tramos_vuelta))
    ruta = tramos_ida[0]["ruta"]
    for t in tramos_ida[1:]:
        ruta += " ⇢ " + t["ruta"].split(" → ", 1)[-1]   # ⇢ = cambio de pasaje
    detalle = {"tramos": todos}
    if tipo == "escala_separada":
        detalle["aviso"] = AVISO_ESCALA
    return {
        "precio": round(sum(t["precio"] for t in todos), 2),
        "aerolineas": aer_ida if aer_ida == aer_vta or not aer_vta else f"{aer_ida} / {aer_vta}",
        "escalas": sum(t["escalas"] for t in tramos_ida) + len(tramos_ida) - 1,
        "duracion_min": duracion_total(tramos_ida),
        "salida": tramos_ida[0]["salida"], "llegada": tramos_ida[-1]["llegada"],
        "ruta": ruta, "link": tramos_ida[0]["link"], "tipo": tipo, "detalle": detalle,
    }


def combinar_solo_ida(reglas, plan: PlanTramos, idas: dict, vueltas: dict, destinos: list[str]):
    """Mejor combinación de dos solo ida por (fecha ida, fecha vuelta, destino).

    idas[(origen, destino, fecha)]    -> tramo más barato de ida
    vueltas[(destino, origen, fecha)] -> tramo más barato de vuelta (el origen puede ser otro: EZE / AEP)
    Devuelve [(ida, vuelta, destino, es_base, opcion)].
    """
    mejor_ida: dict = {}
    for (o, d, f), t in idas.items():
        if (d, f) not in mejor_ida or t["precio"] < mejor_ida[(d, f)]["precio"]:
            mejor_ida[(d, f)] = t
    mejor_vta: dict = {}
    for (d, o, f), t in vueltas.items():
        if (d, f) not in mejor_vta or t["precio"] < mejor_vta[(d, f)]["precio"]:
            mejor_vta[(d, f)] = t
    out = []
    for d in destinos:
        for i in plan.idas:
            ti = mejor_ida.get((d, i))
            if not ti:
                continue
            for v in plan.vueltas:
                dur = (v - i).days
                if dur not in plan.base and dur not in plan.flex:
                    continue
                if not reglas.valida(i, v, en_ventana=not reglas.fijas):
                    continue
                tv = mejor_vta.get((d, v))
                if not tv:
                    continue
                es_base = reglas.es_base(i, v) if reglas.fijas else dur in plan.base
                out.append((i, v, d, es_base, opcion_compuesta("dos_solo_ida", [ti], [tv])))
    out.sort(key=lambda x: x[4]["precio"])
    return out
