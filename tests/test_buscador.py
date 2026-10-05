import datetime as dt
import json
import os
import re
from pathlib import Path

import pytest

from src.core import database as db
from src.core.config import ConfigError, cargar_config
from src.core.flight_searcher import Proveedor, ReglasFechas, correr_busqueda
from src.core.paths import REPORTE_INDEX, REPORTES_DIR
from src.notifications import mailer
from src.reporting.generator import generar_reportes, json_seguro

HOY = dt.date(2026, 9, 26)

CONFIG = """
general:
  moneda: USD
  pausa_segundos: [0, 0]
  max_consultas_por_corrida: 500
  alertas: {emails: [yo@mail.com]}
busquedas:
  - nombre: Verano
    origenes: [AEP]
    destinos: [BRC]
    pasajeros: 2
    salida_desde: "2027-01-05"
    salida_hasta: "2027-02-28"
    meses_permitidos: [1, 2]
    duracion_dias: [7]
    paso_dias: 7
    tolerancia_dias: 1
    refinar_top: 2
  - nombre: China
    origenes: [EZE]
    destinos: [PEK]
    fechas: [{ida: 2026-11-01, vuelta: 2026-11-15}]
"""


def escribir_config(texto=CONFIG) -> Path:
    ruta = Path(os.environ["VUELOS_CONFIG"])
    ruta.write_text(texto, encoding="utf-8")
    return ruta


def cfg_(texto=CONFIG, hoy=HOY):
    return cargar_config(escribir_config(texto), hoy=hoy)


class Falso(Proveedor):
    """Proveedor determinístico: precio = base + día del mes (total para todos los pasajeros)."""
    nombre = "falso"
    requiere_pausa = False

    def __init__(self, base=200.0, escalas=1, dur=120):
        self.base, self.escalas, self.dur, self.llamadas = base, escalas, dur, 0

    def buscar(self, b, g, origen, destino, ida, vuelta):
        self.llamadas += 1
        op = {"precio": self.base + ida.day, "aerolineas": "Test Air", "escalas": self.escalas,
              "duracion_min": self.dur, "salida": f"{ida} 10:00", "llegada": f"{ida} 12:00",
              "ruta": f"{origen} → {destino}"}
        return [op], "https://www.google.com/travel/flights?test"


# --------------------------------------------------------------------------- fechas
def test_rango_estricto_y_meses():
    b = cfg_().buscar("Verano")
    r = ReglasFechas(b, hoy=HOY)
    combos = r.iniciales()
    assert combos, "debería haber combinaciones"
    assert all(dt.date(2027, 1, 5) <= i <= dt.date(2027, 2, 28) for i, _ in combos)
    assert all(i.month in (1, 2) for i, _ in combos)
    assert all((v - i).days == 7 for i, v in combos)


def test_meses_filtra_dentro_de_ventana_amplia():
    b = cfg_(CONFIG.replace('salida_desde: "2027-01-05"', 'salida_desde: "+1"')
                   .replace('salida_hasta: "2027-02-28"', 'salida_hasta: "2027-12-31"')).buscar("Verano")
    combos = ReglasFechas(b, hoy=HOY).iniciales()
    assert {i.month for i, _ in combos} <= {1, 2}


def test_descarta_pasado_y_vuelta_anterior():
    b = cfg_().buscar("Verano")
    r = ReglasFechas(b, hoy=dt.date(2027, 1, 20))
    assert not r.valida(dt.date(2027, 1, 20), dt.date(2027, 1, 27))      # hoy no: mínimo mañana
    assert r.valida(dt.date(2027, 1, 21), dt.date(2027, 1, 28))
    assert not r.valida(dt.date(2027, 1, 25), dt.date(2027, 1, 24))      # vuelta < ida
    assert all(i >= dt.date(2027, 1, 21) for i, _ in r.iniciales())


def test_flexibles_respetan_ventana_y_meses():
    b = cfg_().buscar("Verano")
    r = ReglasFechas(b, hoy=HOY)
    flex = r.flexibles([(dt.date(2027, 1, 5), dt.date(2027, 1, 12))], set())
    assert flex and all(i >= dt.date(2027, 1, 5) and i.month in (1, 2) for i, _ in flex)


def test_vuelta_hasta():
    texto = CONFIG.replace("meses_permitidos: [1, 2]", 'meses_permitidos: [1, 2]\n    vuelta_hasta: "2027-02-20"')
    r = ReglasFechas(cfg_(texto).buscar("Verano"), hoy=HOY)
    assert all(v <= dt.date(2027, 2, 20) for _, v in r.iniciales())


