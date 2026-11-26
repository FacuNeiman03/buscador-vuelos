import re, json, time, pathlib, sys
from playwright.sync_api import sync_playwright
L = {
 "eze_17_24": "https://www.google.com/travel/flights/search?tfs=Gh8SCjIwMjctMDEtMTcoAGDgA2oFEgNFWkVyBRIDTlFOGh8SCjIwMjctMDEtMjQoAGDgA2oFEgNOUU5yBRIDRVpFQgIBAUgBmAEB&hl=es&curr=USD&gl=AR",
 "china": "https://www.google.com/travel/flights/search?tfs=GhoSCjIwMjctMDMtMDFqBRIDRVpFcgUSA1BWRxoaEgoyMDI3LTAzLTE1agUSA1BWR3IFEgNFWkVCAgEBSAFqBBABGAGYAQE%3D&hl=es&curr=USD&gl=AR",
}
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
out = pathlib.Path("salida"); out.mkdir(exist_ok=True)
for f in out.iterdir(): f.unlink()
with sync_playwright() as p:
    b = p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
    ctx = b.new_context(locale="es-AR", timezone_id="America/Argentina/Buenos_Aires", user_agent=UA,
                        viewport={"width": 1366, "height": 900})
    ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
    L["aep_01_07"] = "https://www.google.com/travel/flights/search?tfs=Gh8SCjIwMjctMDEtMDEoAGDgA2oFEgNBRVByBRIDTlFOGh8SCjIwMjctMDEtMDcoAGDgA2oFEgNOUU5yBRIDQUVQQgIBAUgBmAEB&hl=es&curr=USD&gl=AR"
    for k, u in [(f"{k}_{n}", u) for n in range(3) for k, u in L.items()]:
        pg = ctx.new_page(); t0 = time.time(); rpc = []
        def on_resp(r):
            if "GetShoppingResults" in r.url:
                try: rpc.append({"t": round(time.time()-t0,1), "status": r.status, "body": r.text()})
                except Exception as e: rpc.append({"t": round(time.time()-t0,1), "err": repr(e)})
        pg.on("response", on_resp)
        pg.goto(u, wait_until="domcontentloaded", timeout=60000)
        pg.wait_for_timeout(10000)
        (out / f"{k}_rpc.json").write_text(json.dumps(rpc, ensure_ascii=False), encoding="utf-8")
        h = pg.content()
        (out / f"{k}.txt").write_text(json.dumps({"error": "se ha producido un error" in h, "labels": re.findall(r'aria-label="(A partir de [^"]*)"', h)[:5]}, ensure_ascii=False), encoding="utf-8")
        pg.close()
    b.close()
