#!/usr/bin/env python3
"""Regenera src/core/aeropuertos.json desde OurAirports (dominio público, https://ourairports.com/data/).

Solo hace falta correrlo si querés actualizar la lista (cambia muy poco):
    python scripts/generar_aeropuertos.py
Incluye aeropuertos con vuelos comerciales regulares y código IATA. Los nombres de países salen en
castellano si está instalado `babel` (pip install babel); si no, en inglés.
"""
from __future__ import annotations

import csv
import io
import json
import sys
import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/davidmegginson/ourairports-data/main/"
DESTINO = Path(__file__).resolve().parent.parent / "src" / "core" / "aeropuertos.json"
TIPOS = {"large_airport": "L", "medium_airport": "M", "small_airport": "S"}

# Ciudades en castellano (y áreas metropolitanas con varios aeropuertos) -> códigos IATA
METROS = {
    "Buenos Aires": ["AEP", "EZE"], "Cipolletti": ["NQN"], "Bariloche": ["BRC"], "San Martín de los Andes": ["CPC"],
    "Montevideo": ["MVD"], "Punta del Este": ["PDP", "MVD"], "Santiago de Chile": ["SCL"], "Lima": ["LIM"],
    "Cusco": ["CUZ"], "Bogotá": ["BOG"], "Cartagena": ["CTG"], "Panamá": ["PTY"], "Cancún": ["CUN"],
    "Ciudad de México": ["MEX", "NLU"], "La Habana": ["HAV"], "Punta Cana": ["PUJ"],
    "San Pablo": ["GRU", "CGH", "VCP"], "São Paulo": ["GRU", "CGH", "VCP"], "Río de Janeiro": ["GIG", "SDU"],
    "Florianópolis": ["FLN"], "Salvador de Bahía": ["SSA"],
    "Nueva York": ["JFK", "EWR", "LGA"], "Miami": ["MIA", "FLL"], "Orlando": ["MCO"], "Chicago": ["ORD", "MDW"],
    "Los Ángeles": ["LAX"], "San Francisco": ["SFO", "OAK", "SJC"], "Las Vegas": ["LAS"],
    "Washington": ["IAD", "DCA", "BWI"], "Toronto": ["YYZ"], "Montreal": ["YUL"],
    "Madrid": ["MAD"], "Barcelona": ["BCN"], "Sevilla": ["SVQ"], "Lisboa": ["LIS"], "Oporto": ["OPO"],
    "París": ["CDG", "ORY"], "Londres": ["LHR", "LGW", "STN", "LTN", "LCY"], "Dublín": ["DUB"], "Edimburgo": ["EDI"],
    "Ámsterdam": ["AMS"], "Bruselas": ["BRU", "CRL"], "Fráncfort": ["FRA"], "Múnich": ["MUC"], "Berlín": ["BER"],
    "Zúrich": ["ZRH"], "Ginebra": ["GVA"], "Viena": ["VIE"], "Praga": ["PRG"], "Budapest": ["BUD"],
    "Varsovia": ["WAW", "WMI"], "Cracovia": ["KRK"], "Copenhague": ["CPH"], "Estocolmo": ["ARN"], "Oslo": ["OSL"],
    "Roma": ["FCO", "CIA"], "Milán": ["MXP", "LIN", "BGY"], "Venecia": ["VCE", "TSF"], "Florencia": ["FLR", "PSA"],
    "Nápoles": ["NAP"], "Atenas": ["ATH"], "Estambul": ["IST", "SAW"], "Moscú": ["SVO", "DME", "VKO"],
    "Marrakech": ["RAK"], "El Cairo": ["CAI"], "Ciudad del Cabo": ["CPT"], "Johannesburgo": ["JNB"],
    "Dubái": ["DXB"], "Abu Dabi": ["AUH"], "Doha": ["DOH"], "Nueva Delhi": ["DEL"], "Bombay": ["BOM"],
    "Katmandú": ["KTM"], "Bangkok": ["BKK", "DMK"], "Phuket": ["HKT"], "Singapur": ["SIN"], "Hong Kong": ["HKG"],
    "Pekín": ["PEK", "PKX"], "Shanghái": ["PVG", "SHA"], "Seúl": ["ICN", "GMP"], "Taipéi": ["TPE", "TSA"],
    "Tokio": ["HND", "NRT"], "Osaka": ["KIX", "ITM"], "Sídney": ["SYD"], "Melbourne": ["MEL"], "Auckland": ["AKL"],
}

