"""Alertas por email con detección de mejoras (sin spam).

Regla de envío, por destinatario y búsqueda (y dentro de sus fechas preferidas, si las definió):
  * Se toma la mejor opción de la corrida de hoy (precio/persona, y a igual precio menos escalas
    y menor duración).
  * Se compara con el MÍNIMO REGISTRADO en todas las corridas anteriores.
      - Bajó el precio                                   -> SE ENVÍA
      - Mismo precio pero menos escalas o vuelo más corto -> SE ENVÍA
      - Mismo precio y mismas condiciones, o subió       -> NO se envía
  * La primera corrida de una búsqueda solo fija la referencia (no se envía nada).
  * Nunca se manda dos veces el mismo aviso para la misma corrida (tabla `notificaciones`).

Proveedores de envío (el primero configurado gana):
  1. Resend  -> RESEND_API_KEY (+ RESEND_FROM, ej: "Vuelos <alertas@tudominio.com>")
  2. SMTP    -> SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD (+ SMTP_FROM)
               Gmail: SMTP_HOST=smtp.gmail.com, SMTP_PORT=587 y una "contraseña de aplicación".
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import os
import smtplib
import sqlite3
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage
from email.utils import formataddr, parseaddr
from string import Template

from ..core import database as db
from ..core import suscriptores as subs
from ..core.config import EQUIPAJES, AppConfig
from ..core.paths import PLANTILLA_EMAIL
from ..reporting.metrics import agregar_horarios, estadisticas, filtro_rango, fmt_min, mejores_por_combo

log = logging.getLogger("vuelos")
TOLERANCIA_PRECIO = 0.5      # diferencias menores a medio dólar/peso se consideran "mismo precio"
TOLERANCIA_DURACION = 10     # minutos: menos que esto no cuenta como "vuelo más corto"


# ============================================================================
# Detección de mejoras
# ============================================================================
def _en_ventana(f: dict, desde: str | None, hasta: str | None) -> bool:
    return (not desde or f["ida"] >= desde) and (not hasta or f["ida"] <= hasta)


def _mejores_de_corrida(con: sqlite3.Connection, corrida_id: int, desde: str | None, hasta: str | None,
                        filtro=None) -> list[dict]:
    filas = []
    for f in db.filas_corrida(con, corrida_id):
        if filtro and not filtro(f):
            continue
        f = dict(f)
        f["precio"] = f["precio"] / (f["pasajeros"] or 1)
        f["total"] = f["precio"] * (f["pasajeros"] or 1)
        f["flexible"] = f.get("flexible") or 0
        if _en_ventana(f, desde, hasta):
            filas.append(f)
    agregar_horarios(con, corrida_id, filas)
    base, _ = mejores_por_combo(filas)
    return base


def confirmado(con: sqlite3.Connection, corrida_id: int, fila: dict | None) -> bool:
    """Si la corrida pasó por la confirmación en vivo, solo se avisa con precios confirmados (iguales o
    corregidos). Sin navegador (ej. corrida local sin Chrome) se avisa como antes."""
    if not fila:
        return False
    r = con.execute("SELECT COALESCE(verificada,0) FROM corridas WHERE id=?", (corrida_id,)).fetchone()
    if not r or not r[0]:
        return True
    if fila.get("verificado") in (1, 2):
        return True
    log.info(f"  ✉ No aviso {fila.get('ida')} → {fila.get('vuelta')}: el precio no se pudo confirmar en vivo.")
    return False


def detectar_mejora(con: sqlite3.Connection, busqueda: str, corrida_id: int, desde: str | None = None,
                    hasta: str | None = None, avisar_condiciones: bool = True, filtro=None) -> dict | None:
    """Devuelve la info para el email si hay una mejora real; None si no hay que avisar.

    `filtro(fila)`: rango vigente y nivel de equipaje de la búsqueda; así un precio viejo de junio no bloquea
    los avisos de una búsqueda que ahora está acotada a enero-marzo.
    """
    base = _mejores_de_corrida(con, corrida_id, desde, hasta, filtro)
    if not base:
        return None
    hoy = base[0]
    if not confirmado(con, corrida_id, hoy):
        return None
    ref = db.minimo_historico(con, busqueda, antes_de_corrida=corrida_id, desde=desde, hasta=hasta, filtro=filtro)
    if ref is None:
        return None   # primera corrida (o primera vez con datos en esa ventana): solo referencia

    # corrida anterior (para "ahorro vs ayer" y para avisar cuando vuelve al mínimo)
    anteriores = [c for c in db.corridas_utiles(con, busqueda) if c["id"] < corrida_id]
    prev_best = None
    if anteriores:
        pb = _mejores_de_corrida(con, anteriores[-1]["id"], desde, hasta, filtro)
        prev_best = pb[0]["precio"] if pb else None

    motivo = None
    if hoy["precio"] < ref["precio"] - TOLERANCIA_PRECIO:
        motivo = "precio"
    elif abs(hoy["precio"] - ref["precio"]) <= TOLERANCIA_PRECIO and prev_best is not None \
            and hoy["precio"] < prev_best - TOLERANCIA_PRECIO:
        motivo = "minimo"   # había subido y volvió a bajar al precio más bajo que se vio
    elif avisar_condiciones and abs(hoy["precio"] - ref["precio"]) <= TOLERANCIA_PRECIO:
        if (hoy["escalas"] or 0) < (ref["escalas"] or 0):
            motivo = "escalas"
        elif (hoy["escalas"] or 0) == (ref["escalas"] or 0) and hoy["duracion_min"] and ref["duracion_min"] \
                and hoy["duracion_min"] < ref["duracion_min"] - TOLERANCIA_DURACION:
            motivo = "duracion"
    if not motivo:
        return None
    stats = estadisticas([c["precio"] for c in base])
    return {"busqueda": busqueda, "corrida_id": corrida_id, "motivo": motivo, "hoy": hoy, "ref": ref,
            "prev_best": prev_best, "stats": stats, "desde": desde, "hasta": hasta}


# ============================================================================
# Composición del email
# ============================================================================
TIPOS_TXT = {"ida_vuelta": "pasaje de ida y vuelta", "dos_solo_ida": "dos pasajes de solo ida",
             "escala_separada": "escala armada con pasajes separados"}
MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]


def _fd(iso: str | None) -> str:
    if not iso:
        return "-"
    d = dt.date.fromisoformat(iso[:10])
    return f"{DIAS[d.weekday()]} {d.day} {MESES[d.month - 1]} {d.year}"


def _money(v: float | None, cur: str) -> str:
    if v is None:
        return "—"
    n = f"{round(v):,}".replace(",", ".")
    return f"US$ {n}" if cur == "USD" else f"{cur} {n}"


def componer_email(info: dict, meta: dict | None, url_reporte: str = "", url_baja: str = "") -> tuple[str, str, str]:
    """Devuelve (asunto, html, texto_plano)."""
    h, ref = info["hoy"], info["ref"]
    cur = h.get("moneda") or "USD"
    pax = int(h.get("pasajeros") or 1)
    nombre = info["busqueda"]
    ida_vuelta = bool(h.get("vuelta"))
    dias = (dt.date.fromisoformat(h["vuelta"]) - dt.date.fromisoformat(h["ida"])).days if ida_vuelta else None
    esc = html.escape

    if info["motivo"] == "objetivo":
        obj = info["objetivo"]
        etiqueta, color = "Llegó a tu precio", "#0a7d35"
        titulo = f"{nombre}: {_money(h['precio'], cur)} por persona"
        dif = obj - h["precio"]
        ahorro_linea = (f"Tu aviso era a {_money(obj, cur)} o menos"
                        + (f": está {_money(dif, cur)} por debajo" if dif >= 1 else ""))
        if info.get("ultimo_enviado") is not None:
            ahorro_linea += f" · ▼ {_money(info['ultimo_enviado'] - h['precio'], cur)} menos que el último aviso"
        asunto = f"🎯 {nombre}: {_money(h['precio'], cur)} x persona — llegó a tu precio ({_money(obj, cur)})"
    elif info["motivo"] == "precio":
        ahorro = ref["precio"] - h["precio"]
        etiqueta, color = "Bajó el precio", "#0a7d35"
        titulo = f"{nombre}: {_money(h['precio'], cur)} por persona"
        ahorro_linea = f"▼ {_money(ahorro, cur)} menos que el mínimo que habíamos visto ({_money(ref['precio'], cur)})"
        asunto = f"✈️ {nombre}: bajó a {_money(h['precio'], cur)} x persona (-{_money(ahorro, cur)})"
    elif info["motivo"] == "minimo":
        ahorro = (info["prev_best"] or h["precio"]) - h["precio"]
        etiqueta, color = "Volvió al precio más bajo", "#0a7d35"
        titulo = f"{nombre}: {_money(h['precio'], cur)} por persona"
        ahorro_linea = (f"▼ {_money(ahorro, cur)} menos que la búsqueda anterior ({_money(info['prev_best'], cur)}) "
                        f"· igual al mínimo que habíamos visto")
        asunto = f"✈️ {nombre}: volvió a {_money(h['precio'], cur)} x persona (mínimo histórico)"
    else:
        etiqueta, color = "Mejores condiciones al mismo precio", "#2a78d6"
        titulo = f"{nombre}: mismo precio, mejor vuelo"
        if info["motivo"] == "escalas":
            ahorro_linea = f"{h['escalas']} escala(s) en vez de {ref['escalas']} por {_money(h['precio'], cur)}"
        else:
            ahorro_linea = f"{fmt_min(h['duracion_min'])} de viaje en vez de {fmt_min(ref['duracion_min'])} por {_money(h['precio'], cur)}"
        asunto = f"✈️ {nombre}: {ahorro_linea}"

    filas = []
    def fila(etq: str, val: str, color_v: str = "#0b0b0b") -> str:
        return (f'<tr><td style="padding:7px 0;border-bottom:1px solid #ecebe6;color:#52514e">{esc(etq)}</td>'
                f'<td align="right" style="padding:7px 0;border-bottom:1px solid #ecebe6;font-weight:600;color:{color_v}">{esc(val)}</td></tr>')
    if info["motivo"] == "objetivo":
        filas.append(fila("Tu precio objetivo", _money(info["objetivo"], cur)))
        if ref:
            filas.append(fila("Mínimo visto antes de hoy", f"{_money(ref['precio'], cur)} · {_fd(ref['ida'])}"))
        if info.get("condiciones"):
            filas.append(fila("Condiciones del aviso", info["condiciones"]))
        if info.get("vence"):
            filas.append(fila("Aviso activo hasta", _fd(info["vence"])))
        if info.get("repetir") == "una_vez":
            filas.append(fila("Este aviso era de una sola vez", "ya quedó desactivado"))
    else:
        filas.append(fila("Mínimo registrado antes de hoy", f"{_money(ref['precio'], cur)} · {_fd(ref['ida'])}"))
    if info["prev_best"] is not None:
        d = h["precio"] - info["prev_best"]
        txt = "igual" if abs(d) < 1 else f"{'▼' if d < 0 else '▲'} {_money(abs(d), cur)} (ayer {_money(info['prev_best'], cur)})"
        filas.append(fila("vs corrida anterior", txt, "#0a7d35" if d < 0 else "#0b0b0b"))
    if h.get("ida_hora") or h.get("vta_hora"):
        hs = f"ida {h['ida_hora'] or '—'}"
        if ida_vuelta:
            hs += f" · vuelta {h['vta_hora'] or '—'}" + (" (aprox.)" if h.get("vta_aprox") else "")
        filas.append(fila("Salida", hs))
    st = info["stats"]
    if st["promedio"]:
        filas.append(fila("Promedio de hoy (todas las fechas)", _money(st["promedio"], cur)))
        ahorro_prom = st["promedio"] - h["precio"]
        if ahorro_prom > 0:
            filas.append(fila("Ahorro vs promedio", f"{_money(ahorro_prom, cur)} ({100 * ahorro_prom / st['promedio']:.0f}%)", "#0a7d35"))
        filas.append(fila("Rango de hoy", f"{_money(st['minimo'], cur)} – {_money(st['maximo'], cur)}"))
    tramos = (h.get("detalle") or {}).get("tramos") or []
    pax_ = int(h.get("pasajeros") or 1)
    if tramos:
        filas.append(fila("Cómo se arma", TIPOS_TXT.get(h.get("tipo"), "") + f" · {len(tramos)} pasajes separados"))
        for t in tramos:
            filas.append(
                f'<tr><td style="padding:7px 0;border-bottom:1px solid #ecebe6;color:#52514e">'
                f'{"Ida" if t["sentido"] == "ida" else "Vuelta"} {esc(t["origen"])} → {esc(t["destino"])} · '
                f'{_fd(t["fecha"])} {esc(t["salida"][11:])}<br><span style="font-size:12px">{esc(t["aerolineas"])}</span></td>'
                f'<td align="right" style="padding:7px 0;border-bottom:1px solid #ecebe6;font-weight:600">'
                f'<a href="{esc(t["link"])}" style="color:#2a78d6">{esc(_money(t["precio"] / pax_, cur))} ↗</a></td></tr>')
        if (h.get("detalle") or {}).get("aviso"):
            filas.append(f'<tr><td colspan="2" style="padding:10px 12px;background:#fff4d6;color:#6b4e00;font-size:13px;'
                         f'border-radius:8px">⚠️ {esc(h["detalle"]["aviso"])}</td></tr>')
    if h.get("sin_equipaje") is not None:
        filas.append(fila("Sin equipaje costaría", _money(h["sin_equipaje"], cur)))
    if info.get("desde") or info.get("hasta"):
        filas.append(fila("Tus fechas preferidas", f"{_fd(info.get('desde')) if info.get('desde') else '…'} → {_fd(info.get('hasta')) if info.get('hasta') else '…'}"))

    ruta_txt = f"{h['origen']} {'⇄' if ida_vuelta else '→'} {h['destino']}"
    if meta and meta.get("origenes"):
        ruta_txt = f"{'/'.join(meta['origenes'])} {'⇄' if ida_vuelta else '→'} {'/'.join(meta['destinos'])}"
    valores = {
        "asunto": esc(asunto), "preheader": esc(ahorro_linea), "color": color, "etiqueta": esc(etiqueta),
        "titulo": esc(titulo), "ruta": esc(ruta_txt), "tipo_viaje": "ida y vuelta" if ida_vuelta else "solo ida",
        "precio": esc(_money(h["precio"], cur)), "ahorro_linea": esc(ahorro_linea),
        "fechas": esc(_fd(h["ida"]) + (f" → {_fd(h['vuelta'])}" if ida_vuelta else "")),
        "dias": esc(f"{dias} días · sale de {h['origen']}" if dias is not None else f"sale de {h['origen']}"),
        "aerolineas": esc(h.get("aerolineas") or "-"),
        "escalas": "directo" if not h["escalas"] else f"{h['escalas']} escala{'s' if h['escalas'] > 1 else ''}",
        "duracion": esc(fmt_min(h["duracion_min"])), "itinerario": esc(h.get("ruta") or ""),
        "filas_comparativa": "".join(filas), "link": esc(h.get("link") or "https://www.google.com/travel/flights"),
        "boton": "Ver el primer pasaje en Google Flights" if tramos else "Ver y reservar en Google Flights",
        "link_reporte": (f'<div style="margin-top:12px"><a href="{esc(url_reporte)}" style="color:#2a78d6;font-size:14px">Ver el reporte completo</a></div>'
                         if url_reporte else ""),
        "pasajeros": pax, "total": esc(_money(h["precio"] * pax, cur)),
        "pie": esc("Recibís este aviso porque el pasaje llegó al precio que pediste. " + {
            "una_vez": "Era de una sola vez: no te vamos a volver a escribir por este aviso.",
            "diario": "Te escribimos como mucho una vez por día mientras siga en ese precio o menos.",
        }.get(info.get("repetir"), "Te volvemos a escribir solo si baja todavía más.")) if info["motivo"] == "objetivo"
        else esc("Recibís este aviso porque bajó el precio o mejoraron las condiciones respecto de lo que ya "
                 "habíamos visto; si no cambia nada, no te escribimos."),
        "baja": (f'<br><a href="{esc(url_baja)}" style="color:#8a8984">Dejar de recibir estas alertas</a>' if url_baja else ""),
    }
    cuerpo = Template(PLANTILLA_EMAIL.read_text(encoding="utf-8")).safe_substitute(valores)

    texto = "\n".join([
        f"{etiqueta.upper()} — {nombre} ({ruta_txt})",
        f"Precio por persona: {_money(h['precio'], cur)}  ({ahorro_linea})",
        f"Fechas: {_fd(h['ida'])}" + (f" → {_fd(h['vuelta'])} ({dias} días)" if ida_vuelta else ""),
        f"Vuelo: {h.get('aerolineas')} · {valores['escalas']} · {fmt_min(h['duracion_min'])} · {h.get('ruta')}",
        f"Salida: ida {h.get('ida_hora') or '—'}" + (f" · vuelta {h.get('vta_hora') or '—'}" if ida_vuelta else ""),
        (f"Tu precio objetivo: {_money(info['objetivo'], cur)}" if info["motivo"] == "objetivo"
         else f"Mínimo registrado antes de hoy: {_money(ref['precio'], cur)}"),
        *([f"Se arma con {len(tramos)} pasajes separados ({TIPOS_TXT.get(h.get('tipo'), '')}):"]
          + [f"  - {'Ida' if t['sentido'] == 'ida' else 'Vuelta'} {t['origen']}→{t['destino']} {t['fecha']} "
             f"{t['aerolineas']} {_money(t['precio'] / pax_, cur)}: {t['link']}" for t in tramos]
          + ([f"  ATENCIÓN: {h['detalle']['aviso']}"] if (h.get('detalle') or {}).get('aviso') else [])
          if tramos else [f"Reservar: {h.get('link')}"]),
        f"Reporte: {url_reporte}" if url_reporte else "",
        f"Baja: {url_baja}" if url_baja else "",
    ])
    return asunto, cuerpo, texto


# ============================================================================
# Envío
# ============================================================================
class EmailNoConfigurado(RuntimeError):
    pass


def proveedor_email() -> str | None:
    if os.environ.get("RESEND_API_KEY"):
        return "resend"
    if os.environ.get("SMTP_HOST") and os.environ.get("SMTP_USER"):
        return "smtp"
    return None


def _remitente(cfg_remitente: str = "") -> str:
    return (cfg_remitente or os.environ.get("RESEND_FROM") or os.environ.get("SMTP_FROM")
            or os.environ.get("SMTP_USER") or "Buscador de vuelos <onboarding@resend.dev>")


def _enviar_resend(para: str, asunto: str, cuerpo_html: str, texto: str, remitente: str) -> None:
    req = urllib.request.Request(
        "https://api.resend.com/emails", method="POST",
        data=json.dumps({"from": remitente, "to": [para], "subject": asunto, "html": cuerpo_html, "text": texto}).encode(),
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}", "Content-Type": "application/json",
                 "User-Agent": "buscador-vuelos/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Resend {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}") from e


def _enviar_smtp(para: str, asunto: str, cuerpo_html: str, texto: str, remitente: str) -> None:
    msg = EmailMessage()
    nombre, direccion = parseaddr(remitente)
    msg["From"] = formataddr((nombre or "Buscador de vuelos", direccion or os.environ["SMTP_USER"]))
    msg["To"] = para
    msg["Subject"] = asunto
    msg.set_content(texto)
    msg.add_alternative(cuerpo_html, subtype="html")
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587"))
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=30) as s:
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls(context=ctx)
            s.login(os.environ["SMTP_USER"], os.environ.get("SMTP_PASSWORD", ""))
            s.send_message(msg)


def enviar_email(para: str, asunto: str, cuerpo_html: str, texto: str, remitente: str = "") -> None:
    prov = proveedor_email()
    if prov is None:
        raise EmailNoConfigurado("No hay RESEND_API_KEY ni SMTP_HOST/SMTP_USER configurados")
    (_enviar_resend if prov == "resend" else _enviar_smtp)(para, asunto, cuerpo_html, texto, _remitente(remitente))


# ============================================================================
# Orquestación
# ============================================================================
def _url_publica() -> str:
    return os.environ.get("VUELOS_URL_WEB", "").rstrip("/")


def destinatarios(con: sqlite3.Connection, cfg: AppConfig, busqueda: str) -> list[dict]:
    """[{email, desde, hasta, token}] — config global + config de la búsqueda + suscriptores web (data/suscriptores.json)."""
    meta = cfg.buscar(busqueda) or {}
    vistos: dict[str, dict] = {}
    for e in cfg.general["alertas"]["emails"] + meta.get("emails", []):
        vistos.setdefault(e.lower(), {"email": e, "desde": None, "hasta": None, "token": None})
    for s in subs.de_busqueda(busqueda):
        if s.get("tipo", "mejora") != "mejora":
            continue   # los avisos de precio objetivo van por avisos_de_precio()
        vistos[s["email"].lower()] = {"email": s["email"], "desde": s["desde"], "hasta": s["hasta"], "token": s["token"]}
    return list(vistos.values())


def notificar(con: sqlite3.Connection, cfg: AppConfig, corridas: dict[str, int]) -> int:
    """corridas: {nombre_busqueda: corrida_id} de esta ejecución. Devuelve cuántos emails se enviaron."""
    alertas = cfg.general["alertas"]
    if not alertas["activas"]:
        return 0
    enviados = 0
    for nombre, corrida_id in corridas.items():
        try:
            enviados += avisos_de_precio(con, cfg, nombre, corrida_id)
        except EmailNoConfigurado as e:
            log.info(f"  ✉ No se pudo avisar: {e}")
            return enviados
        lista = destinatarios(con, cfg, nombre)
        if not lista:
            continue
        filtro = filtro_rango(cfg.buscar(nombre))
        cache: dict = {}
        for d in lista:
            clave = (d["desde"], d["hasta"])
            if clave not in cache:
                cache[clave] = detectar_mejora(con, nombre, corrida_id, d["desde"], d["hasta"],
                                               alertas["avisar_mejores_condiciones"], filtro)
            info = cache[clave]
            if not info:
                continue
            if db.ya_notificado(con, nombre, d["email"], corrida_id):
                continue
            base = _url_publica()
            url_baja = f"{base}/baja?token={d['token']}" if base and d["token"] else ""
            url_rep = cfg.general.get("url_reporte") or (base + "/" if base else "")
            asunto, cuerpo, texto = componer_email(info, cfg.buscar(nombre), url_rep, url_baja)
            try:
                enviar_email(d["email"], asunto, cuerpo, texto, alertas["remitente"])
                db.registrar_notificacion(con, nombre, d["email"], corrida_id, info["hoy"], info["motivo"], True)
                enviados += 1
                log.info(f"  ✉ Alerta enviada a {d['email']}: {asunto}")
            except EmailNoConfigurado as e:
                log.info(f"  ✉ Había una mejora para '{nombre}' pero no se pudo avisar: {e}")
                return enviados
            except Exception as e:
                db.registrar_notificacion(con, nombre, d["email"], corrida_id, info["hoy"], info["motivo"], False)
                log.info(f"  ✉ Error enviando a {d['email']}: {e}")
    return enviados


# ----------------------------------------------------------------------------
# Avisos de precio objetivo ("avisame cuando llegue a US$ X")
# ----------------------------------------------------------------------------
REPETIR = ("bajada", "diario", "una_vez")
EQ_TXT = {"ninguno": "solo mochila", "mano": "con carry on", "despachado": "con valija despachada"}


def descripcion_aviso(av: dict, desde: str | None = None, hasta: str | None = None) -> str:
    partes = []
    if desde or hasta:
        partes.append(f"saliendo {_fd(desde) if desde else '…'} → {_fd(hasta) if hasta else '…'}")
    if av.get("dias_min") or av.get("dias_max"):
        a, b = av.get("dias_min"), av.get("dias_max")
        partes.append(f"{a} días" if a and a == b else f"{a or '…'} a {b or '…'} días")
    if av.get("solo_directos"):
        partes.append("solo directos")
    if av.get("equipaje") in EQ_TXT:
        partes.append(EQ_TXT[av["equipaje"]])
    return " · ".join(partes)


def mejor_para_aviso(con: sqlite3.Connection, corrida_id: int, meta: dict | None, av: dict,
                     desde: str | None, hasta: str | None) -> dict | None:
    """La opción más barata de la corrida que cumple las condiciones del aviso (precio por persona)."""
    m = dict(meta or {})
    if av.get("equipaje") in EQUIPAJES:
        m["nivel_equipaje"] = EQUIPAJES[av["equipaje"]]
    filtro = filtro_rango(m) if meta else None
    con_duracion = bool(av.get("dias_min") or av.get("dias_max"))
    filas = []
    for f in db.filas_corrida(con, corrida_id):
        if filtro and not filtro(f):
            continue
        f = dict(f)
        f["flexible"] = f.get("flexible") or 0
        if f["flexible"] and not con_duracion:
            continue   # sin duración propia, solo cuentan los viajes de la duración de la búsqueda
        if not _en_ventana(f, desde, hasta):
            continue
        if con_duracion and f.get("vuelta"):
            dias = (dt.date.fromisoformat(f["vuelta"]) - dt.date.fromisoformat(f["ida"])).days
            if (av.get("dias_min") and dias < av["dias_min"]) or (av.get("dias_max") and dias > av["dias_max"]):
                continue
        if av.get("solo_directos") and (f.get("escalas") or 0) > 0:
            continue
        f["precio"] = f["precio"] / (f["pasajeros"] or 1)
        f["total"] = f["precio"] * (f["pasajeros"] or 1)
        filas.append(f)
    if not filas:
        return None
    agregar_horarios(con, corrida_id, filas)
    base, flex = mejores_por_combo(filas)
    todas = sorted(base + flex, key=lambda c: (c["precio"], c["escalas"], c["duracion_min"]))
    return todas[0] if todas else None


def evaluar_aviso(con: sqlite3.Connection, s: dict, mejor: dict | None, hoy: str) -> tuple[bool, float | None]:
    """(¿mandar?, precio del último aviso enviado). Reglas según aviso.repetir:
    bajada  = al llegar al precio y de nuevo cada vez que baje más que el último aviso (por defecto)
    diario  = una vez por día mientras siga en ese precio o menos
    una_vez = un solo mail y el aviso se desactiva"""
    av = s.get("aviso") or {}
    if mejor is None or mejor["precio"] > float(av.get("precio_max") or 0) + TOLERANCIA_PRECIO:
        return False, None
    previos = db.avisos_enviados(con, s["token"])
    ultimo = previos[0]["precio"] if previos else None
    rep = av.get("repetir", "bajada")
    if rep == "una_vez":
        return not previos, ultimo
    if rep == "diario":
        return not (previos and previos[0]["enviado"][:10] == hoy), ultimo
    return (not previos or mejor["precio"] < min(p["precio"] for p in previos) - TOLERANCIA_PRECIO), ultimo


def avisos_de_precio(con: sqlite3.Connection, cfg: AppConfig, nombre: str, corrida_id: int) -> int:
    meta = cfg.buscar(nombre)
    hoy = dt.date.today()
    enviados = 0
    for s in subs.de_busqueda(nombre):
        if s.get("tipo") != "precio" or not subs.vigente(s, hoy):
            continue
        av = s.get("aviso") or {}
        mejor = mejor_para_aviso(con, corrida_id, meta, av, s.get("desde"), s.get("hasta"))
        mandar, ultimo = evaluar_aviso(con, s, mejor, hoy.isoformat())
        if not mandar or not confirmado(con, corrida_id, mejor):
            continue
        ref = db.minimo_historico(con, nombre, antes_de_corrida=corrida_id, filtro=filtro_rango(meta))
        info = {"busqueda": nombre, "corrida_id": corrida_id, "motivo": "objetivo", "hoy": mejor,
                "ref": dict(ref) if ref else None, "prev_best": None, "objetivo": float(av["precio_max"]),
                "ultimo_enviado": ultimo, "vence": av.get("vence"), "repetir": av.get("repetir", "bajada"),
                "condiciones": descripcion_aviso(av, s.get("desde"), s.get("hasta")),
                "stats": {"promedio": None}, "desde": None, "hasta": None}
        base = _url_publica()
        url_baja = f"{base}/baja?token={s['token']}" if base else ""
        url_rep = cfg.general.get("url_reporte") or (base + "/" if base else "")
        asunto, cuerpo, texto = componer_email(info, meta, url_rep, url_baja)
        try:
            enviar_email(s["email"], asunto, cuerpo, texto, cfg.general["alertas"]["remitente"])
        except EmailNoConfigurado:
            raise
        except Exception as e:
            db.registrar_notificacion(con, nombre, s["email"], corrida_id, mejor, "objetivo", False, s["token"])
            log.info(f"  ✉ Error enviando aviso a {s['email']}: {e}")
            continue
        db.registrar_notificacion(con, nombre, s["email"], corrida_id, mejor, "objetivo", True, s["token"])
        enviados += 1
        log.info(f"  🎯 Aviso de precio enviado a {s['email']}: {asunto}")
        if av.get("repetir") == "una_vez":
            subs.desactivar(s["token"])
    return enviados


def email_de_prueba(con: sqlite3.Connection, cfg: AppConfig, para: str) -> str:
    """Manda la mejor opción actual de la primera búsqueda con datos, sin aplicar la regla anti-spam."""
    for nombre in db.busquedas_con_datos(con):
        corridas = db.corridas_utiles(con, nombre)
        base = _mejores_de_corrida(con, corridas[-1]["id"], None, None)
        if not base:
            continue
        ref = db.minimo_historico(con, nombre) or {"precio": base[0]["precio"] * 1.1, "ida": base[0]["ida"],
                                                   "escalas": base[0]["escalas"], "duracion_min": base[0]["duracion_min"]}
        ref = dict(ref)
        ref["precio"] = max(ref["precio"], base[0]["precio"] * 1.08)   # simula una baja para ver el diseño
        info = {"busqueda": nombre, "corrida_id": corridas[-1]["id"], "motivo": "precio", "hoy": base[0], "ref": ref,
                "prev_best": None, "stats": estadisticas([c["precio"] for c in base]), "desde": None, "hasta": None}
        asunto, cuerpo, texto = componer_email(info, cfg.buscar(nombre), cfg.general.get("url_reporte", ""))
        enviar_email(para, "[PRUEBA] " + asunto, cuerpo, texto, cfg.general["alertas"]["remitente"])
        return asunto
    raise RuntimeError("No hay datos en la base para armar un email de prueba")
