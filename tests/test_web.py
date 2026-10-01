
from fastapi.testclient import TestClient

from tests.test_buscador import CONFIG, escribir_config


def cliente():
    escribir_config(CONFIG)
    from src.web.app import app
    return TestClient(app)


def test_salud_e_indice():
    with cliente() as c:
        assert c.get("/api/salud").json()["ok"]
        r = c.get("/")
        assert r.status_code == 200 and "Vuelos baratos" in r.text
        assert c.get("/../config.html").status_code == 404


def test_suscripcion_y_baja(monkeypatch):
    from src.core import suscriptores as subs
    import src.notifications.mailer as mailer
    mails = []
    monkeypatch.setattr(mailer, "enviar_email", lambda para, asunto, html, texto, remitente="": mails.append((para, html)))
    with cliente() as c:
        assert c.get("/suscribirse").status_code == 200
        r = c.post("/suscribirse", data={"email": "malo", "busqueda": "Verano"})
        assert r.status_code == 400
        import src.web.app as w
        w._ultimos_envios.clear()
        r = c.post("/suscribirse", data={"email": "a@b.com", "busqueda": "Verano", "desde": "2027-01-01", "hasta": "2027-01-31"})
        assert r.status_code == 200 and "te llega un mail" in r.text
        assert not subs.de_busqueda("Verano")          # pendiente hasta confirmar
        assert mails and mails[0][0] == "a@b.com"
        import re
        token = re.search(r"token=([^\"&]+)", mails[0][1]).group(1)
        assert "confirmado" in c.get(f"/confirmar?token={token}").text
        s = subs.de_busqueda("Verano")
        assert s and s[0]["desde"] == "2027-01-01"
        assert "no vas a recibir" in c.get(f"/baja?token={s[0]['token']}").text


def test_actualizar_con_token(monkeypatch):
    monkeypatch.setenv("WEB_ADMIN_TOKEN", "secreto")
    import src.web.app as w
    monkeypatch.setattr(w, "ejecutar", lambda **k: {"emails": 0})
    with cliente() as c:
        assert c.post("/api/actualizar", json={"busqueda": "Verano"}).status_code == 401
        assert c.post("/api/actualizar", json={"busqueda": "NoExiste"}, headers={"X-Token": "secreto"}).status_code == 404
        assert c.post("/api/actualizar", json={"busqueda": "Verano"}, headers={"X-Token": "secreto"}).status_code == 202


def test_api_busquedas(monkeypatch):
    import datetime as dt
    import src.web.app as w
    monkeypatch.setattr(w, "_regenerar", lambda: None)
    lanzadas = []
    monkeypatch.setattr(w, "_lanzar", lambda n: lanzadas.append(n))
    año = dt.date.today().year + 1
    f = {"nombre": "Web", "destinos": ["BRC"], "viajar_desde": f"{año}-01-01", "viajar_hasta": f"{año}-01-20",
         "dias_min": 5, "dias_max": 6, "buscar_ahora": True, "estrategia": "ida_vuelta"}
    with cliente() as c:
        est = c.post("/api/busquedas/estimar", json=f).json()
        assert est["rutas"] == 2 and est["consultas"] == est["combinaciones"] * 2
        assert c.post("/api/busquedas", json={**f, "destinos": []}).status_code == 422
        r = c.post("/api/busquedas", json=f)
        assert r.status_code == 201 and r.json()["buscando"] and lanzadas == ["Web"]
        assert any(b["nombre"] == "Web" for b in c.get("/api/busquedas").json())
        assert c.put("/api/busquedas/Web", json={**f, "nombre": "Web 2", "buscar_ahora": False}).status_code == 200
        assert c.post("/api/busquedas/Web 2/estado", json={"activa": False}).status_code == 200
        assert c.delete("/api/busquedas/Web 2").status_code == 200
        assert c.delete("/api/busquedas/Web 2").status_code == 404


