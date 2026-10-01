"""Estrategias de precio: dos solo ida, verificación de ida y vuelta, escala armada y equipaje."""
import datetime as dt

from src.core import config_editor as ce
from src.core import database as db
from src.core import estrategias
from src.core.config import cargar_config
from src.core.flight_searcher import Proveedor, ReglasFechas, correr_busqueda, estimar
from src.reporting.metrics import datos_busqueda
from tests.test_buscador import escribir_config
from tests.test_editor import AÑO, CFG

INI, FIN = dt.date(AÑO, 1, 5), dt.date(AÑO, 1, 20)


def op(precio, aer, fecha, sale="10:00", llega="12:00", escalas=0, ruta=None, o="X", d="Y"):
    return {"precio": float(precio), "aerolineas": aer, "escalas": escalas, "duracion_min": 120,
            "salida": f"{fecha} {sale}", "llegada": f"{fecha} {llega}", "ruta": ruta or f"{o} → {d}"}


class Mercado(Proveedor):
    """Proveedor falso con reglas de precios conocidas (todos los precios son TOTAL para 2 pasajeros).

    * Ida y vuelta AEP/EZE <-> NQN: 300 fijo.
    * Solo ida AEP->NQN: 100 (JetSMART), 120 el día 10; vuelta NQN->EZE: 90 (Aerolíneas), NQN->AEP: 95.
    * Solo ida a NRT: 1000; a MAD: 300 (llega 10:00); MAD->NRT: 350 a las 16:00 y 200 a las 11:00
      (esta última no deja 4 h de conexión). Vuelta NRT->MAD 300 + MAD->EZE 300 vs NRT->EZE 1000.
    * Equipaje: +40 por pasaje con valija despachada.
    """
    nombre, requiere_pausa = "mercado", False

    def __init__(self):
        self.consultas = []

    def buscar(self, b, g, o, d, ida, vta):
        self.consultas.append((o, d, ida, vta, b.get("nivel_equipaje", 0)))
        extra = 40 if b.get("nivel_equipaje", 0) >= 2 else 0
        f = ida.isoformat()
        if vta:   # ida y vuelta
            precio = 300 if d == "NQN" else 1900
            return [op(precio + extra, "Aerolíneas Argentinas", f, o=o, d=d)], "rt"
        tabla = {
            ("NQN",): lambda: [op((120 if ida.day == 10 else 100) + extra, "JetSMART", f, o=o, d=d)],
            ("AEP", "NQN"): None,
        }
        del tabla
        if d == "NQN":
            return [op((120 if ida.day == 10 else 100) + extra, "JetSMART", f, o=o, d=d)], f"ow-{o}{d}{f}"
        if o == "NQN":
            return [op((90 if d == "EZE" else 95) + extra, "Aerolíneas Argentinas", f, o=o, d=d)], f"ow-{o}{d}{f}"
        if d == "MAD" or (o == "NRT" and d == "MAD"):
            return [op(300 + extra, "Iberia", f, "20:00" if o != "NRT" else "08:00",
                       "10:00" if o != "NRT" else "18:00", o=o, d=d)], "ow"
        if o == "MAD" and d == "NRT":
            return [op(200 + extra, "Barata Air", f, "11:00", "23:00", o=o, d=d),
                    op(350 + extra, "Iberia", f, "16:00", "23:59", o=o, d=d)], "ow"
        if o == "MAD":   # MAD -> EZE/AEP
            return [op(300 + extra, "Iberia", f, "23:00", "23:59", o=o, d=d)], "ow"
        return [op(1000 + extra, "Directo Air", f, o=o, d=d)], "ow"


def _config(**extra):
    escribir_config(CFG)
    base = {"nombre": "Test", "destinos": ["NQN"], "viajar_desde": INI.isoformat(), "viajar_hasta": FIN.isoformat(),
            "dias_min": 7, "dias_max": 7, "pasajeros": 2, "max_escalas": 1}
    base.update(extra)
    ce.crear(base)
    return cargar_config()


def _correr(cfg, **cambios):
    b = {**cfg.buscar("Test"), **cambios}
    prov = Mercado()
    with db.conexion() as con:
        correr_busqueda(con, b, cfg.general, [5000], prov, dormir=lambda s: None)
        d = datos_busqueda(con, "Test", b)
    return b, prov, d


def test_plan_tramos_cubre_todas_las_duraciones_con_pocas_consultas():
    cfg = _config(dias_min=5, dias_max=9)
    b = cfg.buscar("Test")
    plan = estrategias.plan_tramos(ReglasFechas(b), b)
    assert plan.idas[0] == INI and plan.vueltas[-1] == FIN
    assert plan.idas[-1] == FIN - dt.timedelta(days=5) and plan.vueltas[0] == INI + dt.timedelta(days=5)
    e_rt = estimar({**b, "estrategia": "ida_vuelta"})
    e_ow = estimar({**b, "estrategia": "solo_ida"})
    assert e_ow["consultas"] < e_rt["consultas"], (e_ow, e_rt)


