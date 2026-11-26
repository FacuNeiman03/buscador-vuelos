import json, time, pathlib, sys
from playwright.sync_api import sync_playwright
U = "https://www.google.com/travel/flights/search?tfs=Gh8SCjIwMjctMDEtMTcoAGDgA2oFEgNFWkVyBRIDTlFOGh8SCjIwMjctMDEtMjQoAGDgA2oFEgNOUU5yBRIDRVpFQgIBAUgBmAEB&hl=es&curr=USD&gl=AR"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
out = pathlib.Path("salida"); out.mkdir(exist_ok=True)
variantes = {
  "ua": dict(launch={"args": ["--disable-blink-features=AutomationControlled"]}, ua=UA),
  "nuevo_headless": dict(launch={"channel": "chromium", "args": ["--disable-blink-features=AutomationControlled"]}, ua=UA),
  "headed_xvfb": dict(launch={"headless": False, "args": ["--disable-blink-features=AutomationControlled"]}, ua=UA),
}
with sync_playwright() as p:
    for k in sys.argv[1:]:
        v = variantes[k]; res = {"rpc": []}
        try:
            b = p.chromium.launch(**v["launch"])
            ctx = b.new_context(locale="es-AR", timezone_id="America/Argentina/Buenos_Aires", user_agent=v["ua"],
                                viewport={"width": 1366, "height": 900})
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            pg = ctx.new_page()
            pg.on("response", lambda r: res["rpc"].append([r.status, r.url[:120]]) if "rpc" in r.url or "Shopping" in r.url else None)
            pg.goto(U, wait_until="domcontentloaded", timeout=60000)
            pg.wait_for_timeout(12000)
            res["labels"] = pg.eval_on_selector_all("[aria-label]", "els => els.map(e => e.getAttribute('aria-label')).filter(t => t && /US\\$/.test(t))")[:40]
            res["error"] = "se ha producido un error" in pg.content()
            pg.screenshot(path=str(out / f"{k}.png"))
            (out / f"{k}.html").write_text(pg.content(), encoding="utf-8")
            b.close()
        except Exception as e:
            res["excepcion"] = repr(e)
        (out / f"{k}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
