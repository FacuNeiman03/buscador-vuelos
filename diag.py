import time, json, pathlib
from playwright.sync_api import sync_playwright
from src.core.verificador import leer_respuesta
L = ["https://www.google.com/travel/flights/search?tfs=Gh8SCjIwMjctMDEtMTcoAGDgA2oFEgNFWkVyBRIDTlFOGh8SCjIwMjctMDEtMjUoAGDgA2oFEgNOUU5yBRIDRVpFQgIBAUgBmAEB&hl=es&curr=USD&gl=AR",
     "https://www.google.com/travel/flights/search?tfs=Gh8SCjIwMjctMDEtMTUoAGDgA2oFEgNBRVByBRIDTlFOQgIBAUgBmAEC&hl=es&curr=USD&gl=AR"]
out = []
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", args=["--disable-blink-features=AutomationControlled"])
    c0 = b.new_context(); ua = c0.new_page().evaluate("navigator.userAgent").replace("HeadlessChrome", "Chrome"); c0.close()
    for u in L:
        for n in range(8):
            ctx = b.new_context(locale="es-AR", timezone_id="America/Argentina/Buenos_Aires", user_agent=ua, viewport={"width": 1366, "height": 900})
            pg = ctx.new_page(); t0 = time.time(); ev = []
            pg.on("response", lambda r: ev.append([round(time.time()-t0,1), r.status, r.url[:90]]) if ("google.com/travel" in r.url or "sorry" in r.url or "sgsync" in r.url or "Shopping" in r.url) else None)
            try:
                with pg.expect_response(lambda r: "GetShoppingResults" in r.url, timeout=15000) as ri:
                    pg.goto(u, wait_until="commit", timeout=30000)
                body = ri.value.text(); ops = leer_respuesta(body)
                res = {"ok": ops is not None, "len": len(body), "p": [o["precio"] for o in (ops or [])[:3]]}
            except Exception as e:
                res = {"exc": repr(e)[:120], "url": pg.url[:100]}
            res["t"] = round(time.time()-t0,1); res["ev"] = ev[:6]
            out.append(res); ctx.close()
    b.close()
pathlib.Path("salida").mkdir(exist_ok=True)
pathlib.Path("salida/diag.json").write_text(json.dumps(out, indent=1))
