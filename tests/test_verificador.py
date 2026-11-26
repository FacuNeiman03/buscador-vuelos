"""Confirmación en vivo: corrige precios guardados por Google, descarta promos caídas y frena mails sin confirmar."""
import datetime as dt
import json

from src.core import database as db
from src.core import verificador as ver
from src.notifications import mailer
from tests.test_buscador import cfg_

I = dt.date(2027, 1, 12)
V = I + dt.timedelta(7)


def _op(total, salida="06:00", ruta="AEP → BRC", escalas=0):
    return {"precio": total, "aerolineas": "JetSMART", "escalas": escalas, "duracion_min": 120,
            "salida": f"{I} {salida}", "llegada": f"{I} 08:00", "ruta": ruta}


class NavFalso:
    """Navegador de mentira: link -> opciones en vivo (None = Google no respondió)."""

    def __init__(self, vivo: dict):
        self.vivo, self.cache, self.cargas, self.fallidas, self.abiertos = vivo, {}, 0, 0, []

    def opciones(self, link):
        if link not in self.cache:
            self.abiertos.append(link)
            self.cache[link] = self.vivo.get(link)
            self.fallidas += self.cache[link] is None
        return self.cache[link]


def _corrida(con, filas, nombre="Verano"):
    """filas: [(link, [ops], ida, vuelta)]"""
    cid = db.iniciar_corrida(con, nombre, "t")
    for link, ops, i, v in filas:
        db.guardar_opciones(con, cid, nombre, "x", "AEP", "BRC", i, v, ops, "USD", 2, link, flexible=False)
    db.finalizar_corrida(con, cid, 1, 0, db.COMPLETA)
    return cid


def _filas(con, cid):
    return {(f["ida"], f["salida"][-5:]): f for f in db.filas_corrida(con, cid)}


def test_corrige_precio_y_descarta_promo_caida():
    cfg = cfg_()
    with db.conexion() as con:
        cid = _corrida(con, [
            ("L1", [_op(115, "06:00"), _op(400, "09:00")], I, V),       # promo guardada de 115 (ya no existe)
            ("L2", [_op(300, "07:00")], I + dt.timedelta(1), V + dt.timedelta(1)),  # precio real distinto
        ])
        nav = NavFalso({"L1": [_op(313, "06:00"), _op(400, "09:00")],
                        "L2": [_op(320, "07:00")]})
        ver.verificar_corrida(con, cfg, "Verano", cid, nav)
        f = _filas(con, cid)
        assert f[(I.isoformat(), "06:00")]["precio"] == 313 and f[(I.isoformat(), "06:00")]["verificado"] == 2
        assert f[(I.isoformat(), "06:00")]["precio_google"] == 115
        assert f[(I.isoformat(), "09:00")]["verificado"] == 1
        assert f[((I + dt.timedelta(1)).isoformat(), "07:00")]["precio"] == 320


def test_vuelo_que_ya_no_esta_se_reemplaza_por_lo_que_hay_en_vivo():
    cfg = cfg_()
    with db.conexion() as con:
        cid = _corrida(con, [("L1", [_op(115, "06:00")], I, V)])
        nav = NavFalso({"L1": [_op(350, "19:00")]})
        ver.verificar_corrida(con, cfg, "Verano", cid, nav)
        f = _filas(con, cid)
        assert (I.isoformat(), "06:00") not in f          # descartada (verificado = -1): no se muestra
        assert f[(I.isoformat(), "19:00")]["precio"] == 350 and f[(I.isoformat(), "19:00")]["verificado"] == 1
        assert db.minimo_historico(con, "Verano")["precio"] != 115 / 2


