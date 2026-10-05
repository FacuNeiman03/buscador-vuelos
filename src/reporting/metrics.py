"""Cálculo de métricas por búsqueda a partir de historial.db. Todos los precios son POR PERSONA.

Para cada combinación de fechas (ida, vuelta, origen, destino) se toma su opción más barata;
Mínimo / Máximo / Promedio se calculan sobre esas "mejores por combinación" de la última corrida,
así el máximo refleja "la peor fecha para viajar", no el vuelo más caro de todos.
"""
from __future__ import annotations

import datetime as dt
import statistics
import sqlite3
from statistics import mean

from ..core import aeropuertos
from ..core import database as db
from ..core.flight_searcher import ReglasFechas


def fmt_min(m) -> str:
    if not m:
        return "-"
    return f"{int(m) // 60}h {int(m) % 60:02d}m"


def _dias(ida: str, vta: str | None) -> int | None:
    if not vta:
        return None
    return (dt.date.fromisoformat(vta) - dt.date.fromisoformat(ida)).days


def _meta_desde_filas(nombre: str, filas: list[dict]) -> dict:
    """Metadatos para búsquedas que ya no están en config.yaml (se reconstruyen de la base)."""
    return {
        "nombre": nombre, "activa": False, "tipo": "ida_vuelta" if any(f["vuelta"] for f in filas) else "solo_ida",
        "pasajeros": filas[0]["pasajeros"] or 1,
        "origenes": sorted({f["origen"] for f in filas}), "destinos": sorted({f["destino"] for f in filas}),
        "alerta_precio_persona": None, "ahorro_minimo_pct": 5, "_en_config": False,
    }


def _normalizar(filas: list[dict]) -> list[dict]:
    for f in filas:
        f["total"] = f["precio"]
        f["precio"] = f["precio"] / (f["pasajeros"] or 1)
        f["dias"] = _dias(f["ida"], f["vuelta"])
        f["duracion_txt"] = fmt_min(f["duracion_min"])
        f["flexible"] = f.get("flexible") or 0
        f["destino_txt"] = f"{aeropuertos.ciudad(f['destino'])} ({f['destino']})"
    return filas


def _hhmm(s: str | None) -> str | None:
    return s[11:16] if s and len(s) >= 16 else None


def _aerolineas(txt: str | None) -> set[str]:
    return {a.strip().lower() for a in (txt or "").split(",") if a.strip()}


def agregar_horarios(con: sqlite3.Connection, corrida_id: int, filas: list[dict]) -> None:
    """Agrega a cada fila la hora de salida de la ida (ida_hora) y de la vuelta (vta_hora).

    Pasajes armados con varios tramos: horarios exactos de cada tramo. Ida y vuelta de Google: Google
    solo informa la ida, así que la vuelta sale de la consulta de solo ida de regreso (tabla vueltas):
    el vuelo de la misma aerolínea ese día (vta_aprox = True: Google lo confirma al elegir la vuelta)."""
    vtas = db.vueltas_corrida(con, corrida_id)
    for f in filas:
        f["ida_hora"], f["vta_hora"], f["vta_aerolinea"], f["vta_aprox"] = _hhmm(f.get("salida")), None, None, False
        tramos = (f.get("detalle") or {}).get("tramos") or []
        if tramos:
            ida = [t for t in tramos if t.get("sentido") == "ida"]
            vta = [t for t in tramos if t.get("sentido") == "vuelta"]
            if ida:
                f["ida_hora"] = _hhmm(ida[0].get("salida")) or f["ida_hora"]
            if vta:
                f["vta_hora"], f["vta_aerolinea"] = _hhmm(vta[0].get("salida")), vta[0].get("aerolineas")
            continue
        if not f.get("vuelta"):
            continue
        aer = _aerolineas(f.get("aerolineas"))
        for o in vtas.get((f["destino"], f["origen"], f["vuelta"]), []):   # en orden de precio
            if aer & _aerolineas(o["aerolineas"]):
                f["vta_hora"], f["vta_aerolinea"], f["vta_aprox"] = _hhmm(o["salida"]), o["aerolineas"], True
                break


