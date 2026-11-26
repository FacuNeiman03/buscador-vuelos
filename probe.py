import json, time, pathlib
from playwright.sync_api import sync_playwright
L = {
 "eze_17_24": "https://www.google.com/travel/flights/search?tfs=Gh8SCjIwMjctMDEtMTcoAGDgA2oFEgNFWkVyBRIDTlFOGh8SCjIwMjctMDEtMjQoAGDgA2oFEgNOUU5yBRIDRVpFQgIBAUgBmAEB&hl=es&curr=USD&gl=AR",
 "aep_01_07": "https://www.google.com/travel/flights/search?tfs=Gh8SCjIwMjctMDEtMDEoAGDgA2oFEgNBRVByBRIDTlFOGh8SCjIwMjctMDEtMDcoAGDgA2oFEgNOUU5yBRIDQUVQQgIBAUgBmAEB&hl=es&curr=USD&gl=AR",
}
out = pathlib.Path("salida"); out.mkdir(exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(locale="es-AR", timezone_id="America/Argentina/Buenos_Aires",
                        viewport={"width": 1366, "height": 900})
    for k, u in L.items():
        pg = ctx.new_page(); t0 = time.time()
        pg.goto(u, wait_until="domcontentloaded", timeout=60000)
        res = {"url_final": None, "snap": []}
        for s in (2, 5, 10, 20):
            pg.wait_for_timeout((s - (time.time() - t0)) * 1000 if s > time.time() - t0 else 0)
            labels = pg.eval_on_selector_all("[aria-label]", "els => els.map(e => e.getAttribute('aria-label')).filter(t => t && t.includes('US$'))")
            res["snap"].append({"t": round(time.time() - t0, 1), "labels": labels[:60]})
        res["url_final"] = pg.url
        res["title"] = pg.title()
        (out / f"{k}.html").write_text(pg.content(), encoding="utf-8")
        pg.screenshot(path=str(out / f"{k}.png"), full_page=True)
        (out / f"{k}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
        pg.close()
    b.close()