# Aeropuertos con más tráfico (orden aproximado por pasajeros). Al elegir un país se preseleccionan
# primero estos: sin este orden, la lista salía alfabética y en China quedaban afuera Shanghái y Shenzhen.
PRINCIPALES = """
    ATL DFW DEN ORD LAX JFK LAS MCO MIA CLT SEA PHX EWR SFO IAH BOS FLL MSP LGA DTW
    PHL SLC BWI DCA SAN IAD TPA BNA AUS MDW HNL YYZ YVR YUL YYC MEX CUN GDL MTY TIJ
    SJD PVR PTY SJO SJU PUJ SDQ HAV MBJ NAS GRU BOG LIM SCL GIG BSB CGH VCP CNF MDE
    CTG CLO UIO GYE EZE AEP MVD ASU VVI LPB CCS REC SSA POA FOR CWB FLN BEL MAO NAT
    MCZ IGU COR MDZ BRC NQN USH FTE IGR SLA TUC CUZ PDP LHR IST CDG AMS MAD FRA BCN
    LGW FCO MUC SAW PMI DUB LIS ORY ZRH CPH OSL ARN VIE ATH MXP BER BRU MAN STN HEL
    WAW PRG BUD OPO AGP NCE VCE NAP GVA HAM DUS LTN EDI KEF OTP SVO DME DXB DOH AUH
    RUH JED TLV AMM MCT KWI BAH JNB CPT CAI ADD NBO CMN LOS ACC DSS RAK ALG TUN HND
    NRT KIX CTS FUK OKA NGO ICN GMP PUS CJU PEK PKX PVG SHA CAN SZX CTU TFU CKG KMG
    XIY HGH WUH NKG CSX XMN TAO CGO SYX HAK TSN SHE DLC HRB URC FOC NNG KWE LHW TYN
    HFE NGB WNZ HKG MFM TPE KHH BKK DMK HKT CNX KBV USM SIN KUL PEN BKI JHB CGK DPS
    SUB MNL CEB SGN HAN DAD CXR PQC RGN DEL BOM BLR MAA HYD CCU COK GOI KTM CMB MLE
    DAC ISB KHI LHE TAS ALA SYD MEL BNE PER AKL ADL CHC NAN PPT
""".split()


def descargar(nombre: str) -> list[dict]:
    with urllib.request.urlopen(BASE + nombre, timeout=120) as r:
        return list(csv.DictReader(io.StringIO(r.read().decode("utf-8"))))


def nombres_paises(paises: list[dict]) -> dict[str, str]:
    nombres = {p["code"]: p["name"] for p in paises}
    try:
        from babel import Locale
        es = Locale("es").territories
        nombres.update({k: es[k] for k in nombres if k in es})
    except ImportError:
        print("Aviso: sin babel los países quedan en inglés (pip install babel)", file=sys.stderr)
    return nombres


def main() -> None:
    aeropuertos = descargar("airports.csv")
    paises = nombres_paises(descargar("countries.csv"))
    filas, continentes = [], {}
    for a in aeropuertos:
        if a["scheduled_service"] != "yes" or not a["iata_code"] or a["type"] not in TIPOS:
            continue
        if len(a["iata_code"]) != 3 or not a["iata_code"].isalpha():
            continue
        continentes.setdefault(a["iso_country"], []).append(a["continent"])
        filas.append([a["iata_code"].upper(), a["municipality"] or a["name"], a["name"], a["iso_country"],
                      TIPOS[a["type"]]])
    filas.sort(key=lambda f: (f[3], "LMS".index(f[4]), f[1]))
    usados = {f[3] for f in filas}
    datos = {
        "fuente": "OurAirports (dominio público) — https://ourairports.com/data/",
        "campos": ["iata", "ciudad", "nombre", "pais", "tipo (L=grande, M=mediano, S=chico)"],
        "paises": {k: v for k, v in sorted(paises.items()) if k in usados},
        # continente mayoritario del país (ej: España = EU aunque Canarias figure en AF)
        "continentes": {k: max(set(v), key=v.count) for k, v in sorted(continentes.items())},
        "metros": {k: [c for c in v if c in {f[0] for f in filas}] for k, v in METROS.items()},
        "principales": [c for c in PRINCIPALES if c in {f[0] for f in filas}],
        "aeropuertos": filas,
    }
    DESTINO.write_text(json.dumps(datos, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"{len(filas)} aeropuertos de {len(datos['paises'])} países -> {DESTINO}")


if __name__ == "__main__":
    main()