DIAS_HABITUAL = 30


def nivel_precio(precios: list[float], actual: float, corridas: int) -> dict | None:
    """¿El precio de hoy es bajo, habitual o alto? Con los precios reales que se vieron para esta búsqueda
    (todas las fechas, últimos DIAS_HABITUAL días): habitual = entre el percentil 25 y el 75."""
    if len(precios) < 20 or corridas < 2:
        return None   # poca historia: el rango no sería confiable
    p25, med, p75 = statistics.quantiles(precios, n=4)
    nivel = "bajo" if actual < p25 - 0.5 else "alto" if actual > p75 + 0.5 else "habitual"
    return {"nivel": nivel, "actual": actual, "desde": p25, "hasta": p75, "mediana": med,
            "minimo": min(min(precios), actual), "maximo": max(precios),
            "diferencia": med - actual, "n": len(precios), "corridas": corridas, "dias": DIAS_HABITUAL}


def mejores_por_combo(filas: list[dict]) -> tuple[list[dict], list[dict]]:
    """(base, flexibles): la opción más barata de cada combinación, ordenadas por precio."""
    por_combo: dict = {}
    for f in filas:   # filas ya vienen ordenadas por precio/persona, escalas, duración
        por_combo.setdefault((f["ida"], f["vuelta"], f["origen"], f["destino"]), f)
    clave = lambda f: (f["precio"], f["escalas"], f["duracion_min"] or 10**6)  # noqa: E731
    base = sorted((c for c in por_combo.values() if not c["flexible"]), key=clave)
    flex = sorted((c for c in por_combo.values() if c["flexible"]), key=clave)
    return base, flex


def estadisticas(precios: list[float]) -> dict:
    if not precios:
        return {"minimo": None, "maximo": None, "promedio": None, "n": 0}
    return {"minimo": min(precios), "maximo": max(precios), "promedio": mean(precios), "n": len(precios)}


def texto_ruta(b: dict) -> str:
    flecha = "⇄" if b.get("tipo", "ida_vuelta") == "ida_vuelta" else "→"
    origenes, destinos = b.get("origenes", []), b.get("destinos", [])
    if b.get("destino_pais"):
        extra = f", detalle de los {b['explorar_top']} más baratos" if b.get("explorar_top") and \
            len(destinos) > b["explorar_top"] else ""
        return f"{'/'.join(origenes)} {flecha} {aeropuertos.nombre_pais(b['destino_pais'])} ({len(destinos)} aeropuertos{extra})"
    nombres = [f"{aeropuertos.ciudad(d)} ({d})" if aeropuertos.existe(d) else d for d in destinos]
    return f"{'/'.join(origenes)} {flecha} {', '.join(nombres) if len(nombres) <= 4 else '/'.join(destinos)}"


def reglas_de(meta: dict | None) -> ReglasFechas | None:
    if not meta or ("_en_config" in meta and not meta["_en_config"]):
        return None
    try:
        return ReglasFechas(meta)
    except (KeyError, TypeError):
        return None


def filtro_rango(meta: dict | None):
    """filtro(fila) -> bool: la fila cae en el rango de fechas VIGENTE y es del nivel de equipaje elegido."""
    reglas = reglas_de(meta)
    if reglas is None:
        return None
    nivel = int(meta.get("nivel_equipaje", 0) or 0)
    return lambda f: reglas.valida_iso(f["ida"], f["vuelta"]) and int(f["equipaje"] or 0) == nivel


def descripcion_rango(meta: dict) -> str:
    if meta.get("fechas"):
        return f"{len(meta['fechas'])} fecha(s) exacta(s)"
    d, h, v = meta.get("_desde"), meta.get("_hasta"), meta.get("_vuelta_hasta")
    if meta.get("viajar_desde") and d and h:
        return f"viaje entre {d:%d/%m/%Y} y {h:%d/%m/%Y}"
    txt = f"salidas del {d:%d/%m/%Y} al {h:%d/%m/%Y}" if d and h else ""
    if meta.get("meses_permitidos"):
        txt += f" · meses {', '.join(str(m) for m in meta['meses_permitidos'])}"
    if v:
        txt += f" · vuelta hasta {v:%d/%m/%Y}"
    return txt