def test_suscriptores_cifrados(monkeypatch):
    from src.core import suscriptores as subs
    monkeypatch.setenv("VUELOS_CLAVE", "clave-de-prueba")
    subs.agregar("x@y.com", "Verano", None, None)
    crudo = subs.ruta().read_text(encoding="utf-8")
    assert "x@y.com" not in crudo and "cifrado" in crudo
    assert any(s["email"] == "x@y.com" for s in subs.cargar())
    monkeypatch.delenv("VUELOS_CLAVE")
    assert subs.cargar() == []      # sin la clave no se lee nada


def test_modo_nube_dispara_y_baja(monkeypatch):
    """Render: 'Actualizar' dispara GitHub Actions; al terminar la ejecución baja los datos."""
    import time
    import src.web.app as w
    from src.core import github_sync as gh
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "u/r")
    monkeypatch.setenv("VUELOS_EJECUCION", "github")
    monkeypatch.setattr(w, "_regenerar", lambda: None)
    disparos, bajadas = [], []
    monkeypatch.setattr(gh, "disparar_busqueda", lambda b: disparos.append(b) or (True, ""))
    monkeypatch.setattr(gh, "bajar_archivo", lambda ruta: False)
    monkeypatch.setattr(gh, "sha_datos", lambda: "sha%d" % len(bajadas))
    monkeypatch.setattr(gh, "bajar_datos", lambda d: bajadas.append(d) or True)
    ahora = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    run = {"id": 1, "evento": "workflow_dispatch", "estado": "in_progress", "conclusion": None,
           "creado": ahora, "iniciado": ahora, "url": "https://github.com/u/r/actions/runs/1"}
    monkeypatch.setattr(gh, "ejecuciones", lambda n=5: [dict(run)])
    w.trabajo.corriendo = False
    with cliente() as c:
        assert c.post("/api/actualizar", json={"busqueda": "Verano"}).status_code == 202
        assert disparos == ["Verano"]
        e = c.get("/api/estado").json()
        assert e["corriendo"] and e["nube"] and "nube" in e["progreso"]
        n = len(bajadas)
        run.update(estado="completed", conclusion="success")
        w.nube.consultado = 0
        e = c.get("/api/estado").json()
        assert not e["corriendo"] and not e["error"] and len(bajadas) == n + 1


def test_confirmacion_en_la_nube(monkeypatch):
    """Render no puede usar SMTP: dispara confirmacion.yml con un hash, y Actions manda el mail."""
    import re
    import src.web.app as w
    import src.notifications.mailer as mailer
    from src.core import github_sync as gh
    from src.core import suscriptores as subs
    from src.notifications import confirmacion
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_REPOSITORY", "u/r")
    monkeypatch.setenv("VUELOS_EJECUCION", "github")
    monkeypatch.setattr(w, "_regenerar", lambda: None)
    monkeypatch.setattr(gh, "bajar_archivo", lambda ruta: False)
    monkeypatch.setattr(gh, "sha_datos", lambda: None)
    monkeypatch.setattr(gh, "subir", lambda *a, **k: True)
    disparos, mails = [], []
    monkeypatch.setattr(gh, "disparar_workflow", lambda archivo, inputs: disparos.append((archivo, inputs)) or (True, ""))
    monkeypatch.setattr(mailer, "enviar_email", lambda para, asunto, html, texto, remitente="": mails.append((para, html)))
    with cliente() as c:
        w._ultimos_envios.clear()
        r = c.post("/suscribirse", data={"email": "nube@b.com", "busqueda": "Verano"})
        assert r.status_code == 200 and "te llega un mail" in r.text
    assert not mails and disparos and disparos[0][0] == "confirmacion.yml"
    ref = disparos[0][1]["ref"]
    assert "nube@b.com" not in ref and len(ref) == 24
    monkeypatch.setenv("VUELOS_URL_WEB", "https://web.test")
    assert confirmacion.enviar_pendiente(ref)        # lo que corre en GitHub Actions
    assert mails[0][0] == "nube@b.com" and "https://web.test/confirmar?token=" in mails[0][1]
    token = re.search(r"token=([^\"&]+)", mails[0][1]).group(1)
    subs.confirmar(token)
    assert any(s["email"] == "nube@b.com" for s in subs.de_busqueda("Verano"))
