"""Motor de búsqueda: generación de combinaciones de fechas + proveedores de precios.

Proveedores:
  * fast_flights  -> scraping de Google Flights (gratis, sin límite, pero Google puede bloquear
                     IPs de datacenter). Rota perfiles de navegador (User-Agent + huella TLS
                     coherentes) y usa pausas aleatorias con descansos largos cada tanto.
  * serpapi       -> API de SerpApi sobre Google Flights (JSON estructurado, sin captchas;
                     plan gratuito con cupo mensual chico). Necesita SERPAPI_API_KEY.
  * auto          -> fast_flights y, si Google bloquea y hay SERPAPI_API_KEY, sigue con serpapi.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Iterable

from . import database as db
from . import estrategias
from .ritmo import Ritmo
from .config import lista_duraciones

log = logging.getLogger("vuelos")

Combo = tuple[dt.date, "dt.date | None"]


# ============================================================================
# 1) FECHAS: generación y filtros
# ============================================================================
def es_ida_vuelta(b: dict) -> bool:
    return b.get("tipo", "ida_vuelta") == "ida_vuelta"


class ReglasFechas:
    """Encapsula todas las restricciones de fechas de una búsqueda.

    - rango estricto de salida (salida_desde / salida_hasta), fechas fijas o relativas (+N)
    - meses_permitidos: [1, 2] => solo salidas en enero y febrero
    - vuelta_hasta: la vuelta no puede pasar de esta fecha (opcional)
    - nunca fechas pasadas (ida >= mañana) ni vuelta anterior a la ida
    """

    def __init__(self, b: dict, hoy: dt.date | None = None):
        self.b = b
        self.hoy = hoy or dt.date.today()
        self.manana = self.hoy + dt.timedelta(days=1)
        self.meses = set(b.get("meses_permitidos") or [])
        self.vuelta_hasta: dt.date | None = b.get("_vuelta_hasta")
        self.fijas = bool(b.get("fechas"))
        self.ida_vuelta = es_ida_vuelta(b)
        if self.fijas:
            self.desde = self.hasta = None
        else:
            self.desde = max(b["_desde"], self.manana)
            self.hasta = b["_hasta"]
        self.duraciones = lista_duraciones(b) if self.ida_vuelta else [None]
        self.paso = max(1, int(b.get("paso_dias", 3)))

    # --- validación de una combinación -------------------------------------
    def valida(self, ida: dt.date, vta: dt.date | None, en_ventana: bool = True) -> bool:
        if ida < self.manana:
            return False
        if vta is not None and vta < ida:
            return False
        if self.meses and ida.month not in self.meses:
            return False
        if self.vuelta_hasta and vta and vta > self.vuelta_hasta:
            return False
        if en_ventana and not self.fijas and not (self.desde <= ida <= self.hasta):
            return False
        return True

    def valida_iso(self, ida: str, vta: str | None) -> bool:
        """Para filas de la base: ¿esta combinación sigue dentro de lo que el usuario quiere HOY?

        Se usa para que reportes, mínimo histórico y alertas ignoren precios de fechas que ya
        pasaron o que quedaron fuera del rango (por ejemplo, después de acotar a enero-marzo).
        """
        try:
            i = dt.date.fromisoformat(ida[:10])
            v = dt.date.fromisoformat(vta[:10]) if vta else None
        except (TypeError, ValueError):
            return False
        return self.valida(i, v, en_ventana=not self.fijas)

    def filtrar(self, combos: Iterable[Combo], ya: set | None = None) -> list[Combo]:
        out, vistos = [], set(ya or ())
        for c in combos:
            if c not in vistos and self.valida(*c):
                vistos.add(c)
                out.append(c)
        return out

    # --- ¿combinación pedida o variante flexible? ---------------------------
    def es_base(self, ida: dt.date, vta: dt.date | None) -> bool:
        if self.fijas:
            return any(f["ida"] == ida and f["vuelta"] == vta for f in self.b["fechas"])
        if not self.ida_vuelta:
            return True
        return vta is not None and (vta - ida).days in self.duraciones

    # --- pasadas -----------------------------------------------------------
    def iniciales(self) -> list[Combo]:
        if self.fijas:
            pares = [(f["ida"], f["vuelta"] if self.ida_vuelta else None) for f in self.b["fechas"]]
            descartadas = [p for p in pares if not self.valida(*p)]
            for ida, vta in descartadas:
                log.info(f"  (descarto {ida}{' / ' + str(vta) if vta else ''}: fecha pasada o fuera de los filtros)")
            return self.filtrar(pares)
        out, d = [], self.desde
        while d <= self.hasta:
            if not self.meses or d.month in self.meses:
                for dur in self.duraciones:
                    out.append((d, d + dt.timedelta(days=dur) if dur is not None else None))
            d += dt.timedelta(days=self.paso)
        return self.filtrar(out)

    def refinamiento(self, mejores: list[Combo], ya: set) -> list[Combo]:
        """Salidas día por día alrededor de las más baratas (con la duración configurada)."""
        if self.fijas or self.paso == 1:
            return []
        out = []
        for ida, _ in mejores:
            for delta in range(-(self.paso - 1), self.paso):
                d = ida + dt.timedelta(days=delta)
                for dur in self.duraciones:
                    out.append((d, d + dt.timedelta(days=dur) if dur is not None else None))
        return self.filtrar(out, ya)

    def flexibles(self, mejores: list[Combo], ya: set) -> list[Combo]:
        """Alrededor de las más baratas: salir ±1 día y volver con ±tolerancia días de diferencia."""
        tol = int(self.b.get("tolerancia_dias", 2) or 0)
        if tol <= 0 or not self.ida_vuelta:
            return []
        out = []
        for ida, vta in mejores:
            dur = (vta - ida).days
            for corre in (-1, 0, 1):
                for dd in range(-tol, tol + 1):
                    i = ida + dt.timedelta(days=corre)
                    out.append((i, i + dt.timedelta(days=dur + dd)))
        # en modo fechas fijas las variantes pueden salir de la "ventana" (no existe), pero no del resto de filtros
        vistos, res = set(ya), []
        for c in out:
            if c not in vistos and c[1] > c[0] and self.valida(*c, en_ventana=not self.fijas):
                vistos.add(c)
                res.append(c)
        return res


def elegir_top(precios: dict, n: int) -> list[Combo]:
    """Las n combinaciones más baratas, evitando elegir fechas pegadas entre sí."""
    elegidas: list[Combo] = []
    for c in sorted(precios, key=precios.get):
        if all(abs((c[0] - e[0]).days) > 1 for e in elegidas):
            elegidas.append(c)
        if len(elegidas) >= n:
            break
    return elegidas


# ============================================================================
# 2) PROVEEDORES
# ============================================================================
class Bloqueado(Exception):
    """Google devolvió captcha / página sin datos."""


class SinCupo(Exception):
    """El proveedor no puede seguir en esta corrida (sin créditos, API key inválida...)."""


def params_google(q, g: dict) -> dict:
    """Parámetros de la consulta a Google Flights, con el país fijo (gl): si no, Google cotiza según el país
    de la IP y desde la nube (EE.UU.) los precios no coinciden con los que se ven desde Argentina."""
    p = dict(q.params())
    if g.get("pais"):
        p["gl"] = g["pais"]
    return p


def link_google_flights(b: dict, g: dict, origen: str, destino: str, ida: dt.date, vuelta: dt.date | None) -> str:
    try:
        q = _armar_query(b, g, origen, destino, ida, vuelta)
        return "https://www.google.com/travel/flights/search?" + urllib.parse.urlencode(params_google(q, g))
    except Exception:
        q = f"Flights from {origen} to {destino} on {ida}" + (f" through {vuelta}" if vuelta else " one way")
        return "https://www.google.com/travel/flights?" + urllib.parse.urlencode({"q": q, "curr": g.get("moneda", "USD")})


def nivel_equipaje(b: dict) -> int:
    """0 = sin equipaje (solo artículo personal), 1 = valija de mano, 2 = valija despachada."""
    if "nivel_equipaje" in b:
        return int(b["nivel_equipaje"] or 0)
    return 2 if int(b.get("equipaje_despachado", 0) or 0) > 0 else 0


def _armar_query(b: dict, g: dict, origen: str, destino: str, ida: dt.date, vuelta: dt.date | None):
    from fast_flights import FlightQuery, Passengers, create_query

    max_dur = b.get("max_duracion_horas")
    extra = {"max_duration_minutes": int(max_dur * 60)} if max_dur else {}
    nivel = nivel_equipaje(b)
    tramos = [FlightQuery(date=ida.isoformat(), from_airport=origen, to_airport=destino, **extra)]
    if vuelta:
        tramos.append(FlightQuery(date=vuelta.isoformat(), from_airport=destino, to_airport=origen, **extra))
    return create_query(
        flights=tramos,
        trip="round-trip" if vuelta else "one-way",
        seat=b.get("clase", "economy"),
        passengers=Passengers(adults=int(b.get("pasajeros", 1))),
        language=g.get("idioma", "es"),
        currency=g.get("moneda", "USD"),
        max_stops=b.get("max_escalas"),
        carry_on_bags=1 if nivel >= 1 else 0,
        checked_bags=max(1, int(b.get("equipaje_despachado", 0) or 0)) if nivel >= 2 else 0,
        hide_separate_and_self_transfer=bool(b.get("ocultar_autotransferencia", False)),
    )


def _dt_js(fecha, hora) -> dt.datetime | None:
    try:
        h = [*(hora or []), None, None]
        return dt.datetime(int(fecha[0]), int(fecha[1]), int(fecha[2]), h[0] or 0, h[1] or 0)
    except Exception:
        return None


def _opcion(precio: float, aerolineas: str, segs: list[dict]) -> dict:
    """segs: [{de, a, sal: datetime, lleg: datetime, dur: min}] del tramo de ida."""
    total = sum(x["dur"] or 0 for x in segs)
    for a, b in zip(segs, segs[1:]):   # escalas: mismo aeropuerto => misma hora local
        if a["lleg"] and b["sal"]:
            total += max(0, int((b["sal"] - a["lleg"]).total_seconds() // 60))
    return {
        "precio": float(precio),
        "aerolineas": aerolineas,
        "escalas": max(0, len(segs) - 1),
        "duracion_min": total,
        "salida": segs[0]["sal"].strftime("%Y-%m-%d %H:%M") if segs[0]["sal"] else "",
        "llegada": segs[-1]["lleg"].strftime("%Y-%m-%d %H:%M") if segs[-1]["lleg"] else "",
        "ruta": " → ".join([segs[0]["de"]] + [x["a"] for x in segs]),
    }


def _dedup_ordenar(opciones: list[dict]) -> list[dict]:
    vistos, out = set(), []
    for op in opciones:
        k = (op["precio"], op["aerolineas"], op["salida"], op["ruta"])
        if k not in vistos:
            vistos.add(k)
            out.append(op)
    out.sort(key=lambda o: (o["precio"], o["escalas"], o["duracion_min"] or 10**6))
    return out


def parsear_html_google(html_txt: str, info: dict | None = None) -> list[dict]:
    """Extrae las opciones (precio + itinerario de ida) del HTML de Google Flights.
    Si se pasa `info`, deja ahí cuántas opciones venían y cuántas no se pudieron leer (diagnóstico)."""
    from selectolax.lexbor import LexborHTMLParser

    script = LexborHTMLParser(html_txt).css_first(r"script.ds\:1")
    if script is None:
        raise Bloqueado("Google no devolvió resultados (posible captcha/bloqueo)")
    data = script.text().split("data:", 1)[1].rsplit(",", 1)[0]
    if data.endswith("errorHasStatus: true"):
        return []
    payload = json.loads(data)

    crudos = []
    for idx in (2, 3):  # 2 = "mejores vuelos", 3 = "otros vuelos"
        try:
            sec = payload[idx][0]
            if isinstance(sec, list):
                crudos.extend(sec)
        except (IndexError, TypeError):
            pass

    opciones, sin_precio, fallidos = [], 0, 0
    for k in crudos:
        try:
            fl, precio = k[0], k[1][0][1]
            if not precio:
                sin_precio += 1
                continue
            segs = [{"de": s[3], "a": s[6], "sal": _dt_js(s[20], s[8]), "lleg": _dt_js(s[21], s[10]),
                     "dur": s[11] or 0} for s in fl[2]]
            aer = ", ".join(fl[1]) if isinstance(fl[1], list) else str(fl[1])
            opciones.append(_opcion(precio, aer, segs))
        except Exception:
            fallidos += 1
            continue
    if info is not None:
        info.update(crudos=len(crudos), sin_precio=sin_precio, fallidos=fallidos)
    return _dedup_ordenar(opciones)


_aviso_formato = threading.Lock()
_avisado_formato = False


_muestra_guardada = False


def _guardar_muestra(html_txt: str, etiqueta: str) -> None:
    """Guarda UNA respuesta real de Google por corrida (data/muestra_google.html, va a la rama "datos").
    Sirve para analizar datos extra que trae la página (por ejemplo el "precio habitual" de Google)."""
    global _muestra_guardada
    if _muestra_guardada:
        return
    _muestra_guardada = True
    try:
        from .paths import DATA_DIR
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        (DATA_DIR / "muestra_google.html").write_text(f"<!-- {etiqueta} -->\n" + html_txt, encoding="utf-8")
    except OSError:
        pass


def _guardar_diagnostico(html_txt: str, info: dict, etiqueta: str) -> None:
    """Guarda el último HTML que vino 'sin vuelos' para poder revisarlo, y avisa una sola vez
    si Google mandó opciones que el parser no supo leer (señal de que cambió el formato)."""
    global _avisado_formato
    try:
        from .paths import LOGS_DIR
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        (LOGS_DIR / "ultimo_sin_vuelos.html").write_text(f"<!-- {etiqueta} · {info} -->\n" + html_txt,
                                                         encoding="utf-8")
    except OSError:
        pass
    if info.get("fallidos") or info.get("sin_precio"):
        with _aviso_formato:
            if _avisado_formato:
                return
            _avisado_formato = True
        log.info(f"  ⚠ {etiqueta}: Google devolvió {info.get('crudos')} opciones pero ninguna se pudo usar "
                 f"({info.get('sin_precio')} sin precio, {info.get('fallidos')} con formato desconocido). "
                 f"Guardé la respuesta en logs/ultimo_sin_vuelos.html")


class Proveedor:
    nombre = "base"
    requiere_pausa = True   # scraping: sí. APIs pagas por consulta: no hace falta esperar

    def buscar(self, b: dict, g: dict, origen: str, destino: str,
               ida: dt.date, vuelta: dt.date | None) -> tuple[list[dict], str]:
        raise NotImplementedError


class FastFlightsProveedor(Proveedor):
    """Scraping de Google Flights con rotación de perfiles de navegador."""

    nombre = "fast_flights"
    # (impersonate, os): cada perfil manda User-Agent, headers y huella TLS coherentes entre sí.
    # Solo navegadores Chromium: Google les sirve el mismo HTML que parsea parsear_html_google()
    # (con Firefox/Safari a veces cambia el marcado y se perderían resultados).
    PERFILES = [
        ("chrome_146", "windows"), ("chrome_146", "macos"), ("chrome_145", "windows"), ("chrome_145", "macos"),
        ("chrome_144", "windows"), ("chrome_144", "linux"), ("edge_145", "windows"), ("edge_144", "windows"),
        ("opera_126", "windows"), ("opera_126", "macos"),
    ]
    ROTAR_CADA = (15, 35)   # cambia de perfil cada N consultas (aleatorio en el rango)

    def __init__(self, proxy: str | None = None):
        self.proxy = proxy or os.environ.get("VUELOS_PROXY") or None
        self._local = threading.local()   # un "navegador" (cliente + cookies) por hilo
        self.al_reintentar: Callable[[], None] | None = None   # avisa al control de velocidad

    @property
    def _cliente(self):
        return getattr(self._local, "cliente", None)

    @_cliente.setter
    def _cliente(self, valor):
        self._local.cliente = valor

    @property
    def _restantes(self) -> int:
        return getattr(self._local, "restantes", 0)

    @_restantes.setter
    def _restantes(self, valor: int):
        self._local.restantes = valor

    def _nuevo_cliente(self):
        from primp import Client

        perfiles = self.PERFILES[:]
        random.shuffle(perfiles)
        for imp, so in perfiles:
            try:
                self._cliente = Client(impersonate=imp, impersonate_os=so, referer=True,
                                       proxy=self.proxy, cookie_store=True, timeout=40)
                break
            except Exception:   # perfil no soportado por la versión instalada de primp
                continue
        else:
            self._cliente = Client(impersonate="chrome_145", referer=True, proxy=self.proxy, cookie_store=True)
        self._restantes = random.randint(*self.ROTAR_CADA)

    def _cliente_actual(self, forzar_nuevo: bool = False):
        if forzar_nuevo or self._cliente is None or self._restantes <= 0:
            self._nuevo_cliente()
        self._restantes -= 1
        return self._cliente

    def buscar(self, b, g, origen, destino, ida, vuelta):
        from fast_flights.fetcher import URL

        q = _armar_query(b, g, origen, destino, ida, vuelta)
        link = "https://www.google.com/travel/flights/search?" + urllib.parse.urlencode(params_google(q, g))
        ultimo: Exception | None = None
        for intento in range(3):
            try:
                html = self._cliente_actual(forzar_nuevo=intento > 0).get(URL, params=params_google(q, g)).text
                info: dict = {}
                ops = parsear_html_google(html, info)
                if not ops:
                    _guardar_diagnostico(html, info, f"{origen}-{destino} {ida}" + (f" / {vuelta}" if vuelta else ""))
                elif vuelta:
                    _guardar_muestra(html, f"{origen}-{destino} {ida} / {vuelta}")
                return ops, link
            except Bloqueado as e:
                ultimo = e
                if self.al_reintentar:
                    self.al_reintentar()
                time.sleep(20 * (intento + 1) + random.uniform(0, 10))
            except Exception as e:   # red, timeout, formato
                ultimo = e
                if self.al_reintentar:
                    self.al_reintentar()
                time.sleep(5 * (intento + 1))
        raise ultimo  # type: ignore[misc]


class SerpApiProveedor(Proveedor):
    """Google Flights vía SerpApi (https://serpapi.com/google-flights-api)."""

    nombre = "serpapi"
    requiere_pausa = False
    URL = "https://serpapi.com/search.json"
    CLASES = {"economy": 1, "premium-economy": 2, "business": 3, "first": 4}

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("SERPAPI_API_KEY", "")
        if not self.api_key:
            raise SinCupo("Falta la variable de entorno SERPAPI_API_KEY")

    def _params(self, b, g, origen, destino, ida, vuelta) -> dict:
        p = {
            "engine": "google_flights", "api_key": self.api_key,
            "departure_id": origen, "arrival_id": destino, "outbound_date": ida.isoformat(),
            "type": 1 if vuelta else 2, "currency": g.get("moneda", "USD"), "hl": g.get("idioma", "es"),
            "gl": g.get("pais", "AR").lower(),
            "adults": int(b.get("pasajeros", 1)), "travel_class": self.CLASES.get(b.get("clase", "economy"), 1),
        }
        if vuelta:
            p["return_date"] = vuelta.isoformat()
        me = b.get("max_escalas")
        if me is not None:
            p["stops"] = min(3, int(me) + 1)   # 1 = directo, 2 = hasta 1 escala, 3 = hasta 2 escalas
        if b.get("max_duracion_horas"):
            p["max_duration"] = int(b["max_duracion_horas"] * 60)
        if nivel_equipaje(b) >= 2:   # SerpApi solo contempla valijas despachadas
            p["bags"] = max(1, int(b.get("equipaje_despachado", 0) or 0))
        return p

    @staticmethod
    def _hora(s: str | None) -> dt.datetime | None:
        try:
            return dt.datetime.strptime(s, "%Y-%m-%d %H:%M") if s else None
        except ValueError:
            return None

    def buscar(self, b, g, origen, destino, ida, vuelta):
        url = self.URL + "?" + urllib.parse.urlencode(self._params(b, g, origen, destino, ida, vuelta))
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            cuerpo = e.read().decode("utf-8", "ignore")
            if e.code in (401, 403, 429):
                raise SinCupo(f"SerpApi {e.code}: {cuerpo[:200]}") from e
            raise RuntimeError(f"SerpApi {e.code}: {cuerpo[:200]}") from e

        err = data.get("error")
        if err:
            if "hasn't returned any results" in err or "no results" in err.lower():
                return [], link_google_flights(b, g, origen, destino, ida, vuelta)
            if any(x in err.lower() for x in ("run out", "invalid api key", "plan", "limit")):
                raise SinCupo(f"SerpApi: {err}")
            raise RuntimeError(f"SerpApi: {err}")

        link = (data.get("search_metadata") or {}).get("google_flights_url") \
            or link_google_flights(b, g, origen, destino, ida, vuelta)
        opciones = []
        for grupo in (data.get("best_flights") or []) + (data.get("other_flights") or []):
            try:
                if not grupo.get("price"):
                    continue
                segs = [{"de": s["departure_airport"]["id"], "a": s["arrival_airport"]["id"],
                         "sal": self._hora(s["departure_airport"].get("time")),
                         "lleg": self._hora(s["arrival_airport"].get("time")),
                         "dur": s.get("duration") or 0} for s in grupo["flights"]]
                aer = ", ".join(dict.fromkeys(s.get("airline", "") for s in grupo["flights"] if s.get("airline")))
                op = _opcion(grupo["price"], aer, segs)
                if grupo.get("total_duration"):
                    op["duracion_min"] = int(grupo["total_duration"])
                opciones.append(op)
            except (KeyError, TypeError):
                continue
        return _dedup_ordenar(opciones), link


class ProveedorEnCadena(Proveedor):
    """Usa el primer proveedor; si queda bloqueado/sin cupo, pasa al siguiente para el resto de la corrida."""

    def __init__(self, proveedores: list[Proveedor], umbral_bloqueos: int = 3):
        self.proveedores = proveedores
        self.i = 0
        self.umbral = umbral_bloqueos
        self.bloqueos_seguidos = 0

    @property
    def nombre(self) -> str:  # type: ignore[override]
        return "+".join(p.nombre for p in self.proveedores[: self.i + 1])

    @property
    def requiere_pausa(self) -> bool:  # type: ignore[override]
        return self.proveedores[self.i].requiere_pausa

    def _siguiente(self, motivo: str) -> bool:
        if self.i + 1 < len(self.proveedores):
            self.i += 1
            self.bloqueos_seguidos = 0
            log.info(f"  ⇢ {motivo}. Sigo con el proveedor '{self.proveedores[self.i].nombre}'.")
            return True
        return False

    def buscar(self, b, g, origen, destino, ida, vuelta):
        while True:
            p = self.proveedores[self.i]
            try:
                res = p.buscar(b, g, origen, destino, ida, vuelta)
                self.bloqueos_seguidos = 0
                return res
            except SinCupo as e:
                if not self._siguiente(f"{p.nombre} sin cupo ({e})"):
                    raise
            except Bloqueado:
                self.bloqueos_seguidos += 1
                if self.bloqueos_seguidos >= self.umbral and self._siguiente(f"{p.nombre} bloqueado por Google"):
                    continue
                raise


def crear_proveedor(nombre: str) -> Proveedor:
    nombre = (nombre or "auto").lower()
    if nombre == "fast_flights":
        return FastFlightsProveedor()
    if nombre == "serpapi":
        return SerpApiProveedor()
    cadena: list[Proveedor] = [FastFlightsProveedor()]
    if os.environ.get("SERPAPI_API_KEY"):
        cadena.append(SerpApiProveedor())
    return ProveedorEnCadena(cadena) if len(cadena) > 1 else cadena[0]


# ============================================================================
# 3) ORQUESTACIÓN DE UNA BÚSQUEDA
# ============================================================================
def _hubs(b: dict, destinos: list[str]) -> list[str]:
    hubs = b.get("hubs") or estrategias.hubs_sugeridos(destinos, b["origenes"])
    return [h for h in hubs if h not in destinos and h not in b["origenes"]]


def usa_escala_separada(b: dict) -> bool:
    return bool(b.get("escala_separada")) and es_ida_vuelta(b) and b.get("max_escalas") != 0


def estimar(b: dict, hoy: dt.date | None = None, g: dict | None = None) -> dict:
    """Consultas previstas por etapa. Las pasadas de refinamiento (solo en estrategia ida_vuelta)
    dependen de los resultados y suman ~10-30%."""
    reglas = ReglasFechas(b, hoy)
    combos = reglas.iniciales()
    n_orig, destinos = len(b["origenes"]), list(b["destinos"])
    estrategia = b.get("estrategia", "ida_vuelta") if es_ida_vuelta(b) else "ida_vuelta"
    out = {"combinaciones": len(combos), "rutas": n_orig * len(destinos), "exploracion": 0, "tramos": 0,
           "verificacion": 0, "escala": 0, "equipaje": 0, "estrategia": estrategia,
           "destinos_detalle": len(destinos), "primera_ida": combos[0][0].isoformat() if combos else None,
           "ultima_ida": combos[-1][0].isoformat() if combos else None}
    if usa_exploracion(b):
        out["exploracion"] = len(destinos) * len(muestras_exploracion(reglas, combos))
        out["destinos_detalle"] = int(b["explorar_top"])
    nd = out["destinos_detalle"]
    if estrategia in ("ida_vuelta", "ambas"):
        # 1ra pasada + estimación de las pasadas 2 (día por día) y 3 (± días) alrededor de las mejores fechas
        n_top = min(int(b.get("refinar_top", 6)), len(combos))
        paso, tol = max(1, int(b.get("paso_dias", 3))), int(b.get("tolerancia_dias", 0) or 0)
        durs = len(lista_duraciones(b)) if es_ida_vuelta(b) else 1
        p2 = 0 if reglas.fijas or paso == 1 else n_top * 2 * (paso - 1) * durs
        p3 = int(n_top * 3 * (2 * tol + 1) * 0.75) if tol > 0 and es_ida_vuelta(b) else 0
        out["refinamiento"] = (p2 + p3) * n_orig * nd
        out["detalle"] = len(combos) * n_orig * nd + out["refinamiento"]
    if estrategia != "ida_vuelta":
        plan = estrategias.plan_tramos(reglas, b) if combos else None
        out["dias_ida"] = len(plan.idas) if plan else 0
        out["dias_vuelta"] = len(plan.vueltas) if plan else 0
        out["tramos"] = (out["dias_ida"] + out["dias_vuelta"]) * n_orig * nd
        if estrategia == "mixta" and combos:
            out["verificacion"] = min(int(b.get("verificar_top", 8)), len(combos) * nd) * n_orig
        out["detalle"] = out.get("detalle", 0) + out["tramos"] + out["verificacion"]
    if usa_escala_separada(b) and combos:
        out["hubs"] = _hubs(b, destinos[:1])
        out["escala"] = min(int(b.get("escala_top", 4)), len(combos)) * len(out["hubs"]) * (3 * n_orig + 3)
    niveles = list(b.get("niveles_equipaje") or [int(b.get("nivel_equipaje", 0) or 0)])
    out["niveles_equipaje"] = len(niveles)
    if int(b.get("nivel_equipaje", 0) or 0) > 0 and 0 not in niveles and combos:
        out["equipaje"] = int(b.get("verificar_top", 8)) * (1 if estrategia == "ida_vuelta" else 2)
    # horarios de la vuelta de las mejores opciones (una consulta por fecha de vuelta y origen distinta)
    n_hv = int(b.get("horarios_vuelta_top", 40) or 0) if es_ida_vuelta(b) and estrategia != "solo_ida" else 0
    out["horarios_vuelta"] = min(n_hv, len({v for _, v in combos}) * n_orig * nd) if combos and n_hv else 0
    # con varios equipajes se repite todo (menos la exploración) una vez por equipaje
    k = len(niveles)
    out["consultas"] = out["exploracion"] + k * (out["detalle"] + out["escala"] + out["horarios_vuelta"]) + out["equipaje"]
    ritmo = Ritmo.desde_config(g or {}, activo=(g or {}).get("proveedor", "auto") != "serpapi")
    out["segundos"] = round(out["consultas"] * ritmo.segundos_por_consulta())
    return out


def estimar_consultas(b: dict, hoy: dt.date | None = None) -> int:
    return estimar(b, hoy)["consultas"]


class _Parar(Exception):
    """Cortar la búsqueda: sin cupo, límite de consultas o demasiados errores seguidos."""


class Corrida:
    """Una corrida de una búsqueda: consultas, estrategias y guardado en la base."""

    def __init__(self, con, b: dict, g: dict, presupuesto: list[int], proveedor: Proveedor,
                 dormir: Callable[[float], None] = time.sleep):
        self.con, self.b, self.g, self.presupuesto, self.proveedor, self.dormir = con, b, g, presupuesto, proveedor, dormir
        self.nombre = b["nombre"]
        self.reglas = ReglasFechas(b)
        self.ahora = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        self.pax = int(b.get("pasajeros", 1))
        self.moneda = g.get("moneda", "USD")
        self.nivel = int(b.get("nivel_equipaje", 0) or 0)
        self.consultas = self.errores = self.seguidos = 0
        self.ritmo = Ritmo.desde_config(g, activo=proveedor.requiere_pausa, dormir=dormir)
        self._lock = threading.Lock()
        self.parar = False
        for p in getattr(proveedor, "proveedores", [proveedor]):
            if hasattr(p, "al_reintentar"):
                p.al_reintentar = self.ritmo.problema
        self.ya: set = set()
        self.precio_base: dict = {}      # (ida, vuelta) -> precio (duración pedida)
        self.precio_flex: dict = {}      # (ida, vuelta) -> precio (días de diferencia)
        self.mejores: dict = {}          # (ida, vuelta, destino) -> (precio, es_base, opcion, origen)
        self.cache_tramos: dict = {}     # (origen, destino, fecha, nivel) -> [tramos]
        self.destinos_detalle = list(b["destinos"])
        self._hubo_datos = False
        self.corrida_id = db.iniciar_corrida(con, self.nombre, proveedor.nombre)

    # ------------------------------------------------------------------ consultas
    def _consulta(self, tarea: tuple):
        """Una consulta (puede correr en un hilo). tarea = (origen, destino, ida, vuelta, nivel).
        Devuelve (ops, link) o None. Si hay que cortar la corrida, marca self.parar."""
        origen, destino, ida, vta, nivel = tarea
        with self._lock:
            if self.parar:
                return None
            if self.presupuesto[0] <= 0:
                log.info("  Límite de consultas por corrida alcanzado (subí max_consultas_por_corrida).")
                self.parar = True
                return None
            self.presupuesto[0] -= 1
            self.consultas += 1
        self.ritmo.esperar_turno()
        dias = f" ({(vta - ida).days} días)" if vta else ""
        etiqueta = f"{origen}-{destino} {ida}" + (f" / {vta}" if vta else " solo ida") + dias
        if nivel != self.nivel:
            etiqueta += " [sin equipaje]" if nivel == 0 else f" [equipaje {nivel}]"
        bq = self.b if nivel == self.b.get("nivel_equipaje", 0) else {**self.b, "nivel_equipaje": nivel}
        try:
            ops, link = self.proveedor.buscar(bq, self.g, origen, destino, ida, vta)
        except SinCupo as e:
            with self._lock:
                self.errores += 1
                self.parar = True
                self.presupuesto[0] = 0
            log.info(f"  ✗ Proveedor sin cupo: {e}. Corto la corrida.")
            return None
        except Exception as e:
            self.ritmo.problema()
            with self._lock:
                self.errores += 1
                self.seguidos += 1
                cortar = self.seguidos >= 6
                if cortar:
                    self.parar = True
            log.info(f"  ✗ {etiqueta}: {e}")
            if cortar:
                log.info("  Demasiados errores seguidos (Google puede estar bloqueando). Corto esta búsqueda.")
            return None
        with self._lock:
            self.seguidos = 0
        self.ritmo.ok()
        if not ops:
            log.info(f"  · {etiqueta}: sin vuelos")
            return None
        log.info(f"  ✓ {etiqueta}: {self.moneda} {ops[0]['precio'] / self.pax:,.0f} x persona  ({ops[0]['aerolineas']})")
        return ops, link

    def lote(self, tareas: list[tuple]) -> dict:
        """Ejecuta varias consultas (en paralelo si el ritmo lo permite). {tarea: (ops, link) | None}.
        Las tareas repetidas se consultan una sola vez. No lanza _Parar: el que llama guarda lo obtenido
        y después llama a self.seguir()."""
        unicas = list(dict.fromkeys(tareas))
        if self.ritmo.concurrencia <= 1 or len(unicas) <= 1:
            return {t: self._consulta(t) for t in unicas}
        with ThreadPoolExecutor(max_workers=self.ritmo.concurrencia, thread_name_prefix="vuelos") as ex:
            return dict(zip(unicas, ex.map(self._consulta, unicas)))

    def seguir(self) -> None:
        if self.parar:
            raise _Parar

    def consultar(self, origen: str, destino: str, ida: dt.date, vta: dt.date | None, nivel: int | None = None):
        """Una consulta suelta. Devuelve (ops, link) o None."""
        t = (origen, destino, ida, vta, self.nivel if nivel is None else nivel)
        r = self.lote([t])[t]
        self.seguir()
        return r

    def precargar_tramos(self, claves: list[tuple]) -> None:
        """Consulta en paralelo pasajes de solo ida que todavía no están en caché.
        claves = [(origen, destino, fecha, nivel)]."""
        faltan = [k for k in dict.fromkeys(claves) if k not in self.cache_tramos]
        if not faltan:
            return
        res = self.lote([(o, d, f, None, n) for o, d, f, n in faltan])
        for (o, d, f, n) in faltan:
            r = res.get((o, d, f, None, n))
            if r is not None or not self.parar:
                self.cache_tramos[(o, d, f, n)] = [estrategias.tramo("", o, d, f, op, r[1]) for op in r[0]] if r else []
        self.seguir()

    def tramos(self, origen: str, destino: str, fecha: dt.date, sentido: str, nivel: int | None = None) -> list[dict]:
        """Opciones de un pasaje de solo ida (con caché dentro de la corrida).
        sentido = "ida" o "vuelta": a qué parte del viaje pertenece el pasaje."""
        nivel = self.nivel if nivel is None else nivel
        k = (origen, destino, fecha, nivel)
        if k not in self.cache_tramos:
            self.precargar_tramos([k])
        return [{**t, "sentido": sentido} for t in self.cache_tramos.get(k, [])]

    # ------------------------------------------------------------------ guardado
    def guardar(self, origen: str, destino: str, ida: dt.date, vta: dt.date | None, ops: list[dict], link: str,
                es_base: bool, nivel: int | None = None, commit: bool = True) -> None:
        nivel = self.nivel if nivel is None else nivel
        db.guardar_opciones(self.con, self.corrida_id, self.nombre, self.ahora, origen, destino, ida, vta,
                            ops, self.moneda, self.pax, link, flexible=not es_base, equipaje=nivel, commit=commit)
        if nivel != self.nivel or not ops:
            return
        p = ops[0]["precio"]
        d = self.precio_base if es_base else self.precio_flex
        if (ida, vta) not in d or p < d[(ida, vta)]:
            d[(ida, vta)] = p
        k = (ida, vta, destino)
        if k not in self.mejores or p < self.mejores[k][0]:
            self.mejores[k] = (p, es_base, ops[0], origen)

    def top(self, n: int, solo_base: bool = True) -> list[tuple]:
        filas = [(k, v) for k, v in self.mejores.items() if v[1] or not solo_base]
        filas.sort(key=lambda kv: kv[1][0])
        return filas[:n]

    # ------------------------------------------------------------------ estrategia: ida y vuelta
    def ejecutar(self, lista: list[Combo], destinos: list[str] | None = None) -> None:
        tareas = [(o, d, ida, vta, self.nivel) for ida, vta in lista
                  for o in self.b["origenes"] for d in destinos or self.destinos_detalle]
        res = self.lote(tareas)
        for t in tareas:   # se guarda en el orden original, en el hilo principal
            o, d, ida, vta, _ = t
            self.ya.add((ida, vta))
            r = res.get(t)
            if r:
                self.guardar(o, d, ida, vta, r[0][:3], r[1], self.reglas.es_base(ida, vta))
        self.seguir()

    def pasadas_ida_vuelta(self, iniciales: list[Combo]) -> None:
        b, n_top = self.b, int(self.b.get("refinar_top", 6))
        log.info(f"  1ra pasada: {len(iniciales)} combinaciones de fechas "
                 f"(~{len(iniciales) * len(b['origenes']) * len(self.destinos_detalle)} consultas)")
        self.ejecutar(iniciales)
        if self.precio_base:
            extra = self.reglas.refinamiento(elegir_top(self.precio_base, n_top), self.ya)
            if extra:
                log.info(f"  2da pasada: {len(extra)} salidas día por día alrededor de las más baratas")
                self.ejecutar(extra)
        if self.precio_base:
            top = list(self.precio_base) if self.reglas.fijas else elegir_top(self.precio_base, n_top)
            flex = self.reglas.flexibles(top, self.ya)
            if flex:
                log.info(f"  3ra pasada: {len(flex)} combinaciones con días de diferencia (±{b.get('tolerancia_dias', 2)})")
                self.ejecutar(flex)

    # ------------------------------------------------------------------ horarios de la vuelta
    def horarios_vuelta(self) -> None:
        """El ida y vuelta de Google trae solo los horarios de la ida (la vuelta se elige en un segundo paso).
        Para las mejores opciones consulta el solo ida de regreso y guarda sus horarios: el reporte muestra el
        vuelo de vuelta de la misma aerolínea, que es el que Google suele combinar en ese precio."""
        n = int(self.b.get("horarios_vuelta_top", 40) or 0)
        if n <= 0:
            return
        self.con.commit()
        filas = [f for f in db.filas_corrida(self.con, self.corrida_id)
                 if f["tipo"] == "ida_vuelta" and f["vuelta"] and int(f["equipaje"] or 0) == self.nivel]
        claves: list[tuple] = []
        for flexible, tope in ((0, n), (1, max(1, n // 2))):
            vistos: set = set()
            for f in filas:   # vienen ordenadas por precio por persona
                if bool(f["flexible"]) != bool(flexible):
                    continue
                vistos.add((f["ida"], f["vuelta"], f["origen"], f["destino"]))
                if len(vistos) > tope:
                    break
                claves.append((f["destino"], f["origen"], dt.date.fromisoformat(f["vuelta"])))
        claves = list(dict.fromkeys(claves))
        if not claves:
            return
        log.info(f"  Horarios de vuelta: {len(claves)} consultas de solo ida para las mejores opciones")
        tareas = [(o, d, fecha, None, self.nivel) for o, d, fecha in claves]
        res = self.lote(tareas)
        for t in tareas:
            r = res.get(t)
            if r:
                db.guardar_vueltas(self.con, self.corrida_id, t[0], t[1], t[2], r[0][:8])
        self.seguir()

    # ------------------------------------------------------------------ estrategia: dos solo ida
    def solo_ida(self) -> None:
        b = self.b
        plan = estrategias.plan_tramos(self.reglas, b)
        n = (len(plan.idas) + len(plan.vueltas)) * len(b["origenes"]) * len(self.destinos_detalle)
        log.info(f"  Pasajes de solo ida: {len(plan.idas)} días de ida + {len(plan.vueltas)} de vuelta "
                 f"(~{n} consultas; cubre todas las duraciones de {min(plan.base | plan.flex)} a "
                 f"{max(plan.base | plan.flex)} días)")
        self.precargar_tramos([k for d in self.destinos_detalle for o in b["origenes"]
                               for k in [(o, d, f, self.nivel) for f in plan.idas] + [(d, o, f, self.nivel) for f in plan.vueltas]])
        idas, vueltas = {}, {}
        for d in self.destinos_detalle:
            for o in b["origenes"]:
                for f in plan.idas:
                    ops = self.tramos(o, d, f, "ida")
                    if ops:
                        idas[(o, d, f)] = ops[0]
            for o in b["origenes"]:
                for f in plan.vueltas:
                    ops = self.tramos(d, o, f, "vuelta")
                    if ops:
                        vueltas[(d, o, f)] = ops[0]
        combos = estrategias.combinar_solo_ida(self.reglas, plan, idas, vueltas, self.destinos_detalle)
        for i, v, d, es_base, op in combos:
            self.ya.add((i, v))
            self.guardar(op["detalle"]["tramos"][0]["origen"], d, i, v, [op], op["link"], es_base, commit=False)
        self.con.commit()
        if combos:
            i, v, d, _, op = combos[0]
            log.info(f"  ► Mejor con 2 pasajes: {self.moneda} {op['precio'] / self.pax:,.0f} x persona — {i} a {v} "
                     f"({op['aerolineas']})")

    def verificar_ida_vuelta(self) -> None:
        """Estrategia mixta: consulta el ida y vuelta real en las mejores fechas encontradas."""
        n = int(self.b.get("verificar_top", 8))
        top = self.top(n)
        if not top:
            return
        log.info(f"  Verificando ida y vuelta en las {len(top)} mejores fechas")
        tareas = [(o, d, i, v, self.nivel) for (i, v, d), _ in top for o in self.b["origenes"]]
        res = self.lote(tareas)
        for t in tareas:
            o, d, i, v, _ = t
            if res.get(t):
                self.guardar(o, d, i, v, res[t][0][:3], res[t][1], self.reglas.es_base(i, v))
        self.seguir()

    # ------------------------------------------------------------------ escala armada por separado
    @staticmethod
    def _fechas_conexion(primeros: list[dict], limite: dt.date | None) -> list[dt.date]:
        """Días en que conviene buscar el 2do pasaje: el de llegada de los 3 primeros más baratos y el siguiente."""
        fechas = set()
        for t in sorted(primeros, key=lambda t: t["precio"])[:3]:
            llega = estrategias._dt(t["llegada"])
            if llega:
                fechas.add(llega.date())
                fechas.add(llega.date() + dt.timedelta(days=1))
        return sorted(f for f in fechas if not limite or f <= limite)[:2]

    def _via_hub(self, salidas: list[str], hub: str, llegadas: list[str], fecha: dt.date,
                 limite: dt.date | None, sentido: str) -> tuple[dict, dict] | None:
        """Mejor combinación salida->hub + hub->llegada armando la conexión con pasajes separados."""
        min_h, max_h = self.b.get("conexion_min_horas", 4), self.b.get("conexion_max_horas", 24)
        primeros = [t for o in salidas for t in self.tramos(o, hub, fecha, sentido)]
        if not primeros:
            return None
        fechas = self._fechas_conexion(primeros, limite)
        segundos = [t for f in fechas for d in llegadas for t in self.tramos(hub, d, f, sentido)]
        return estrategias.emparejar(primeros, segundos, min_h, max_h)

    def escala_separada(self) -> None:
        b = self.b
        top = self.top(int(b.get("escala_top", 4)))
        if not top:
            return
        hubs = _hubs(b, [top[0][0][2]])
        if not hubs:
            return
        log.info(f"  Escalas armadas por separado vía {', '.join(hubs)} para las {len(top)} mejores fechas")
        n, orig = self.nivel, b["origenes"]
        # etapa 1 (en paralelo): primer pasaje hasta cada hub + pasajes directos de ida y de vuelta
        etapa1 = []
        for (i, v, d), _ in top:
            for h in hubs:
                etapa1 += [(o, h, i, n) for o in orig] + [(d, h, v, n)]
            etapa1 += [(o, d, i, n) for o in orig] + [(d, o, v, n) for o in orig]
        self.precargar_tramos(etapa1)
        # etapa 2 (en paralelo): segundo pasaje desde el hub, en los días en que llega el primero
        etapa2 = []
        for (i, v, d), _ in top:
            for h in hubs:
                for f in self._fechas_conexion([t for o in orig for t in self.cache_tramos.get((o, h, i, n), [])], None):
                    etapa2.append((h, d, f, n))
                for f in self._fechas_conexion(self.cache_tramos.get((d, h, v, n), []), self.reglas.vuelta_hasta):
                    etapa2 += [(h, o, f, n) for o in orig]
        self.precargar_tramos(etapa2)
        mejoras = 0
        for (i, v, d), (precio_actual, es_base, _, _) in top:
            ida_st = [p for h in hubs if (p := self._via_hub(b["origenes"], h, [d], i, None, "ida"))]
            vta_st = [p for h in hubs if (p := self._via_hub([d], h, b["origenes"], v, self.reglas.vuelta_hasta, "vuelta"))]
            ida_dir = sorted((t for o in b["origenes"] for t in self.tramos(o, d, i, "ida")), key=lambda t: t["precio"])[:1]
            vta_dir = sorted((t for o in b["origenes"] for t in self.tramos(d, o, v, "vuelta")),
                             key=lambda t: t["precio"])[:1]
            opciones_ida = [list(p) for p in ida_st] + [ida_dir] * bool(ida_dir)
            opciones_vta = [list(p) for p in vta_st] + [vta_dir] * bool(vta_dir)
            mejor = None
            for ti in opciones_ida:
                for tv in opciones_vta:
                    if len(ti) == 1 and len(tv) == 1:
                        continue   # dos directos = ya cubierto por "dos solo ida"
                    op = estrategias.opcion_compuesta("escala_separada", ti, tv)
                    if mejor is None or op["precio"] < mejor["precio"]:
                        mejor = op
            if mejor and mejor["precio"] < precio_actual - 0.5:
                mejoras += 1
                self.guardar(mejor["detalle"]["tramos"][0]["origen"], d, i, v, [mejor], mejor["link"], es_base)
                log.info(f"  ► Escala armada más barata {i} a {v}: {self.moneda} {mejor['precio'] / self.pax:,.0f} "
                         f"x persona (antes {precio_actual / self.pax:,.0f}) — {mejor['ruta']}")
        if not mejoras:
            log.info("  Las escalas armadas no mejoraron el precio en estas fechas.")

    # ------------------------------------------------------------------ equipaje
    @staticmethod
    def _misma(ops: list[dict], ref: dict) -> dict | None:
        for o in ops:
            if o["salida"] == ref["salida"] and o["ruta"] == ref["ruta"]:
                return o
        for o in ops:
            if o["salida"] == ref["salida"]:
                return o
        return None

    def comparar_sin_equipaje(self) -> None:
        """Con equipaje elegido: re-cotiza las mejores opciones SIN equipaje para mostrar cuánto suma."""
        top = self.top(int(self.b.get("verificar_top", 8)))
        if not top:
            return
        log.info(f"  Comparando {len(top)} mejores opciones sin equipaje")
        rt = [(origen, d, i, v, 0) for (i, v, d), (_, _, op, origen) in top if op.get("tipo", "ida_vuelta") == "ida_vuelta"]
        res_rt = self.lote(rt)
        self.precargar_tramos([(t["origen"], t["destino"], dt.date.fromisoformat(t["fecha"]), 0)
                               for _, (_, _, op, _) in top if op.get("detalle") for t in op["detalle"]["tramos"]])
        self.seguir()
        for (i, v, d), (_, es_base, op, origen) in top:
            if op.get("tipo", "ida_vuelta") == "ida_vuelta":
                r = res_rt.get((origen, d, i, v, 0))
                igual = self._misma(r[0], op) if r else None
                if igual:
                    self.guardar(origen, d, i, v, [igual], r[1], es_base, nivel=0)
                continue
            nuevos = []
            for t in op["detalle"]["tramos"]:
                igual = self._misma(self.tramos(t["origen"], t["destino"], dt.date.fromisoformat(t["fecha"]),
                                                t["sentido"], 0), t)
                if not igual:
                    break
                nuevos.append(igual)
            else:
                ida = [t for t in nuevos if t["sentido"] == "ida"]
                vta = [t for t in nuevos if t["sentido"] == "vuelta"]
                comp = estrategias.opcion_compuesta(op["tipo"], ida, vta)
                self.guardar(origen, d, i, v, [comp], comp["link"], es_base, nivel=0)

    # ------------------------------------------------------------------ exploración
    def explorar(self, iniciales: list[Combo]) -> None:
        """Pasada rápida por todos los destinos con pocas fechas de muestra; deja los N más baratos."""
        b = self.b
        top_n = int(b.get("explorar_top") or 0)
        muestras = muestras_exploracion(self.reglas, iniciales)
        log.info(f"  Exploración: {len(b['destinos'])} destinos x {len(muestras)} fechas de muestra "
                 f"(~{len(b['destinos']) * len(muestras)} consultas); después detalle de los {top_n} más baratos")
        mejor: dict[str, float] = {}
        tareas = [(b["origenes"][(k + j) % len(b["origenes"])], destino, ida, vta, self.nivel)  # alterna orígenes
                  for k, destino in enumerate(b["destinos"]) for j, (ida, vta) in enumerate(muestras)]
        res = self.lote(tareas)
        for t in tareas:
            origen, destino, ida, vta, _ = t
            r = res.get(t)
            if r:
                ops, link = r
                db.guardar_exploracion(self.con, self.corrida_id, self.nombre, self.ahora, origen, destino, ida, vta,
                                       ops[0], self.pax, self.moneda, link)
                mejor[destino] = min(mejor.get(destino, float("inf")), ops[0]["precio"])
        ranking = sorted(mejor, key=mejor.get)
        if not ranking:
            # Antes caía a "los primeros N de la lista" (orden alfabético) y gastaba cientos de consultas
            # en destinos sin vuelos. Sin resultados en la exploración no tiene sentido el detalle.
            log.info("  ⚠ La exploración no encontró vuelos a ningún destino: no hago la búsqueda en detalle. "
                     "Revisá los destinos y filtros (escalas, duración) o logs/ultimo_sin_vuelos.html.")
            self.destinos_detalle = []
            self.parar = True
            self.seguir()
        self.destinos_detalle = ranking[:top_n]
        db.marcar_elegidos(self.con, self.corrida_id, self.destinos_detalle)
        log.info("  Más baratos: " + ", ".join(f"{d} ({self.moneda} {mejor[d] / self.pax:,.0f})"
                                                for d in self.destinos_detalle if d in mejor))

    # ------------------------------------------------------------------ orquestación
    def correr(self) -> bool:
        b, reglas = self.b, self.reglas
        estrategia = b.get("estrategia", "ida_vuelta") if es_ida_vuelta(b) else "ida_vuelta"
        log.info(f"== {self.nombre} ==  [proveedor: {self.proveedor.nombre} · estrategia: {estrategia}"
                 f"{' · equipaje: ' + b.get('equipaje', '') if self.nivel else ''}]")
        filtros = []
        if reglas.meses:
            filtros.append("meses " + ",".join(str(m) for m in sorted(reglas.meses)))
        if not reglas.fijas:
            filtros.append(f"salidas {reglas.desde} a {reglas.hasta}")
        if reglas.vuelta_hasta:
            filtros.append(f"vuelta hasta {reglas.vuelta_hasta}")
        if filtros:
            log.info("  Filtros: " + " · ".join(filtros))

        iniciales = reglas.iniciales()
        if not iniciales:
            log.info("  No hay fechas válidas para buscar (¿rango en el pasado o meses_permitidos sin días?).")
            db.finalizar_corrida(self.con, self.corrida_id, 0, 0, 0)
            return False
        niveles = list(b.get("niveles_equipaje") or [])
        nombres = list(b.get("equipajes") or [])
        if not niveles or niveles[0] != self.nivel:   # el principal manda (p. ej. si se pisó nivel_equipaje)
            niveles, nombres = [self.nivel], [b.get("equipaje", "ninguno")]
        try:
            if usa_exploracion(b):
                self.explorar(iniciales)
            for k, nivel in enumerate(niveles):
                if k:
                    self._cambiar_nivel(nivel, nombres[k] if k < len(nombres) else str(nivel))
                if len(niveles) > 1:
                    log.info(f"  -- Equipaje: {self.b.get('equipaje')} --")
                if estrategia == "ida_vuelta":
                    self.pasadas_ida_vuelta(iniciales)
                elif estrategia == "ambas":
                    # las dos búsquedas completas, en todas las fechas: ida y vuelta Y dos pasajes sueltos
                    self.pasadas_ida_vuelta(iniciales)
                    self.solo_ida()
                else:
                    self.solo_ida()
                    if estrategia == "mixta":
                        self.verificar_ida_vuelta()
                if usa_escala_separada(self.b):
                    self.escala_separada()
                if self.nivel > 0 and 0 not in niveles:
                    self.comparar_sin_equipaje()   # con varios equipajes ya está el precio "sin equipaje" completo
                if es_ida_vuelta(b) and estrategia in ("ida_vuelta", "mixta", "ambas"):
                    self.horarios_vuelta()
        except _Parar:
            pass
        return self.cerrar()

    def _cambiar_nivel(self, nivel: int, nombre: str) -> None:
        """Pasa al siguiente equipaje: mismas fechas y estrategia, precios nuevos (se guardan con su nivel)."""
        self._hubo_datos = self._hubo_datos or bool(self.precio_base or self.precio_flex)
        self.nivel = nivel
        self.b = {**self.b, "nivel_equipaje": nivel, "equipaje": nombre}
        self.ya, self.precio_base, self.precio_flex, self.mejores = set(), {}, {}, {}

    def cerrar(self) -> bool:
        tiene_datos = self._hubo_datos or bool(self.precio_base or self.precio_flex)
        if self.consultas and self.errores < self.consultas and tiene_datos:
            estado = db.COMPLETA
        elif tiene_datos:
            estado = db.PARCIAL
        else:
            estado = 0
        db.finalizar_corrida(self.con, self.corrida_id, self.consultas, self.errores, estado)
        if self.precio_base:
            (i, v), p = min(self.precio_base.items(), key=lambda x: x[1])
            log.info(f"  ► Mejor: {self.moneda} {p / self.pax:,.0f} x persona — {i}" + (f" a {v}" if v else ""))
        if self.precio_flex:
            (i, v), p = min(self.precio_flex.items(), key=lambda x: x[1])
            log.info(f"  ► Mejor cambiando días: {self.moneda} {p / self.pax:,.0f} x persona — {i} a {v} "
                     f"({(v - i).days} días)")
        return estado == db.COMPLETA


def correr_busqueda(con, b: dict, g: dict, presupuesto: list[int], proveedor: Proveedor,
                    dormir: Callable[[float], None] = time.sleep) -> bool:
    """Ejecuta una búsqueda completa (exploración, estrategia elegida, escalas armadas y equipaje)."""
    return Corrida(con, b, g, presupuesto, proveedor, dormir).correr()


def usa_exploracion(b: dict) -> bool:
    return int(b.get("explorar_top") or 0) > 0 and len(b.get("destinos") or []) > int(b["explorar_top"])


def muestras_exploracion(reglas: "ReglasFechas", iniciales: list[Combo], n: int = 3) -> list[Combo]:
    """n combinaciones repartidas en el rango (con la duración del medio) para comparar destinos rápido."""
    if not iniciales:
        return []
    durs = sorted({(v - i).days for i, v in iniciales if v}) if reglas.ida_vuelta else []
    medio = durs[len(durs) // 2] if durs else None
    pool = [c for c in iniciales if medio is None or (c[1] - c[0]).days == medio] or iniciales
    if len(pool) <= n:
        return pool
    idx = sorted({round(k * (len(pool) - 1) / (n - 1)) for k in range(n)})
    return [pool[i] for i in idx]
