"""Confirma en vivo los precios más baratos de cada corrida, como los ve una persona en Google Flights.

Por qué hace falta: la búsqueda masiva lee la respuesta rápida de Google (los datos que vienen dentro del
HTML). Esa respuesta a veces trae una tarifa guardada que ya no existe (ej: una promo de 115 US$ que al abrir
el link es de 313). Lo que ve una persona sale de otra consulta que hace la página con JavaScript
(GetShoppingResults), firmada por el propio navegador. Acá se abre cada link en un Chrome real (sin ventana),
se espera esa respuesta y se toma el precio de ahí.

Para cada fila confirmada:
  * mismo precio            -> verificado = 1
  * precio distinto         -> se corrige, verificado = 2 y precio_google = el que traía la búsqueda
  * Google contesta precios distintos en cada carga (pasa: un servidor todavía tiene la promo agotada)
                            -> queda el más alto, verificado = 3 y precio_google = el más bajo que se vio
  * el vuelo ya no aparece  -> verificado = -1 (no se muestra ni cuenta para alertas o el mínimo histórico)
  * no se pudo confirmar    -> verificado = 0 (se muestra como "sin confirmar"; no dispara mails)

Se repite hasta que lo más barato (en general, por equipaje, por rango de cada suscripción y por cada aviso
de precio) esté confirmado, con un tope de links por corrida.

Necesita `pip install playwright` y Google Chrome (viene en los runners de GitHub). Si no están, la
corrida sigue como siempre, sin confirmar.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import random
import time
from typing import Callable

from . import database as db
from .flight_searcher import opciones_de_payload, seleccionar

log = logging.getLogger("vuelos")

INTENTOS = 12           # Google rechaza ~la mitad de las cargas de un navegador automatizado: se reintenta
TOPE_LINKS = 80         # links como máximo por corrida (cada uno tarda ~1-5 s)
TOP_BASE, TOP_FLEX = 10, 5


def igual(a: float, b: float) -> bool:
    """Google redondea distinto según la respuesta (4476 vs 4477): hasta US$ 2 o 0,1 % es el mismo precio."""
    return abs(a - b) <= max(2.0, 0.001 * max(a, b))


class SinNavegador(Exception):
    pass


class Navegador:
    """Chrome sin ventana. `opciones(link)` -> opciones en vivo (mismo formato que la búsqueda) o None."""

    def __init__(self):
        self._pw = self._browser = None
        self.cache: dict[str, list[dict] | None] = {}
        self.cargas = self.fallidas = 0

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise SinNavegador("falta el paquete playwright") from e
        self._pw = sync_playwright().start()
        args = ["--disable-blink-features=AutomationControlled"]
        for kw in ({"channel": "chrome"}, {}):   # Chrome instalado; si no, el Chromium de Playwright
            try:
                self._browser = self._pw.chromium.launch(args=args, **kw)
                break
            except Exception:
                continue
        if not self._browser:
            self._pw.stop()
            raise SinNavegador("no hay Chrome ni Chromium (python -m playwright install chromium)")
        ctx = self._browser.new_context()
        self._ua = ctx.new_page().evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome")
        ctx.close()
        return self

    def __exit__(self, *exc):
        try:
            self._browser.close()
        finally:
            self._pw.stop()

    def _una_carga(self, link: str) -> list[dict] | None:
        ctx = self._browser.new_context(locale="es-AR", timezone_id="America/Argentina/Buenos_Aires",
                                        user_agent=self._ua, viewport={"width": 1366, "height": 900})
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        try:
            pg = ctx.new_page()
            with pg.expect_response(lambda r: "GetShoppingResults" in r.url, timeout=12000) as ri:
                pg.goto(link, wait_until="commit", timeout=30000)
            return leer_respuesta(ri.value.text())
        except Exception:
            return None
        finally:
            ctx.close()

    def _carga_ok(self, link: str) -> list[dict] | None:
        for k in range(INTENTOS):
            self.cargas += 1
            res = self._una_carga(link)
            if res is not None:
                return res
            time.sleep(0.5 + random.random() * (1 + k / 2))
        return None

    def opciones(self, link: str) -> list[dict] | None:
        """Dos cargas independientes; si no coinciden, una tercera y se combinan (ver `combinar`)."""
        if link in self.cache:
            return self.cache[link]
        cargas = []
        for _ in range(3):
            r = self._carga_ok(link)
            if r is None:
                break
            cargas.append(r)
            if len(cargas) == 2 and _firma(cargas[0]) == _firma(cargas[1]):
                break
        res = combinar(cargas) if cargas else None
        if res is None:
            self.fallidas += 1
        self.cache[link] = res
        return res


def _firma(ops: list[dict]) -> dict:
    out: dict = {}
    for o in ops:
        k = (o["salida"], o["ruta"])
        out[k] = min(out.get(k, o["precio"]), o["precio"])
    return out


def combinar(cargas: list[list[dict]]) -> list[dict]:
    """Google no siempre contesta lo mismo: a veces un servidor todavía tiene una promo que ya se agotó
    (ej. 116 US$ en una carga y 313 en la siguiente, para el mismo link). Para no mostrar un precio que
    después no aparece, cada vuelo toma el precio MÁS ALTO de las cargas en que figura (si figura en al menos
    la mitad), y en `min_visto` queda el más bajo, para avisar que a veces sale más barato."""
    if len(cargas) == 1:
        return cargas[0]
    firmas = [_firma(c) for c in cargas]
    out, vistos = [], set()
    for c in cargas:
        for o in c:
            k = (o["salida"], o["ruta"])
            if k in vistos:
                continue
            precios = [f[k] for f in firmas if k in f]
            if len(precios) * 2 < len(cargas):
                continue
            vistos.add(k)
            o = {**o, "precio": max(precios)}
            if not igual(min(precios), o["precio"]):
                o["min_visto"] = min(precios)
            out.append(o)
    out.sort(key=lambda o: (o["precio"], o["escalas"], o["duracion_min"] or 10**6))
    return out


def leer_respuesta(body: str) -> list[dict] | None:
    """Respuesta de GetShoppingResults -> opciones. None si Google la rechazó (error 13 / sin datos)."""
    for linea in body.splitlines():
        if not linea.startswith('[["wrb.fr"'):
            continue
        try:
            fila = json.loads(linea)[0]
        except (ValueError, IndexError):
            continue
        if len(fila) > 2 and isinstance(fila[2], str):
            try:
                return opciones_de_payload(json.loads(fila[2]))
            except ValueError:
                return None
    return None


def _misma(ops: list[dict], f: dict) -> dict | None:
    """La opción en vivo que corresponde a la fila: mismo horario de salida y misma ruta (o aerolínea)."""
    for o in ops:
        if o["salida"] == f["salida"] and o["ruta"] == f["ruta"]:
            return o
    for o in ops:
        if o["salida"] == f["salida"] and o["aerolineas"] == f["aerolineas"]:
            return o
    return None


class Verificador:
    def __init__(self, con, corrida_id: int, nav: Navegador, tope: int = TOPE_LINKS):
        self.con, self.corrida_id, self.nav, self.tope = con, corrida_id, nav, tope
        self.links = 0
        self.corregidas = self.caidas = self.ok = 0

    # ------------------------------------------------------------------ una fila
    def confirmar(self, fila: dict) -> None:
        # se relee de la base: quien elige puede traer el precio por persona u otra versión de la fila
        r = self.con.execute("SELECT rowid _rid, * FROM precios WHERE rowid=?", (fila["_rid"],)).fetchone()
        if r is None or r["verificado"] is not None:
            return
        f = dict(r)
        f["detalle"] = json.loads(f["detalle"]) if f.get("detalle") else None
        if f.get("detalle") and f["detalle"].get("tramos"):
            self._compuesta(f)
        else:
            self._link(f)

    def _vivo(self, link: str) -> list[dict] | None:
        if link not in self.nav.cache:
            self.links += 1
        return self.nav.opciones(link)

    def _link(self, f: dict) -> None:
        ops = self._vivo(f["link"])
        if ops is None:
            self._marcar(f["_rid"], 0)
            return
        # todas las filas de esta corrida que salieron del mismo link (misma consulta) se confirman juntas
        filas = [dict(r) for r in self.con.execute(
            "SELECT rowid _rid, * FROM precios WHERE corrida_id=? AND link=? AND detalle IS NULL",
            (self.corrida_id, f["link"]))]
        presentes = set()
        for x in filas:
            o = _misma(ops, x)
            if o is None:
                self._marcar(x["_rid"], -1)
                self.caidas += 1
                continue
            presentes.add((o["salida"], o["ruta"]))
            self._precio(x, o["precio"], o.get("min_visto"))
        # las opciones en vivo que no estaban guardadas (ej. lo que quedó más barato al caerse la promo)
        nuevas = [o for o in seleccionar(ops, 3, 8 if any(x["escalas"] for x in filas) else 0)
                  if (o["salida"], o["ruta"]) not in presentes]
        if nuevas and filas:
            r = filas[0]
            self.con.executemany(
                "INSERT INTO precios(corrida_id, busqueda, consultado, origen, destino, ida, vuelta, precio, moneda, "
                "pasajeros, aerolineas, escalas, duracion_min, salida, llegada, ruta, link, flexible, tipo, equipaje, "
                "detalle, verificado) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,1)",
                [(self.corrida_id, r["busqueda"], r["consultado"], r["origen"], r["destino"], r["ida"], r["vuelta"],
                  o["precio"], r["moneda"], r["pasajeros"], o["aerolineas"], o["escalas"], o["duracion_min"],
                  o["salida"], o["llegada"], o["ruta"], r["link"], r["flexible"], r["tipo"] or "ida_vuelta",
                  r["equipaje"]) for o in nuevas])
        self.con.commit()

    def _compuesta(self, f: dict) -> None:
        """Dos pasajes sueltos / escala armada: se confirma cada pasaje y se suma."""
        tramos, minimos = [], []
        for t in f["detalle"]["tramos"]:
            ops = self._vivo(t["link"]) if t.get("link") else None
            if ops is None:
                self._marcar(f["_rid"], 0)
                return
            o = _misma(ops, t)
            if o is None:
                self._marcar(f["_rid"], -1)
                self.caidas += 1
                self.con.commit()
                return
            tramos.append({**t, "precio": o["precio"]})
            minimos.append(o.get("min_visto") or o["precio"])
        total = round(sum(t["precio"] for t in tramos), 2)
        minimo = round(sum(minimos), 2)
        det = {**f["detalle"], "tramos": tramos}
        self.con.execute("UPDATE precios SET detalle=? WHERE rowid=?",
                         (json.dumps(det, ensure_ascii=False), f["_rid"]))
        self._precio(f, total, None if igual(minimo, total) else minimo)
        self.con.commit()

    def _precio(self, f: dict, vivo: float, min_visto: float | None = None) -> None:
        if min_visto is not None:
            # Google mostró dos precios distintos para el mismo vuelo: queda el más alto (verificado = 3) y en
            # precio_google el más bajo que se vio
            self.con.execute("UPDATE precios SET precio=?, verificado=3, precio_google=? WHERE rowid=?",
                             (vivo, min_visto, f["_rid"]))
            self.corregidas += not igual(vivo, f["precio"])
            self.ok += igual(vivo, f["precio"])
            return
        if igual(vivo, f["precio"]):
            self.con.execute("UPDATE precios SET precio=?, verificado=1 WHERE rowid=?", (vivo, f["_rid"]))
            self.ok += 1
        else:
            self.con.execute("UPDATE precios SET precio=?, verificado=2, precio_google=? WHERE rowid=?",
                             (vivo, f["precio"], f["_rid"]))
            self.corregidas += 1

    def _marcar(self, rid: int, v: int) -> None:
        self.con.execute("UPDATE precios SET verificado=? WHERE rowid=?", (v, rid))

    # ------------------------------------------------------------------ hasta que lo mejor esté confirmado
    def asegurar(self, elegir: Callable[[], list[dict]]) -> None:
        """`elegir()` devuelve las filas que se van a mostrar/avisar (con _rid). Confirma las que falten y
        vuelve a elegir (si una promo se cayó, aparece otra opción) hasta que todas estén confirmadas."""
        for _ in range(40):
            pendientes = [f for f in elegir() if f.get("verificado") is None]
            if not pendientes or self.links >= self.tope:
                break
            for f in pendientes:
                if self.links >= self.tope:
                    break
                self.confirmar(f)
        # segunda oportunidad para las que Google no dejó confirmar (suele ser algo pasajero)
        for f in elegir():
            if f.get("verificado") == 0 and self.links < self.tope:
                link = f["link"]
                for t in (f.get("detalle") or {}).get("tramos", []):
                    self.nav.cache.pop(t.get("link"), None)
                self.nav.cache.pop(link, None)
                self._marcar(f["_rid"], None)
                self.confirmar(f)


def top_combos(filas: list[dict], n_base: int, n_flex: int) -> list[dict]:
    """La fila más barata de cada combinación (fechas+destino+equipaje), las n primeras de cada tipo."""
    out, vistos, cuenta = [], set(), {0: 0, 1: 0}
    tope = {0: n_base, 1: n_flex}
    for f in sorted(filas, key=lambda f: f["precio"]):
        flex = 1 if f.get("flexible") else 0
        k = (f["ida"], f["vuelta"], f["destino"], f.get("equipaje") or 0, flex)
        if k in vistos or cuenta[flex] >= tope[flex]:
            continue
        vistos.add(k)
        cuenta[flex] += 1
        out.append(f)
    return out


def verificar_corrida(con, cfg, nombre: str, corrida_id: int, nav: Navegador) -> dict:
    """Confirma lo que se va a mostrar y avisar de una corrida. Devuelve un resumen para el log."""
    from src.core import suscriptores as subs
    from src.notifications.mailer import mejor_para_aviso, _mejores_de_corrida
    from src.reporting.metrics import filtro_rango, reglas_de

    meta = cfg.buscar(nombre)
    filtro = filtro_rango(meta) if meta else None
    reglas = reglas_de(meta) if meta else None   # solo fechas: se confirman TODOS los equipajes
    v = Verificador(con, corrida_id, nav, int((cfg.general.get("verificar") or {}).get("max_links", TOPE_LINKS)))
    # desde acá, los mails de esta corrida solo salen con precios confirmados (aunque algo falle en el medio)
    con.execute("UPDATE corridas SET verificada=1 WHERE id=?", (corrida_id,))
    con.commit()
    niveles = [r[0] for r in con.execute(
        "SELECT DISTINCT COALESCE(equipaje,0) FROM precios WHERE corrida_id=?", (corrida_id,))]

    def filas():
        return [f for f in db.filas_corrida(con, corrida_id) if not reglas or reglas.valida_iso(f["ida"], f["vuelta"])]

    # 1) lo más barato de cada equipaje (lo que muestran los KPIs y las tablas)
    for nv in niveles:
        v.asegurar(lambda nv=nv: top_combos([f for f in filas() if (f["equipaje"] or 0) == nv],
                                            TOP_BASE, TOP_FLEX))
    # 2) lo que dispararía cada mail: el mejor del rango de cada suscripción y de cada aviso de precio
    hoy = dt.date.today()
    for s in subs.de_busqueda(nombre):
        if not subs.vigente(s, hoy):
            continue

        def elegir(s=s):
            if s.get("tipo") == "precio":
                m = mejor_para_aviso(con, corrida_id, meta, s.get("aviso") or {}, s.get("desde"), s.get("hasta"))
                return [m] if m else []
            base = _mejores_de_corrida(con, corrida_id, s.get("desde"), s.get("hasta"), filtro)
            return base[:1]
        v.asegurar(elegir)
    return {"links": v.links, "ok": v.ok, "corregidas": v.corregidas, "caidas": v.caidas,
            "cargas": nav.cargas, "sin_confirmar": nav.fallidas}


def verificar(con, cfg, corridas: dict[str, int]) -> None:
    """Confirma las corridas recién hechas. Si no hay navegador, avisa en el log y sigue."""
    conf = cfg.general.get("verificar") or {}
    if not corridas or conf.get("activo", True) is False:
        return
    try:
        with Navegador() as nav:
            for nombre, cid in corridas.items():
                t0 = time.time()
                log.info(f"== Confirmando precios en vivo: {nombre} ==")
                r = verificar_corrida(con, cfg, nombre, cid, nav)
                log.info(f"  {r['links']} links abiertos en {time.time() - t0:.0f} s: {r['ok']} iguales, "
                         f"{r['corregidas']} con otro precio (corregidos), {r['caidas']} ya no disponibles"
                         + (f", {r['sin_confirmar']} sin poder confirmar" if r["sin_confirmar"] else ""))
    except SinNavegador as e:
        log.info(f"Sin confirmación en vivo de precios ({e}).")
