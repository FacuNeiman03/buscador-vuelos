"""Carga y validación de config/config.yaml.

Devuelve la configuración normalizada (tipos correctos, valores por defecto aplicados)
y junta TODOS los errores en un solo mensaje claro, en vez de explotar a mitad de una
corrida de 20 minutos por un typo.
"""
from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .paths import CONFIG_PATH

CLASES = {"economy", "premium-economy", "business", "first"}
TIPOS = {"ida_vuelta", "solo_ida"}
ESTRATEGIAS = {"ida_vuelta", "solo_ida", "mixta"}
EQUIPAJES = {"ninguno": 0, "mano": 1, "despachado": 2}
PROVEEDORES = {"auto", "fast_flights", "serpapi"}
MODOS_ABRIR = {"siempre", "si_hay_novedad", "nunca"}
_IATA = re.compile(r"^[A-Z]{3}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


HORA_DEFAULT = 6   # 06:00 AR: antes de que arranque el día, así el mail llega a la mañana


class ConfigError(ValueError):
    pass


@dataclass
class AppConfig:
    general: dict[str, Any]
    busquedas: list[dict[str, Any]]            # todas (activas e inactivas), normalizadas
    ruta: Path = field(default=CONFIG_PATH)

    @property
    def activas(self) -> list[dict[str, Any]]:
        """Activas y dentro de su ventana de ejecución (buscar_desde / buscar_hasta, si las tiene)."""
        return self.activas_en(dt.date.today())

    def activas_en(self, hoy: dt.date) -> list[dict[str, Any]]:
        return [b for b in self.busquedas if b["activa"] and en_ventana(b, hoy)]

    def buscar(self, nombre: str) -> dict[str, Any] | None:
        n = nombre.strip().lower()
        return next((b for b in self.busquedas if b["nombre"].lower() == n), None)


# ----------------------------------------------------------------------------
# Fechas
# ----------------------------------------------------------------------------
def en_ventana(b: dict, hoy: dt.date) -> bool:
    """True si hoy cae dentro de 'buscar_desde'..'buscar_hasta' (ambas opcionales, inclusivas)."""
    d, h = b.get("buscar_desde"), b.get("buscar_hasta")
    return (d is None or hoy >= d) and (h is None or hoy <= h)


def parse_fecha(valor: Any, hoy: dt.date) -> dt.date:
    """Acepta date, '2027-03-01' o '+15' (días desde hoy)."""
    if isinstance(valor, dt.datetime):
        return valor.date()
    if isinstance(valor, dt.date):
        return valor
    s = str(valor).strip()
    if re.fullmatch(r"[+-]?\d+", s):
        return hoy + dt.timedelta(days=int(s))
    return dt.date.fromisoformat(s)


def lista_duraciones(b: dict) -> list[int]:
    d = b.get("duracion_dias", [14])
    if isinstance(d, dict):
        return list(range(int(d["min"]), int(d["max"]) + 1))
    if isinstance(d, (int, float)):
        return [int(d)]
    return [int(x) for x in d]


# ----------------------------------------------------------------------------
# Validación
# ----------------------------------------------------------------------------
def _lista(v: Any) -> list:
    if v is None:
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _validar_general(g: dict, err: list[str]) -> dict:
    out = {
        "moneda": str(g.get("moneda", "USD")).upper(),
        "idioma": str(g.get("idioma", "es")),
        # País desde el que "consulta" Google (punto de venta). Sin esto Google usa el país de la IP:
        # desde GitHub Actions (EE.UU.) los precios salen distintos a los que ves vos en Argentina.
        "pais": str(g.get("pais", "AR")).upper(),
        "abrir_reporte": g.get("abrir_reporte", "siempre"),
        "una_vez_por_dia": bool(g.get("una_vez_por_dia", True)),
        "pausa_segundos": g.get("pausa_segundos", [2, 5]),
        "max_consultas_por_corrida": g.get("max_consultas_por_corrida", 400),
        "proveedor": str(g.get("proveedor", "auto")).lower(),
        "url_reporte": g.get("url_reporte") or os.environ.get("VUELOS_URL_REPORTE", ""),
    }
    # Hora del día (Argentina) a la que corren las búsquedas en la nube, salvo que tengan su propia 'hora'
    try:
        out["hora"] = int(g.get("hora", HORA_DEFAULT))
        if not 0 <= out["hora"] <= 23:
            raise ValueError
    except (TypeError, ValueError):
        err.append("general.hora debe ser un número entre 0 y 23")
        out["hora"] = HORA_DEFAULT
    if out["abrir_reporte"] not in MODOS_ABRIR:
        err.append(f"general.abrir_reporte debe ser uno de {sorted(MODOS_ABRIR)}")
    p = out["pausa_segundos"]
    if isinstance(p, (int, float)):
        p = [p, p]
    if not (isinstance(p, list) and len(p) == 2 and all(isinstance(x, (int, float)) and x >= 0 for x in p)):
        err.append("general.pausa_segundos debe ser [min, max] en segundos, ej: [2, 5]")
        p = [2, 5]
    out["pausa_segundos"] = sorted(float(x) for x in p)   # compatibilidad: ya no se usa (ver ritmo.py)

    # Velocidad de búsqueda (ver src/core/ritmo.py): rapida | normal | prudente
    out["velocidad"] = str(os.environ.get("VUELOS_VELOCIDAD") or g.get("velocidad", "normal")).lower()
    if out["velocidad"] not in ("rapida", "normal", "prudente"):
        err.append("general.velocidad debe ser rapida, normal o prudente")
        out["velocidad"] = "normal"
    for k, tipo, lo, hi in (("concurrencia", int, 1, 8), ("intervalo_min", float, 0, 30)):
        if g.get(k) is not None:
            try:
                out[k] = tipo(g[k])
                if not lo <= out[k] <= hi:
                    raise ValueError
            except (TypeError, ValueError):
                err.append(f"general.{k} debe estar entre {lo} y {hi}")
                out.pop(k, None)

    # Variables de entorno pisan la config (útil en la nube sin tocar el YAML)
    if os.environ.get("VUELOS_MAX_CONSULTAS"):
        out["max_consultas_por_corrida"] = os.environ["VUELOS_MAX_CONSULTAS"]
    if os.environ.get("VUELOS_PROVEEDOR"):
        out["proveedor"] = os.environ["VUELOS_PROVEEDOR"].lower()
    try:
        out["max_consultas_por_corrida"] = int(out["max_consultas_por_corrida"])
    except (TypeError, ValueError):
        err.append("general.max_consultas_por_corrida debe ser un número entero")
        out["max_consultas_por_corrida"] = 400
    if out["proveedor"] not in PROVEEDORES:
        err.append(f"general.proveedor debe ser uno de {sorted(PROVEEDORES)}")

    a = g.get("alertas") or {}
    emails = [str(e).strip() for e in _lista(a.get("emails"))]
    emails += [e.strip() for e in os.environ.get("ALERTA_EMAILS", "").split(",") if e.strip()]
    for e in emails:
        if not _EMAIL.match(e):
            err.append(f"general.alertas.emails: '{e}' no parece un email válido")
    out["alertas"] = {
        "activas": bool(a.get("activas", True)),
        "emails": sorted(set(emails)),
        "avisar_mejores_condiciones": bool(a.get("avisar_mejores_condiciones", True)),
        "remitente": a.get("remitente") or os.environ.get("ALERTA_REMITENTE", ""),
    }
    return out


def _validar_busqueda(b: dict, i: int, hoy: dt.date, err: list[str]) -> dict:
    nombre = str(b.get("nombre") or "").strip()
    donde = f"busquedas[{i}] ('{nombre or 'sin nombre'}')"
    if not nombre:
        err.append(f"busquedas[{i}]: falta 'nombre'")
    out = dict(b)
    out["nombre"] = nombre
    out["activa"] = bool(b.get("activa", True))
    for k in ("buscar_desde", "buscar_hasta"):
        v = b.get(k)
        try:
            out[k] = parse_fecha(v, hoy) if v not in (None, "") else None
        except Exception:
            out[k] = None
            err.append(f"{donde}: '{k}' no es una fecha válida (usá AAAA-MM-DD)")
    if out["buscar_desde"] and out["buscar_hasta"] and out["buscar_hasta"] < out["buscar_desde"]:
        err.append(f"{donde}: 'buscar_hasta' no puede ser anterior a 'buscar_desde'")
    if b.get("hora") not in (None, ""):
        try:
            out["hora"] = int(b["hora"])
            if not 0 <= out["hora"] <= 23:
                raise ValueError
        except (TypeError, ValueError):
            err.append(f"{donde}: 'hora' debe ser un número entre 0 y 23")
            out["hora"] = None
    else:
        out["hora"] = None
    out["tipo"] = b.get("tipo", "ida_vuelta")
    out["pasajeros"] = int(b.get("pasajeros", 1) or 1)
    out["clase"] = b.get("clase", "economy")
    out["origenes"] = [str(x).upper().strip() for x in _lista(b.get("origenes"))]
    out["destinos"] = [str(x).upper().strip() for x in _lista(b.get("destinos"))]

    if out["tipo"] not in TIPOS:
        err.append(f"{donde}: tipo debe ser 'ida_vuelta' o 'solo_ida'")
    if out["clase"] not in CLASES:
        err.append(f"{donde}: clase debe ser uno de {sorted(CLASES)}")
    if out["pasajeros"] < 1:
        err.append(f"{donde}: pasajeros debe ser >= 1")
    for campo in ("origenes", "destinos"):
        if not out[campo]:
            err.append(f"{donde}: falta '{campo}'")
        for c in out[campo]:
            if not _IATA.match(c):
                err.append(f"{donde}: '{c}' en {campo} no es un código IATA de 3 letras")

    # --- Exploración por país: primero una pasada rápida por todos los destinos, después detalle ---
    pais = str(b.get("destino_pais") or "").strip().upper()
    if pais and not re.fullmatch(r"[A-Z]{2}", pais):
        err.append(f"{donde}: destino_pais debe ser el código de 2 letras del país (ej: BR)")
    out["destino_pais"] = pais or None
    try:
        out["explorar_top"] = max(0, int(b.get("explorar_top", 3 if pais else 0) or 0))
    except (TypeError, ValueError):
        err.append(f"{donde}: explorar_top debe ser un número entero")
        out["explorar_top"] = 0
    if len(out["destinos"]) > 60:
        err.append(f"{donde}: máximo 60 destinos")

    # --- Estrategia de precios ---
    #   ida_vuelta: pasaje de ida y vuelta (como lo arma Google)
    #   solo_ida:   dos pasajes de solo ida (pueden ser de aerolíneas distintas) combinados
    #   mixta:      barre todo con solo ida y verifica ida y vuelta en las mejores fechas
    out["estrategia"] = str(b.get("estrategia") or "ida_vuelta")
    if out["estrategia"] not in ESTRATEGIAS:
        err.append(f"{donde}: estrategia debe ser una de {sorted(ESTRATEGIAS)}")
    # --- Equipaje (lo que llevás; los precios lo incluyen) ---
    eq = b.get("equipaje")
    if eq is None:
        eq = "despachado" if int(b.get("equipaje_despachado", 0) or 0) > 0 else "ninguno"
    if eq not in EQUIPAJES:
        err.append(f"{donde}: equipaje debe ser ninguno, mano o despachado")
        eq = "ninguno"
    out["equipaje"], out["nivel_equipaje"] = eq, EQUIPAJES[eq]
    # --- Escala armada por separado (pasajes separados vía un hub) ---
    out["escala_separada"] = bool(b.get("escala_separada", False))
    out["hubs"] = [str(x).upper().strip() for x in _lista(b.get("hubs"))]
    for c in out["hubs"]:
        if not _IATA.match(c):
            err.append(f"{donde}: '{c}' en hubs no es un código IATA de 3 letras")
    for campo, defecto, lo, hi in (("verificar_top", 8, 0, 50), ("escala_top", 4, 1, 20),
                                   ("conexion_min_horas", 4, 1, 24), ("conexion_max_horas", 24, 2, 48)):
        try:
            out[campo] = int(b.get(campo, defecto))
            if not lo <= out[campo] <= hi:
                raise ValueError
        except (TypeError, ValueError):
            err.append(f"{donde}: {campo} debe ser un número entre {lo} y {hi}")
            out[campo] = defecto
    if out["conexion_max_horas"] <= out["conexion_min_horas"]:
        err.append(f"{donde}: conexion_max_horas tiene que ser mayor que conexion_min_horas")

    me = b.get("max_escalas")
    if me is not None and (not isinstance(me, int) or not 0 <= me <= 3):
        err.append(f"{donde}: max_escalas debe ser 0, 1, 2, 3 o null")

    # --- Meses permitidos ---
    meses = [int(m) for m in _lista(b.get("meses_permitidos"))]
    if any(not 1 <= m <= 12 for m in meses):
        err.append(f"{donde}: meses_permitidos solo acepta números del 1 al 12")
    out["meses_permitidos"] = sorted(set(meses))

    # --- Fechas ---
    try:
        if b.get("fechas"):
            pares = []
            for f in b["fechas"]:
                ida = parse_fecha(f["ida"], hoy)
                vta = parse_fecha(f["vuelta"], hoy) if f.get("vuelta") else None
                if vta and vta < ida:
                    err.append(f"{donde}: en fechas, la vuelta {vta} es anterior a la ida {ida}")
                pares.append({"ida": ida, "vuelta": vta})
            out["fechas"] = pares
            out["_vuelta_hasta"] = parse_fecha(b["vuelta_hasta"], hoy) if b.get("vuelta_hasta") else None
        elif b.get("viajar_desde") or b.get("viajar_hasta"):
            # Rango de viaje: TODO el viaje (ida y vuelta) tiene que caer entre estas dos fechas.
            if not (b.get("viajar_desde") and b.get("viajar_hasta")):
                err.append(f"{donde}: viajar_desde y viajar_hasta van juntos")
            out["fechas"] = []
            desde = parse_fecha(b.get("viajar_desde") or b.get("viajar_hasta"), hoy)
            hasta = parse_fecha(b.get("viajar_hasta") or b.get("viajar_desde"), hoy)
            if hasta < desde:
                err.append(f"{donde}: viajar_hasta ({hasta}) es anterior a viajar_desde ({desde})")
            out["_desde"], out["_hasta"], out["_vuelta_hasta"] = desde, hasta, hasta
            try:
                dmin = min(lista_duraciones(b)) if out["tipo"] == "ida_vuelta" else 0
                if dmin > (hasta - desde).days:
                    err.append(f"{donde}: el rango {desde} a {hasta} tiene {(hasta - desde).days} días y el "
                               f"viaje dura como mínimo {dmin}: no entra ninguna combinación")
            except (ValueError, KeyError, TypeError):
                pass
        else:
            out["fechas"] = []
            desde = parse_fecha(b.get("salida_desde", "+15"), hoy)
            hasta = parse_fecha(b.get("salida_hasta", "+330"), hoy)
            if hasta < desde:
                err.append(f"{donde}: salida_hasta ({hasta}) es anterior a salida_desde ({desde})")
            out["_desde"], out["_hasta"] = desde, hasta
            out["_vuelta_hasta"] = parse_fecha(b["vuelta_hasta"], hoy) if b.get("vuelta_hasta") else None
    except (ValueError, KeyError, TypeError) as e:
        err.append(f"{donde}: fecha inválida ({e}). Usá AAAA-MM-DD o +N (días desde hoy)")

    try:
        durs = lista_duraciones(b)
        if out["tipo"] == "ida_vuelta" and (not durs or min(durs) < 0):
            err.append(f"{donde}: duracion_dias inválida")
    except (ValueError, KeyError, TypeError):
        err.append(f"{donde}: duracion_dias debe ser un número, una lista [7, 10] o {{min: 7, max: 10}}")

    for campo, defecto in (("paso_dias", 3), ("refinar_top", 6), ("tolerancia_dias", 2)):
        try:
            out[campo] = max(0, int(b.get(campo, defecto) or 0))
        except (TypeError, ValueError):
            err.append(f"{donde}: {campo} debe ser un número entero")
    out["paso_dias"] = max(1, out.get("paso_dias", 3))
    out["ahorro_minimo_pct"] = float(b.get("ahorro_minimo_pct", 5) or 0)

    alerta = b.get("alerta_precio_persona")
    if alerta is None and b.get("alerta_precio_total"):
        alerta = float(b["alerta_precio_total"]) / out["pasajeros"]
    out["alerta_precio_persona"] = float(alerta) if alerta else None

    emails = [str(e).strip() for e in _lista(b.get("emails"))]
    for e in emails:
        if not _EMAIL.match(e):
            err.append(f"{donde}: '{e}' no parece un email válido")
    out["emails"] = emails
    return out


def cargar_config(ruta: str | Path | None = None, hoy: dt.date | None = None) -> AppConfig:
    ruta = Path(ruta) if ruta else CONFIG_PATH
    if not ruta.exists():
        raise ConfigError(f"No existe el archivo de configuración: {ruta}")
    try:
        crudo = yaml.safe_load(ruta.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{ruta.name} tiene un error de formato YAML:\n{e}") from e

    hoy = hoy or dt.date.today()
    err: list[str] = []
    general = _validar_general(crudo.get("general") or {}, err)
    busquedas = [_validar_busqueda(b or {}, i, hoy, err) for i, b in enumerate(crudo.get("busquedas") or [])]
    for b in busquedas:
        b["_hora_propia"] = b["hora"] is not None
        if b["hora"] is None:
            b["hora"] = general["hora"]

    nombres = [b["nombre"].lower() for b in busquedas if b["nombre"]]
    for n in {n for n in nombres if nombres.count(n) > 1}:
        err.append(f"Hay más de una búsqueda llamada '{n}' (los nombres deben ser únicos)")

    if err:
        raise ConfigError(f"Errores en {ruta.name}:\n  - " + "\n  - ".join(err))
    return AppConfig(general=general, busquedas=busquedas, ruta=ruta)