def test_fechas_fijas_pasadas_se_descartan():
    r = ReglasFechas(cfg_().buscar("China"), hoy=dt.date(2026, 12, 1))
    assert r.iniciales() == []


# --------------------------------------------------------------------------- config
@pytest.mark.parametrize("cambio, mensaje", [
    ("meses_permitidos: [1, 2]", "meses_permitidos: [13]"),
    ("destinos: [BRC]", "destinos: [BARILOCHE]"),
    ('salida_hasta: "2027-02-28"', 'salida_hasta: "2026-12-01"'),
    ("pasajeros: 2", "pasajeros: 2\n    clase: turista"),
])
def test_config_invalida(cambio, mensaje):
    with pytest.raises(ConfigError):
        cfg_(CONFIG.replace(cambio, mensaje))


def test_config_nombres_duplicados():
    with pytest.raises(ConfigError, match="más de una"):
        cfg_(CONFIG.replace("nombre: China", "nombre: verano"))


# --------------------------------------------------------------------------- reportes
def _correr(con, cfg, nombre, prov):
    b = cfg.buscar(nombre)
    return correr_busqueda(con, b, cfg.general, [500], prov, dormir=lambda s: None)


def test_reporte_no_se_sobreescribe_y_kpis():
    cfg = cfg_(hoy=dt.date.today())
    # fechas relativas a hoy para que no queden en el pasado
    texto = CONFIG.replace('salida_desde: "2027-01-05"', 'salida_desde: "+10"') \
                  .replace('salida_hasta: "2027-02-28"', 'salida_hasta: "+60"').replace("meses_permitidos: [1, 2]", "") \
                  .replace("fechas: [{ida: 2026-11-01, vuelta: 2026-11-15}]", 'salida_desde: "+10"\n    salida_hasta: "+30"\n    paso_dias: 10')
    cfg = cfg_(texto, hoy=dt.date.today())
    with db.conexion() as con:
        assert _correr(con, cfg, "Verano", Falso())
        generar_reportes(con, cfg)
        assert _correr(con, cfg, "China", Falso(base=1500))   # como `--busqueda China`
        _, _, datos = generar_reportes(con, cfg)
    nombres = [d["nombre"] for d in datos]
    assert set(nombres) == {"Verano", "China"}, "el índice debe tener TODAS las búsquedas"
    assert (REPORTES_DIR / "verano.html").exists() and (REPORTES_DIR / "china.html").exists()
    html = REPORTE_INDEX.read_text(encoding="utf-8")
    payload = json.loads(re.search(r"const DATOS = (.*?);\n", html).group(1).replace("<\\/", "</"))
    v = next(b for b in payload["busquedas"] if b["nombre"] == "Verano")
    s = v["stats"]
    assert s["minimo"] <= s["promedio"] <= s["maximo"]
    assert s["minimo"] == v["mejor"]["precio"]


def test_json_seguro_no_rompe_script():
    s = json_seguro({"x": "</script><script>alert(1)</script>"})
    assert "</script>" not in s and json.loads(s.replace("<\\/", "</"))["x"].startswith("</script>")


def test_corrida_colgada_pasa_a_parcial():
    with db.conexion() as con:
        cid = db.iniciar_corrida(con, "Zombie", "falso")
        db.guardar_opciones(con, cid, "Zombie", "2026-09-26 10:00", "AEP", "NQN", dt.date(2026, 11, 1),
                            dt.date(2026, 11, 8), [{"precio": 100, "aerolineas": "X", "escalas": 0, "duracion_min": 100,
                                                    "salida": "", "llegada": "", "ruta": "AEP → NQN"}],
                            "USD", 1, "l", False)
        db.cerrar_corridas_colgadas(con)
        assert con.execute("SELECT completa FROM corridas WHERE id=?", (cid,)).fetchone()[0] == db.PARCIAL


# --------------------------------------------------------------------------- alertas
def _corrida_manual(con, nombre, precio, escalas=1, dur=120):
    cid = db.iniciar_corrida(con, nombre, "falso")
    db.guardar_opciones(con, cid, nombre, "2026-09-26 10:00", "AEP", "BRC", dt.date(2027, 1, 10), dt.date(2027, 1, 17),
                        [{"precio": precio * 2, "aerolineas": "X", "escalas": escalas, "duracion_min": dur,
                          "salida": "", "llegada": "", "ruta": "AEP → BRC"}], "USD", 2, "https://g.co/x", False)
    db.finalizar_corrida(con, cid, 1, 0, db.COMPLETA)
    return cid


