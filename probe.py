import json, time, pathlib
from src.core.verificador import Navegador
L = {
 "rt": "https://www.google.com/travel/flights/search?tfs=Gh8SCjIwMjctMDEtMTcoAGDgA2oFEgNFWkVyBRIDTlFOGh8SCjIwMjctMDEtMjQoAGDgA2oFEgNOUU5yBRIDRVpFQgIBAUgBmAEB&hl=es&curr=USD&gl=AR",
 "china": "https://www.google.com/travel/flights/search?tfs=GhoSCjIwMjctMDMtMDFqBRIDRVpFcgUSA1BWRxoaEgoyMDI3LTAzLTE1agUSA1BWR3IFEgNFWkVCAgEBSAFqBBABGAGYAQE%3D&hl=es&curr=USD&gl=AR",
 "ow": "https://www.google.com/travel/flights/search?tfs=Gh8SCjIwMjctMDEtMTcoAGDgA2oFEgNFWkVyBRIDTlFOQgIBAUgBmAEC&hl=es&curr=USD&gl=AR",
 "rt_mano": "https://www.google.com/travel/flights/search?tfs=Gh8SCjIwMjctMDEtMTcoAGDgA2oFEgNFWkVyBRIDTlFOGh8SCjIwMjctMDEtMjQoAGDgA2oFEgNOUU5yBRIDRVpFQgIBAUgBagQQARgAmAEB&hl=es&curr=USD&gl=AR",
}
out = pathlib.Path("salida"); out.mkdir(exist_ok=True)
res = {}
t00 = time.time()
with Navegador() as nav:
    for ronda in range(3):
        for k, u in L.items():
            nav.cache.clear(); t0 = time.time()
            ops = nav.opciones(u)
            res[f"{k}_{ronda}"] = {"s": round(time.time() - t0, 1), "ops": [(o["precio"], o["aerolineas"], o["salida"], o["ruta"]) for o in (ops or [])[:3]]}
    res["cargas"], res["fallidas"], res["total_s"] = nav.cargas, nav.fallidas, round(time.time() - t00)
(out / "nav.json").write_text(json.dumps(res, ensure_ascii=False, indent=1))
