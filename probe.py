import re, json, time, pathlib, sys
from playwright.sync_api import sync_playwright
L = {
 "rt": "https://www.google.com/travel/flights/search?tfs=Gh8SCjIwMjctMDEtMTcoAGDgA2oFEgNFWkVyBRIDTlFOGh8SCjIwMjctMDEtMjQoAGDgA2oFEgNOUU5yBRIDRVpFQgIBAUgBmAEB&hl=es&curr=USD&gl=AR",
 "china": "https://www.google.com/travel/flights/search?tfs=GhoSCjIwMjctMDMtMDFqBRIDRVpFcgUSA1BWRxoaEgoyMDI3LTAzLTE1agUSA1BWR3IFEgNFWkVCAgEBSAFqBBABGAGYAQE%3D&hl=es&curr=USD&gl=AR",
 "ow": "https://www.google.com/travel/flights/search?tfs=Gh8SCjIwMjctMDEtMTcoAGDgA2oFEgNFWkVyBRIDTlFOQgIBAUgBmAEC&hl=es&curr=USD&gl=AR",
 "rt_mano": "https://www.google.com/travel/flights/search?tfs=Gh8SCjIwMjctMDEtMTcoAGDgA2oFEgNFWkVyBRIDTlFOGh8SCjIwMjctMDEtMjQoAGDgA2oFEgNOUU5yBRIDRVpFQgIBAUgBagQQARgAmAEB&hl=es&curr=USD&gl=AR",
}
out = pathlib.Path("salida"); out.mkdir(exist_ok=True)
log = {}
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", args=["--disable-blink-features=AutomationControlled"])
    for k, u in L.items():
        t0 = time.time(); log[k] = []
        for n in range(10):
            ctx = b.new_context(locale="es-AR", timezone_id="America/Argentina/Buenos_Aires", viewport={"width": 1366, "height": 900})
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            pg = ctx.new_page()
            try:
                with pg.expect_response(lambda r: "GetShoppingResults" in r.url, timeout=20000) as ri:
                    pg.goto(u, wait_until="commit", timeout=30000)
                body = ri.value.text()
            except Exception as e:
                body = "EXC " + repr(e)[:100]
            ok = "ErrorResponse" not in body and not body.startswith("EXC")
            log[k].append([n, round(time.time() - t0, 1), ok, len(body)])
            if ok:
                (out / f"{k}_body.txt").write_text(body, encoding="utf-8")
                pg.wait_for_timeout(2500)
                (out / f"{k}.txt").write_text("\n".join(re.findall(r'aria-label="(A partir de [^"]*)"', pg.content())), encoding="utf-8")
            ctx.close()
            if ok: break
    b.close()
(out / "log.json").write_text(json.dumps(log, indent=1))