@pytest.mark.parametrize("hoy, esperado", [
    ({"precio": 90}, "precio"),                                   # bajó
    ({"precio": 100}, None),                                      # igual, mismas condiciones
    ({"precio": 110}, None),                                      # subió
    ({"precio": 100, "escalas": 0}, "escalas"),                   # igual, menos escalas
    ({"precio": 100, "dur": 60}, "duracion"),                     # igual, más corto
    ({"precio": 100, "escalas": 2, "dur": 60}, None),             # igual, más corto pero más escalas
])
def test_deteccion_de_mejoras(hoy, esperado):
    nombre = f"Alerta-{esperado}-{hoy}"
    with db.conexion() as con:
        primera = _corrida_manual(con, nombre, 100)
        assert mailer.detectar_mejora(con, nombre, primera) is None, "la primera corrida no avisa"
        cid = _corrida_manual(con, nombre, hoy["precio"], hoy.get("escalas", 1), hoy.get("dur", 120))
        info = mailer.detectar_mejora(con, nombre, cid)
    assert (info or {}).get("motivo") == esperado


def test_no_repite_contra_minimo_historico():
    nombre = "Serrucho"
    with db.conexion() as con:
        _corrida_manual(con, nombre, 100)
        _corrida_manual(con, nombre, 80)     # mínimo registrado = 80
        _corrida_manual(con, nombre, 120)
        cid = _corrida_manual(con, nombre, 90)   # bajó vs ayer pero NO vs el mínimo
        assert mailer.detectar_mejora(con, nombre, cid) is None


def test_email_se_compone_y_envia(monkeypatch):
    enviados = []
    monkeypatch.setenv("RESEND_API_KEY", "x")
    monkeypatch.setattr(mailer, "_enviar_resend", lambda *a: enviados.append(a))
    cfg = cfg_(CONFIG.replace("nombre: Verano", "nombre: Mail"), hoy=dt.date.today())
    with db.conexion() as con:
        _corrida_manual(con, "Mail", 100)
        cid = _corrida_manual(con, "Mail", 70)
        assert mailer.notificar(con, cfg, {"Mail": cid}) == 1
        assert mailer.notificar(con, cfg, {"Mail": cid}) == 0, "no debe mandar dos veces el mismo aviso"
    para, asunto, cuerpo, texto, _ = enviados[0]
    assert para == "yo@mail.com" and "bajó" in asunto and "Google Flights" in cuerpo and "$" not in re.sub(r"US\$", "", cuerpo)


def test_suscriptor_con_ventana():
    nombre = "Ventana"
    with db.conexion() as con:
        _corrida_manual(con, nombre, 100)
        cid = _corrida_manual(con, nombre, 70)          # ida 2027-01-10
        assert mailer.detectar_mejora(con, nombre, cid, desde="2027-02-01") is None
        assert mailer.detectar_mejora(con, nombre, cid, desde="2027-01-01", hasta="2027-01-31")["motivo"] == "precio"


# --------------------------------------------------------------------------- arreglos 10/2026
class ConVuelta(Falso):
    """Ida y vuelta: Google solo da la hora de la ida. Solo ida de regreso: dos aerolíneas."""
    def buscar(self, b, g, origen, destino, ida, vuelta):
        if vuelta is None and origen == "BRC":
            self.llamadas += 1
            return [{"precio": 50.0, "aerolineas": "Otra", "escalas": 0, "duracion_min": 120,
                     "salida": f"{ida} 07:15", "llegada": f"{ida} 09:15", "ruta": "BRC → AEP"},
                    {"precio": 80.0, "aerolineas": "Test Air", "escalas": 0, "duracion_min": 120,
                     "salida": f"{ida} 18:40", "llegada": f"{ida} 20:40", "ruta": "BRC → AEP"}], "https://x"
        return super().buscar(b, g, origen, destino, ida, vuelta)


def test_horarios_de_vuelta_de_la_misma_aerolinea():
    from src.reporting.metrics import datos_busqueda
    cfg = cfg_()
    b = cfg.buscar("Verano")
    prov = ConVuelta()
    with db.conexion() as con:
        correr_busqueda(con, b, cfg.general, [500], prov, dormir=lambda s: None)
        d = datos_busqueda(con, "Verano", b)
    m = d["mejor"]
    assert m["ida_hora"] == "10:00"
    assert m["vta_hora"] == "18:40" and m["vta_aprox"], "la vuelta es la de la MISMA aerolínea, no la más barata"
    assert all(c["vta_hora"] == "18:40" for c in d["top"][:5])