def test_reintenta_hasta_que_lo_mas_barato_este_confirmado():
    """Si la más barata se cae, la siguiente pasa a ser la más barata y también se confirma."""
    cfg = cfg_()
    with db.conexion() as con:
        cid = _corrida(con, [("L%d" % k, [_op(100 + k, "06:00")], I + dt.timedelta(k), V + dt.timedelta(k))
                             for k in range(15)])
        vivo = {"L%d" % k: [] for k in range(15)}          # las 3 primeras ya no existen
        vivo.update({"L%d" % k: [_op(100 + k, "06:00")] for k in range(3, 15)})
        nav = NavFalso(vivo)
        ver.verificar_corrida(con, cfg, "Verano", cid, nav)
        filas = db.filas_corrida(con, cid)
        assert filas[0]["precio"] == 103 and filas[0]["verificado"] == 1
        assert len(nav.abiertos) >= 13


def test_dos_pasajes_sueltos_se_confirman_por_tramo():
    cfg = cfg_()
    t_ida = {**_op(100, "06:00"), "link": "IDA", "origen": "AEP", "destino": "BRC", "fecha": I.isoformat(),
             "sentido": "ida"}
    t_vta = {**_op(90, "18:00", "BRC → AEP"), "salida": f"{V} 18:00", "link": "VTA", "origen": "BRC",
             "destino": "AEP", "fecha": V.isoformat(), "sentido": "vuelta"}
    op = {**_op(190), "tipo": "dos_solo_ida", "detalle": {"tramos": [t_ida, t_vta]}, "link": "IDA"}
    with db.conexion() as con:
        cid = _corrida(con, [("IDA", [op], I, V)])
        nav = NavFalso({"IDA": [_op(120, "06:00")], "VTA": [{**_op(95, "18:00", "BRC → AEP"), "salida": f"{V} 18:00"}]})
        ver.verificar_corrida(con, cfg, "Verano", cid, nav)
        f = db.filas_corrida(con, cid)[0]
        assert f["precio"] == 215 and f["verificado"] == 2
        assert [t["precio"] for t in f["detalle"]["tramos"]] == [120, 95]


def test_sin_confirmar_no_manda_mail(monkeypatch):
    cfg = cfg_()
    enviados = []
    monkeypatch.setattr(mailer, "enviar_email", lambda *a, **k: enviados.append(a))
    with db.conexion() as con:
        _corrida(con, [("A", [_op(500)], I, V)], "SinConfirmar")
        cid = _corrida(con, [("B", [_op(200)], I, V)], "SinConfirmar")
        ver.verificar_corrida(con, cfg, "SinConfirmar", cid, NavFalso({}))   # Google no respondió nunca
        assert db.filas_corrida(con, cid)[0]["verificado"] == 0
        assert mailer.detectar_mejora(con, "SinConfirmar", cid) is None
        # con el precio confirmado, sí avisa
        con.execute("UPDATE precios SET verificado=1 WHERE corrida_id=?", (cid,))
        assert mailer.detectar_mejora(con, "SinConfirmar", cid)["motivo"] == "precio"


def test_leer_respuesta_de_google():
    payload = [None, None, None, [[[["", ["JetSMART"], [["", "", "", "EZE", "", "", "NQN", "", [6], "", [7, 57],
                                                       117, *[None] * 8, [2027, 1, 17], [2027, 1, 17]]]],
                                     [[None, 313]]]]]]
    body = ")]}'\n\n123\n" + json.dumps([["wrb.fr", None, json.dumps(payload)]]) + "\n"
    ops = ver.leer_respuesta(body)
    assert ops and ops[0]["precio"] == 313 and ops[0]["salida"] == "2027-01-17 06:00"
    error = ")]}'\n\n1\n" + json.dumps([["wrb.fr", None, None, None, None, [13]]])
    assert ver.leer_respuesta(error) is None


def test_google_contesta_distinto_en_cada_carga_queda_el_mas_alto():
    a = [_op(116, "06:00"), _op(400, "09:00")]
    b = [_op(313, "06:00"), _op(400, "09:00")]
    ops = ver.combinar([a, b, a])
    seis = [o for o in ops if o["salida"].endswith("06:00")][0]
    assert seis["precio"] == 313 and seis["min_visto"] == 116
    assert [o for o in ops if o["salida"].endswith("09:00")][0].get("min_visto") is None
    assert ver.combinar([a]) == a