def test_dos_solo_ida_encuentra_aerolineas_distintas_y_vuelve_por_otro_aeropuerto():
    cfg = _config(estrategia="solo_ida")
    b, prov, d = _correr(cfg, estrategia="solo_ida")
    assert all(c[3] is None for c in prov.consultas), "solo consulta pasajes de solo ida"
    m = d["mejor"]
    assert m["tipo"] == "dos_solo_ida"
    assert m["precio"] == (100 + 90) / 2, "JetSMART a la ida + Aerolíneas volviendo a EZE"
    tramos = m["detalle"]["tramos"]
    assert tramos[0]["aerolineas"] == "JetSMART" and tramos[1]["destino"] == "EZE"
    assert all(c["dias"] == 7 for c in d["top"])


def test_mixta_verifica_ida_y_vuelta_en_las_mejores_fechas():
    cfg = _config(estrategia="mixta")
    b, prov, d = _correr(cfg, estrategia="mixta", verificar_top=3)
    rt = [c for c in prov.consultas if c[3] is not None]
    assert len(rt) == 3 * 2, "3 fechas x 2 orígenes"
    assert d["mejor"]["tipo"] == "dos_solo_ida"   # acá los 2 pasajes ganan (190 vs 300)
    assert d["por_tipo"]["ida_vuelta"] == 150 and d["por_tipo"]["dos_solo_ida"] == 95


def test_escala_armada_respeta_la_conexion_minima():
    cfg = _config(destinos=["NRT"], estrategia="solo_ida", escala_separada=True, hubs=["MAD"])
    b, prov, d = _correr(cfg, destinos=["NRT"], estrategia="solo_ida", escala_separada=True, hubs=["MAD"],
                         escala_top=1, conexion_min_horas=4)
    m = d["mejor"]
    assert m["tipo"] == "escala_separada"
    # ida: EZE/AEP->MAD 300 + MAD->NRT 350 (el de 200 sale a la hora de llegar: descartado); vuelta 300 + 300
    assert m["precio"] == (300 + 350 + 300 + 300) / 2
    assert "aviso" in m["detalle"] and len(m["detalle"]["tramos"]) == 4
    assert " ⇢ " in m["ruta"]


def test_emparejar():
    a = [{"precio": 10, "escalas": 0, "llegada": "2027-01-01 10:00", "salida": ""}]
    b = [{"precio": 1, "escalas": 0, "salida": "2027-01-01 11:00", "llegada": ""},
         {"precio": 5, "escalas": 0, "salida": "2027-01-01 15:00", "llegada": ""},
         {"precio": 2, "escalas": 0, "salida": "2027-01-03 15:00", "llegada": ""}]
    assert estrategias.emparejar(a, b, 4, 24)[1]["precio"] == 5


def test_equipaje_despachado_y_comparacion_sin_equipaje():
    cfg = _config(estrategia="solo_ida")
    b, prov, d = _correr(cfg, estrategia="solo_ida", equipaje="despachado", nivel_equipaje=2, verificar_top=2)
    assert d["mejor"]["precio"] == (140 + 130) / 2, "precios con valija incluida"
    assert any(c[4] == 0 for c in prov.consultas), "re-cotiza sin equipaje"
    assert d["mejor"]["sin_equipaje"] == (100 + 90) / 2
    assert d["equipaje"] == "despachado"


def test_estimacion_incluye_etapas():
    cfg = _config(destinos=["NRT"], estrategia="mixta", escala_separada=True, hubs=["MAD", "IST"])
    b = cfg.buscar("Test")
    e = estimar(b)
    assert e["tramos"] > 0 and e["verificacion"] > 0 and e["escala"] == min(4, e["combinaciones"]) * 2 * 9


def test_email_de_opcion_armada_muestra_los_pasajes_y_el_aviso():
    from src.notifications import mailer
    cfg = _config(destinos=["NRT"], estrategia="solo_ida", escala_separada=True, hubs=["MAD"])
    b, prov, d = _correr(cfg, destinos=["NRT"], estrategia="solo_ida", escala_separada=True, hubs=["MAD"],
                         escala_top=1)
    with db.conexion() as con:
        cid = con.execute("SELECT MAX(id) FROM corridas WHERE busqueda='Test'").fetchone()[0]
        hoy = mailer._mejores_de_corrida(con, cid, None, None)[0]
    info = {"busqueda": "Test", "corrida_id": cid, "motivo": "precio", "hoy": hoy,
            "ref": {**hoy, "precio": hoy["precio"] + 100}, "prev_best": None,
            "stats": {"minimo": hoy["precio"], "maximo": 1000, "promedio": 800, "n": 3}}
    asunto, html_, texto = mailer.componer_email(info, b)
    assert "escala armada" in html_ and "Pasajes separados" in html_ and "MAD" in html_
    assert texto.count("  - ") == 4 and "ATENCIÓN" in texto


def test_duracion_con_conexion():
    t = [{"duracion_min": 600, "salida": "2027-01-05 20:00", "llegada": "2027-01-06 10:00"},
         {"duracion_min": 800, "salida": "2027-01-06 16:00", "llegada": "2027-01-07 10:00"}]
    assert estrategias.duracion_total(t) == 600 + 360 + 800