def test_consulta_con_pais_fijo():
    from src.core.flight_searcher import _armar_query, link_google_flights, params_google
    cfg = cfg_()
    b, g = cfg.buscar("Verano"), cfg.general
    assert g["pais"] == "AR"
    q = _armar_query(b, g, "AEP", "BRC", dt.date(2027, 1, 10), dt.date(2027, 1, 17))
    assert params_google(q, g)["gl"] == "AR"
    assert "gl=AR" in link_google_flights(b, g, "AEP", "BRC", dt.date(2027, 1, 10), dt.date(2027, 1, 17))


def test_ofertas_se_comparan_con_la_salida_del_mismo_dia_y_sin_repetir():
    from src.reporting.metrics import datos_busqueda
    cfg = cfg_(CONFIG.replace("origenes: [AEP]", "origenes: [AEP, EZE]"))
    b = cfg.buscar("Verano")
    with db.conexion() as con:
        cid = db.iniciar_corrida(con, "Verano", "test")
        op = lambda p: [{"precio": p * 2, "aerolineas": "X", "escalas": 0, "duracion_min": 100,  # noqa: E731
                         "salida": "2027-01-10 06:00", "llegada": "2027-01-10 08:00", "ruta": "A → B"}]
        i = dt.date(2027, 1, 10)
        for o in ("AEP", "EZE"):   # 7 días saliendo el 10: 150; el 12: 70 (la más barata del período)
            db.guardar_opciones(con, cid, "Verano", "2026-10-05 10:00", o, "BRC", i, i + dt.timedelta(7), op(150),
                                "USD", 2, "l", flexible=False)
            db.guardar_opciones(con, cid, "Verano", "2026-10-05 10:00", o, "BRC", i + dt.timedelta(2),
                                i + dt.timedelta(9), op(70), "USD", 2, "l", flexible=False)
            # 6 días saliendo el 10: 80 (por AEP y por EZE: debe aparecer una sola vez)
            db.guardar_opciones(con, cid, "Verano", "2026-10-05 10:00", o, "BRC", i, i + dt.timedelta(6), op(80),
                                "USD", 2, "l", flexible=True)
        db.finalizar_corrida(con, cid, 6, 0, db.COMPLETA)
        d = datos_busqueda(con, "Verano", b)
    of = [o for o in d["ofertas"] if o["ida"] == "2027-01-10"]
    assert len(of) == 1, "una fila por fechas aunque se haya encontrado por EZE y por AEP"
    assert of[0]["ref_precio"] == 150 and of[0]["ref_mismo_dia"] and of[0]["ref_ida"] == "2027-01-10"


def test_alerta_cuando_vuelve_al_minimo_historico():
    cfg = cfg_()
    b = {**cfg.buscar("Verano"), "nombre": "VueltaMin"}
    with db.conexion() as con:
        def corrida(precio):
            cid = db.iniciar_corrida(con, "VueltaMin", "test")
            db.guardar_opciones(con, cid, "VueltaMin", "2026-10-01 10:00", "AEP", "BRC", dt.date(2027, 1, 10),
                                dt.date(2027, 1, 17), [{"precio": precio * 2, "aerolineas": "X", "escalas": 0,
                                                        "duracion_min": 100, "salida": "2027-01-10 06:00",
                                                        "llegada": "", "ruta": ""}], "USD", 2, "l", flexible=False)
            db.finalizar_corrida(con, cid, 1, 0, db.COMPLETA)
            return cid
        corrida(72)
        corrida(95)
        c3 = corrida(72)                   # volvió al mínimo: avisa aunque no sea MÁS barato
        info = mailer.detectar_mejora(con, "VueltaMin", c3)
        assert info and info["motivo"] == "minimo" and info["prev_best"] == 95
        asunto, html, texto = mailer.componer_email(info, b)
        assert "mínimo" in asunto and "Salida" in html
        c4 = corrida(72)                   # sigue igual: no repite el aviso
        assert mailer.detectar_mejora(con, "VueltaMin", c4) is None


def test_nivel_de_precio_bajo_habitual_alto():
    from src.reporting.metrics import nivel_precio
    precios = [100 + i for i in range(100)]          # habitual ≈ 125–175
    assert nivel_precio(precios, 72, 5)["nivel"] == "bajo"
    assert nivel_precio(precios, 150, 5)["nivel"] == "habitual"
    assert nivel_precio(precios, 190, 5)["nivel"] == "alto"
    n = nivel_precio(precios, 72, 5)
    assert n["desde"] < n["mediana"] < n["hasta"] and n["diferencia"] > 0
    assert nivel_precio(precios[:10], 72, 5) is None, "con pocos precios no se muestra"
    assert nivel_precio(precios, 72, 1) is None, "con una sola corrida no hay 'habitual'"