def datos_busqueda(con: sqlite3.Connection, nombre: str, meta: dict | None) -> dict | None:
    corridas = db.corridas_utiles(con, nombre)
    if not corridas:
        return None
    filtro, reglas = filtro_rango(meta), reglas_de(meta)
    ult = corridas[-1]
    todas = _normalizar(db.filas_corrida(con, ult["id"]))
    filas = [f for f in todas if not filtro or filtro(f)]
    if not filas:
        return None
    b = dict(meta) if meta else _meta_desde_filas(nombre, filas)
    b.setdefault("_en_config", True)
    pax = int(b.get("pasajeros", 1))
    ida_vuelta = b.get("tipo", "ida_vuelta") == "ida_vuelta"

    agregar_horarios(con, ult["id"], filas)
    base, flex = mejores_por_combo(filas)
    if not base:          # solo hubo variantes flexibles: se usan como base
        base, flex = flex, []
    if not base:
        return None

    # --- corrida anterior (para deltas por combinación y del mínimo) ---
    previo: dict = {}
    prev_stats = None
    if len(corridas) >= 2:
        ant = corridas[-2]
        prev_filas = [f for f in _normalizar(db.filas_corrida(con, ant["id"])) if not filtro or filtro(f)]
        pb, _ = mejores_por_combo(prev_filas)
        for c in pb:
            previo[(c["ida"], c["vuelta"], c["origen"], c["destino"])] = c["precio"]
        prev_stats = estadisticas([c["precio"] for c in pb]) if pb else None
    for c in base + flex:
        c["previo"] = previo.get((c["ida"], c["vuelta"], c["origen"], c["destino"]))

    # --- cuánto suma el equipaje: precio de la misma opción sin equipaje (si se re-cotizó) ---
    nivel = int(b.get("nivel_equipaje", 0) or 0)
    if nivel:
        sin_eq: dict = {}
        for f in todas:
            if int(f["equipaje"] or 0) == 0 and (not reglas or reglas.valida_iso(f["ida"], f["vuelta"])):
                k = (f["ida"], f["vuelta"], f["destino"], f["tipo"])
                sin_eq[k] = min(sin_eq.get(k, float("inf")), f["precio"])
        for c in base + flex:
            c["sin_equipaje"] = sin_eq.get((c["ida"], c["vuelta"], c["destino"], c["tipo"]))

    # --- mejor precio según cómo se arma el pasaje ---
    por_tipo: dict = {}
    for c in base:
        por_tipo[c["tipo"]] = min(por_tipo.get(c["tipo"], float("inf")), c["precio"])

    stats = estadisticas([c["precio"] for c in base])
    hist = db.minimo_historico(con, nombre, filtro=filtro)
    hist_prev = db.minimo_historico(con, nombre, antes_de_corrida=ult["id"], filtro=filtro)
    mejor = base[0]
    recientes, n_rec = db.precios_recientes(
        con, nombre, (dt.date.today() - dt.timedelta(days=DIAS_HABITUAL)).isoformat(), filtro)
    nivel = nivel_precio(recientes, mejor["precio"], n_rec)
    prev_best = prev_stats["minimo"] if prev_stats else None

    # --- ofertas cambiando días: cada una se compara con el viaje de la duración pedida que sale EL MISMO
    #     DÍA (si no se consultó ese día, con el de la salida más cercana). Una fila por fechas (se queda con
    #     el aeropuerto más barato), así no aparece repetida por EZE y AEP.
    ahorro_min = float(b.get("ahorro_minimo_pct", 5) or 0) / 100
    por_salida: dict = {}
    for c in base:   # base viene ordenada por precio: queda la más barata de cada día de salida
        por_salida.setdefault((c["ida"], c["destino"]), c)
    ofertas, vistas = [], set()
    for f in flex:
        k = (f["ida"], f["vuelta"], f["destino"])
        if k in vistas:
            continue
        ref = por_salida.get((f["ida"], f["destino"]))
        if ref is None:
            fi = dt.date.fromisoformat(f["ida"])
            cerca = [c for c in base if c["destino"] == f["destino"]
                     and abs((dt.date.fromisoformat(c["ida"]) - fi).days) <= 3]
            ref = min(cerca, key=lambda c: (abs((dt.date.fromisoformat(c["ida"]) - fi).days), c["precio"])) \
                if cerca else None
        if not ref or not ref["precio"]:
            continue
        ahorro = ref["precio"] - f["precio"]
        if ahorro / ref["precio"] >= ahorro_min:
            vistas.add(k)
            ofertas.append({**f, "ahorro": ahorro, "ahorro_pct": round(100 * ahorro / ref["precio"]),
                            "ref_ida": ref["ida"], "ref_vuelta": ref["vuelta"], "ref_precio": ref["precio"],
                            "ref_dias": ref["dias"], "ref_mismo_dia": ref["ida"] == f["ida"]})
    ofertas.sort(key=lambda o: (o["precio"], -o["ahorro"]))

    por_mes: dict = {}
    por_dia: dict = {}
    for c in base:
        por_mes.setdefault(c["ida"][:7], c)
        por_dia.setdefault(c["ida"], c)

    alerta = b.get("alerta_precio_persona")
    novedad = bool((alerta and mejor["precio"] <= alerta) or (prev_best and mejor["precio"] < prev_best - 0.5)
                   or (alerta and ofertas and ofertas[0]["precio"] <= alerta))

    exploracion = []
    for e in db.exploracion(con, ult["id"]):
        if reglas and not reglas.valida_iso(e["ida"], e["vuelta"]):
            continue
        exploracion.append({"destino": e["destino"], "ciudad": aeropuertos.ciudad(e["destino"]), "precio": e["pp"],
                            "ida": e["ida"], "vuelta": e["vuelta"], "origen": e["origen"],
                            "aerolineas": e["aerolineas"], "escalas": e["escalas"], "link": e["link"],
                            "elegido": bool(e["elegido"])})
    return {
        "nombre": nombre,
        "slug": None,   # lo completa el generador
        "activa": bool(b.get("activa", True)),
        "en_config": b.get("_en_config", True),
        "ruta": texto_ruta(b),
        "multi_destino": len({f["destino"] for f in filas}) > 1,
        "exploracion": exploracion,
        "ida_vuelta": ida_vuelta,
        "pasajeros": pax,
        "duraciones": sorted({c["dias"] for c in base if c["dias"]}),
        "moneda": filas[0]["moneda"],
        "alerta": alerta,
        "actualizado": ult["fin"] or ult["inicio"],
        "parcial": ult["completa"] == db.PARCIAL,
        "proveedor": ult["proveedor"] if "proveedor" in ult.keys() else None,
        "consultas": ult["consultas"], "errores": ult["errores"],
        "corridas": len(corridas),
        "stats": stats,
        "prev_stats": prev_stats,
        "ahorro_vs_promedio": (stats["promedio"] - mejor["precio"]) if stats["promedio"] else None,
        "mejor": mejor, "prev_best": prev_best,
        "mejor_flex": ofertas[0] if ofertas and ofertas[0]["precio"] < mejor["precio"] else None,
        "hist_min": hist,
        "hist_prev": hist_prev,
        "nivel_precio": nivel,
        "top": base[:30],
        "ofertas": ofertas[:20],
        "por_mes": [por_mes[m] for m in sorted(por_mes)],
        "por_dia": [{"ida": d, "precio": por_dia[d]["precio"], "vuelta": por_dia[d]["vuelta"],
                     "aerolineas": por_dia[d]["aerolineas"]} for d in sorted(por_dia)],
        "evolucion": db.evolucion(con, nombre, filtro),
        "novedad": novedad,
        "por_tipo": por_tipo,
        "estrategia": b.get("estrategia", "ida_vuelta"),
        "equipaje": b.get("equipaje", "ninguno"),
        "escala_separada": bool(b.get("escala_separada")),
    }
