import re, json, time, pathlib, sys
from playwright.sync_api import sync_playwright
L = [
 "https://www.google.com/travel/flights/search?tfs=Gh8SCjIwMjctMDEtMTcoAGDgA2oFEgNFWkVyBRIDTlFOGh8SCjIwMjctMDEtMjQoAGDgA2oFEgNOUU5yBRIDRVpFQgIBAUgBmAEB&hl=es&curr=USD&gl=AR",
 "https://www.google.com/travel/flights/search?tfs=GhoSCjIwMjctMDMtMDFqBRIDRVpFcgUSA1BWRxoaEgoyMDI3LTAzLTE1agUSA1BWR3IFEgNFWkVCAgEBSAFqBBABGAGYAQE%3D&hl=es&curr=USD&gl=AR",
 "https://www.google.com/travel/flights/search?tfs=Gh8SCjIwMjctMDEtMDEoAGDgA2oFEgNBRVByBRIDTlFOGh8SCjIwMjctMDEtMDcoAGDgA2oFEgNOUU5yBRIDQUVQQgIBAUgBmAEB&hl=es&curr=USD&gl=AR",
]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
V = {
 "chromium_ua": dict(launch={"args": ["--disable-blink-features=AutomationControlled"]}, ua=UA),
 "chrome_headless": dict(launch={"channel": "chrome", "args": ["--disable-blink-features=AutomationControlled"]}, ua=None),
 "chrome_headed": dict(launch={"channel": "chrome", "headless": False, "args": ["--disable-blink-features=AutomationControlled"]}, ua=None),
 "chrome_headed_mismo_ctx": dict(launch={"channel": "chrome", "headless": False, "args": ["--disable-blink-features=AutomationControlled"]}, ua=None, mismo=True),
}
out = pathlib.Path("salida"); out.mkdir(exist_ok=True)
res = {}
with sync_playwright() as p:
    for k in sys.argv[1:]:
        v = V[k]; res[k] = []
        b = p.chromium.launch(**v["launch"])
        ctx = None
        for n in range(6):
            if ctx is None or not v.get("mismo"):
                if ctx: ctx.close()
                kw = dict(locale="es-AR", timezone_id="America/Argentina/Buenos_Aires", viewport={"width": 1366, "height": 900})
                if v["ua"]: kw["user_agent"] = v["ua"]
                ctx = b.new_context(**kw)
                ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            pg = ctx.new_page(); t0 = time.time(); rpc = []
            def on_resp(r, rpc=rpc):
                if "GetShoppingResults" in r.url:
                    try: rpc.append((round(time.time()-t0,1), len(r.text()), "ErrorResponse" in r.text()))
                    except Exception as e: rpc.append(repr(e))
            pg.on("response", on_resp)
            try:
                pg.goto(L[n % 3], wait_until="domcontentloaded", timeout=60000)
                pg.wait_for_timeout(8000)
                h = pg.content()
                res[k].append({"n": n, "rpc": rpc, "ua": pg.evaluate("navigator.userAgent")[-40:], "url": pg.url[:60],
                               "labels": [m[11:16] for m in re.findall(r'aria-label="(A partir de [^"]*)"', h)[:3]]})
            except Exception as e:
                res[k].append({"n": n, "exc": repr(e)[:200]})
            pg.close()
        b.close()
f = out / "res.json"
prev = json.loads(f.read_text()) if f.exists() else {}
prev.update(res); f.write_text(json.dumps(prev, ensure_ascii=False, indent=1))
