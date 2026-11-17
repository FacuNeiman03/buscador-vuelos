"""Avisos de precio objetivo: 'avisame cuando Verano llegue a US$ X'."""
import datetime as dt

from src.core import database as db
from src.core import suscriptores as subs
from src.notifications import mailer
from tests.test_buscador import cfg_

I = dt.date(2027, 1, 12)


def _op(precio_total, escalas=0, salida="06:00"):
    return [{"precio": precio_total, "aerolineas": "JetSMART", "escalas": escalas, "duracion_min": 120,
             "salida": f"{I} {salida}", "llegada": "", "ruta": "AEP → BRC"}]


def _corrida(con, precios_pp, nombre="Verano", extra=()):
    """precios_pp: precio por persona del viaje de 7 días saliendo el 12/01 (2 pasajeros)."""
    cid = db.iniciar_corrida(con, nombre, "t")
    db.guardar_opciones(con, cid, nombre, "x", "AEP", "BRC", I, I + dt.timedelta(7), _op(precios_pp * 2),
                        "USD", 2, "l", flexible=False)
    for (ida, vta, pp, esc, flex) in extra:
        db.guardar_opciones(con, cid, nombre, "x", "AEP", "BRC", ida, vta, _op(pp * 2, esc), "USD", 2, "l",
                            flexible=flex)
    db.finalizar_corrida(con, cid, 1, 0, db.COMPLETA)
    return cid


def _aviso(email, **av):
    aviso = {"precio_max": 80, "vence": None, "dias_min": None, "dias_max": None, "solo_directos": False,
             "equipaje": None, "repetir": "bajada", **av}
    tok, _ = subs.agregar(email, "Verano", None, None, confirmado=True, tipo="precio", aviso=aviso)
    return tok


def _mails(monkeypatch):
    enviados = []
    monkeypatch.setattr(mailer, "enviar_email", lambda para, asunto, html, texto, remitente="": enviados.append((para, asunto, html)))
    return enviados


def test_bajada_avisa_al_llegar_y_cada_vez_que_baja(monkeypatch):
    cfg = cfg_()
    _aviso("a@x.com")
    mails = _mails(monkeypatch)
    with db.conexion() as con:
        for pp in (95, 80, 80, 78, 85, 70):
            mailer.avisos_de_precio(con, cfg, "Verano", _corrida(con, pp))
    mails = [m for m in mails if m[0] == "a@x.com"]
    precios = [m[1] for m in mails]
    assert len(mails) == 3, precios               # 80 (llegó), 78 (bajó más), 70 (bajó más)
    assert "US$ 80" in precios[0] and "llegó a tu precio" in precios[0]
    assert "US$ 70" in precios[2]
    assert "Tu precio objetivo" in mails[0][2]


def test_diario_y_una_vez(monkeypatch):
    cfg = cfg_()
    tok_d = _aviso("d@x.com", repetir="diario")
    tok_u = _aviso("u@x.com", repetir="una_vez")
    mails = _mails(monkeypatch)
    with db.conexion() as con:
        mailer.avisos_de_precio(con, cfg, "Verano", _corrida(con, 70))
        mailer.avisos_de_precio(con, cfg, "Verano", _corrida(con, 65))   # mismo día
        a_d = [m for m in mails if m[0] == "d@x.com"]
        a_u = [m for m in mails if m[0] == "u@x.com"]
        assert len(a_d) == 1, "diario: una vez por día"
        assert len(a_u) == 1, "una sola vez"
        assert not any(s["token"] == tok_u for s in subs.de_busqueda("Verano")), "queda desactivado"
        # al otro día, el diario vuelve a avisar
        con.execute("UPDATE notificaciones SET enviado='2000-01-01 10:00' WHERE aviso=?", (tok_d,))
        mailer.avisos_de_precio(con, cfg, "Verano", _corrida(con, 70))
    assert len([m for m in mails if m[0] == "d@x.com"]) == 2


def test_vencido_no_avisa(monkeypatch):
    cfg = cfg_()
    _aviso("v@x.com", vence="2020-01-01")
    mails = _mails(monkeypatch)
    with db.conexion() as con:
        mailer.avisos_de_precio(con, cfg, "Verano", _corrida(con, 50))
    assert not [m for m in mails if m[0] == "v@x.com"]


def test_condiciones_directos_y_dias(monkeypatch):
    cfg = cfg_()
    _aviso("c@x.com", precio_max=60, solo_directos=True, dias_min=5, dias_max=6)
    mails = _mails(monkeypatch)
    with db.conexion() as con:
        # 7 días a 50 (no cumple duración) · 6 días con escala a 40 (no es directo) · 6 días directo a 58 ✓
        extra = [(I, I + dt.timedelta(6), 40, 1, True), (I + dt.timedelta(1), I + dt.timedelta(7), 58, 0, True)]
        mailer.avisos_de_precio(con, cfg, "Verano", _corrida(con, 50, extra=extra))
    mails = [m for m in mails if m[0] == "c@x.com"]
    assert len(mails) == 1 and "US$ 58" in mails[0][1]
    assert "solo directos" in mails[0][2] and "5 a 6 días" in mails[0][2]


def test_aviso_de_precio_no_recibe_alertas_de_minimo():
    cfg = cfg_()
    _aviso("solo-precio@x.com")
    subs.agregar("minimo@x.com", "Verano", None, None, confirmado=True)
    with db.conexion() as con:
        emails = {d["email"] for d in mailer.destinatarios(con, cfg, "Verano")}
    assert "minimo@x.com" in emails and "solo-precio@x.com" not in emails


def test_formulario_web_crea_aviso(monkeypatch):
    from fastapi.testclient import TestClient
    import src.web.app as w
    from tests.test_buscador import escribir_config
    escribir_config()
    monkeypatch.setattr(w, "_mail_confirmacion", lambda *a, **k: None)
    with TestClient(w.app) as c:
        assert "Cuando llegue a un precio" in c.get("/suscribirse?busqueda=Verano&tipo=precio").text
        w._ultimos_envios.clear()
        r = c.post("/suscribirse", data={"email": "web@x.com", "busqueda": "Verano", "tipo": "precio",
                                         "precio_max": "75,5", "duracion": "15", "repetir": "una_vez",
                                         "dias_min": "5", "dias_max": "8", "solo_directos": "1"})
        assert r.status_code == 200 and "te llega un mail" in r.text
        w._ultimos_envios.clear()
        r = c.post("/suscribirse", data={"email": "web2@x.com", "busqueda": "Verano", "tipo": "precio",
                                         "precio_max": "-3"})
        assert r.status_code == 400
    s = next(x for x in subs.cargar() if x["email"] == "web@x.com")
    av = s["aviso"]
    assert s["tipo"] == "precio" and av["precio_max"] == 75.5 and av["repetir"] == "una_vez"
    assert av["vence"] == (dt.date.today() + dt.timedelta(15)).isoformat()
    assert av["dias_min"] == 5 and av["dias_max"] == 8 and av["solo_directos"]
