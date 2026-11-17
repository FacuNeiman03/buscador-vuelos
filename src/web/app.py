"""Interfaz web (FastAPI + Jinja2): reportes en vivo, botón de actualización y suscripción a alertas.

Local:   python -m uvicorn src.web.app:app --port 8000      (o scripts/servidor_web.bat)
Render:  ver render.yaml

Variables de entorno:
    WEB_ADMIN_TOKEN  si está definida, POST /api/actualizar exige el header X-Token con este valor
                     (recomendado en un servidor público: cada actualización hace cientos de consultas)
    VUELOS_URL_WEB   URL pública del sitio (para los links de baja de los emails)
"""
from __future__ import annotations

import datetime as dt
import hmac
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from ..core import aeropuertos
from ..core import database as db
from ..core import github_sync as gh
from ..core import config_editor as editor
from ..core import suscriptores as subs
from ..core.config import ConfigError, cargar_config
from ..core.paths import CONFIG_PATH, DATA_DIR, REPORTE_INDEX, REPORTES_DIR, SRC_DIR, migrar_legado
from ..main import configurar_log, ejecutar

log = logging.getLogger("vuelos")


class _SinSondeos(logging.Filter):
    """No llena la consola con los GET /api/estado que la página hace cada 5 s mientras busca."""
    def filter(self, record: logging.LogRecord) -> bool:
        return "/api/estado" not in record.getMessage()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    configurar_log()
    logging.getLogger("uvicorn.access").addFilter(_SinSondeos())
    migrar_legado()
    if modo_nube():
        # El disco de Render vuelve al último deploy al despertar: traigo lo último del repo
        for ruta in (CONFIG_PATH, subs.ruta()):
            if gh.bajar_archivo(ruta):
                log.info(f"Actualizado desde GitHub: {ruta.name}")
        if not sincronizar(forzar=True):
            _regenerar()
        log.info("Modo nube: las búsquedas corren en GitHub Actions")
    if not REPORTE_INDEX.exists():
        try:
            ejecutar(solo_reporte=True, enviar_alertas=False)
        except Exception as e:
            log.info(f"No se pudo generar el reporte inicial: {e}")
    yield


app = FastAPI(title="Buscador de vuelos", docs_url=None, redoc_url=None, lifespan=lifespan)
templates = Jinja2Templates(directory=str(SRC_DIR / "web" / "templates"))
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
SIN_CACHE = {"Cache-Control": "no-store"}


# ----------------------------------------------------------------------------
# Estado del trabajo en segundo plano
# ----------------------------------------------------------------------------
class Trabajo:
    def __init__(self):
        self.lock = threading.Lock()
        self.corriendo = False
        self.progreso = ""
        self.inicio: str | None = None
        self.error: str | None = None
        self.ultimo: dict | None = None
        self.busqueda: str | None = None

    def snapshot(self) -> dict:
        s = {"corriendo": self.corriendo, "progreso": self.progreso, "inicio": self.inicio,
             "error": self.error, "ultimo": self.ultimo}
        if modo_nube():
            s.update(nube=True, url=nube.url, en_curso=nube.en_curso)
        return s


trabajo = Trabajo()


# ----------------------------------------------------------------------------
# Modo nube (Render): las búsquedas corren en GitHub Actions; acá solo se disparan y se bajan resultados
# ----------------------------------------------------------------------------
def modo_nube() -> bool:
    return gh.habilitado() and os.environ.get("VUELOS_EJECUCION", "").lower() == "github"


class EstadoNube:
    def __init__(self):
        self.sha: str | None = None        # commit de la rama "datos" que tengo bajado
        self.revisado = 0.0                # última vez que pregunté si cambió
        self.consultado = 0.0              # última vez que miré las ejecuciones del workflow
        self.disparo: float | None = None  # cuándo pedí la búsqueda en curso
        self.url: str | None = None        # link a la ejecución en GitHub
        self.en_curso: dict | None = None  # ejecución programada en curso (no lanzada desde la web)


nube = EstadoNube()
_sync_lock = threading.Lock()


def _ts(iso: str) -> float:
    return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def sincronizar(forzar: bool = False) -> bool:
    """Si la rama "datos" cambió (terminó una búsqueda en la nube), baja la base y regenera los reportes."""
    if not _sync_lock.acquire(blocking=forzar):
        return False
    try:
        if not forzar and time.time() - nube.revisado < 60:
            return False
        nube.revisado = time.time()
        sha = gh.sha_datos()
        if not sha or sha == nube.sha:
            return False
        if not gh.bajar_datos(DATA_DIR):
            return False
        nube.sha = sha
        log.info(f"Datos nuevos bajados de GitHub ({sha[:7]})")
        _regenerar()
        return True
    finally:
        _sync_lock.release()


def _actualizar_estado_nube() -> None:
    if time.time() - nube.consultado < 10:
        return
    nube.consultado = time.time()
    runs = gh.ejecuciones(10)
    # Para el indicador de la página: la búsqueda en curso más vieja (manual o programada). Las programadas
    # sin nada para buscar duran segundos: se ignoran las de menos de un minuto.
    activas = [r for r in runs if r["estado"] != "completed"
               and (r["evento"] == "workflow_dispatch" or time.time() - _ts(r["iniciado"]) > 60)]
    nube.en_curso = activas[-1] if activas else None
    if not trabajo.corriendo:
        return
    if nube.disparo is None:
        return
    mios = [r for r in runs if r["evento"] == "workflow_dispatch" and _ts(r["creado"]) >= nube.disparo - 90]
    if not mios:
        if time.time() - nube.disparo > 300:
            trabajo.corriendo, trabajo.progreso = False, ""
            trabajo.error = "GitHub no arrancó la búsqueda en 5 minutos. Revisá la pestaña Actions del repo."
        return
    r = mios[-1]   # vienen de la más nueva a la más vieja: la primera después de mi pedido
    nube.url = r["url"]
    if r["estado"] != "completed":
        if r["estado"] in ("queued", "waiting", "pending", "requested"):
            trabajo.progreso = "En cola en GitHub (si hay otra búsqueda corriendo, espera a que termine)…"
        else:
            mins = max(1, round((time.time() - _ts(r["iniciado"])) / 60))
            trabajo.progreso = (f"Buscando en la nube · {mins} min. Puede tardar bastante; podés cerrar la página, "
                                "los resultados quedan guardados y las alertas llegan por mail.")
        return
    trabajo.progreso = "Bajando resultados…"
    sincronizar(forzar=True)
    if r["conclusion"] == "success":
        trabajo.error = None
        trabajo.ultimo = {"fin": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "busqueda": trabajo.busqueda,
                          "url": r["url"]}
    elif r["conclusion"] == "cancelled":
        trabajo.error = f"La búsqueda en GitHub se canceló. Detalle: {r['url']}"
    else:
        trabajo.error = f"La búsqueda en GitHub terminó con error ({r['conclusion']}). Detalle: {r['url']}"
    trabajo.corriendo, trabajo.progreso, nube.disparo = False, "", None


def _correr(busqueda: str | None) -> None:
    try:
        res = ejecutar(busqueda=busqueda, forzar=True, progreso=lambda m: setattr(trabajo, "progreso", m))
        trabajo.ultimo = {"fin": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "busqueda": busqueda,
                          "emails": res.get("emails", 0)}
    except Exception as e:   # se informa a la UI
        log.exception("Error en actualización web")
        trabajo.error = str(e)
    finally:
        trabajo.corriendo = False
        trabajo.progreso = ""


# ----------------------------------------------------------------------------
# Reportes
# ----------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def indice():
    if modo_nube():
        sincronizar()
    if not REPORTE_INDEX.exists():
        ejecutar(solo_reporte=True, enviar_alertas=False)
    return FileResponse(REPORTE_INDEX, media_type="text/html", headers=SIN_CACHE)


@app.get("/{nombre}.html", include_in_schema=False)
def reporte(nombre: str):
    if not re.fullmatch(r"[a-z0-9-]+", nombre):
        raise HTTPException(404)
    if modo_nube():
        sincronizar()
    ruta = REPORTES_DIR / f"{nombre}.html"
    if not ruta.is_file():
        raise HTTPException(404, "Ese reporte no existe")
    return FileResponse(ruta, media_type="text/html", headers=SIN_CACHE)


# ----------------------------------------------------------------------------
# API
# ----------------------------------------------------------------------------
class PedidoActualizar(BaseModel):
    busqueda: str | None = None


@app.get("/api/salud")
def salud():
    return {"ok": True, "hora": dt.datetime.now().isoformat(timespec="seconds")}


@app.get("/api/estado")
def estado():
    if modo_nube():
        _actualizar_estado_nube()
    return JSONResponse(trabajo.snapshot(), headers=SIN_CACHE)


def _autorizar(x_token: str | None) -> None:
    esperado = os.environ.get("WEB_ADMIN_TOKEN", "")
    if esperado and not hmac.compare_digest(x_token or "", esperado):
        raise HTTPException(401, "Token inválido")


def _lanzar(busqueda: str | None) -> JSONResponse | None:
    """Arranca una búsqueda en segundo plano. Devuelve una respuesta 409 si ya hay una corriendo."""
    with trabajo.lock:
        if trabajo.corriendo:
            return JSONResponse({"detail": "Ya hay una actualización en curso", **trabajo.snapshot()}, status_code=409)
        if modo_nube():
            ok, msg = gh.disparar_busqueda(busqueda)
            if not ok:
                return JSONResponse({"detail": f"No se pudo lanzar la búsqueda en la nube: {msg}"}, status_code=502)
            trabajo.corriendo, trabajo.error, trabajo.busqueda = True, None, busqueda
            trabajo.inicio = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
            trabajo.progreso = "Enviado a la nube: esperando que GitHub lo arranque…"
            nube.disparo, nube.url, nube.consultado = time.time(), None, 0.0
            return None
        trabajo.corriendo, trabajo.error = True, None
        trabajo.inicio = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        trabajo.progreso = "Iniciando…"
    threading.Thread(target=_correr, args=(busqueda,), daemon=True).start()
    return None


@app.post("/api/actualizar", status_code=202)
def actualizar(pedido: PedidoActualizar, x_token: str | None = Header(default=None)):
    _autorizar(x_token)
    if pedido.busqueda:
        try:
            if not cargar_config().buscar(pedido.busqueda):
                raise HTTPException(404, f"No existe la búsqueda '{pedido.busqueda}'")
        except ConfigError as e:
            raise HTTPException(500, str(e))
    return _lanzar(pedido.busqueda) or {"ok": True, "busqueda": pedido.busqueda}


# ----------------------------------------------------------------------------
# Gestión de búsquedas (crear / editar / pausar / borrar)
# ----------------------------------------------------------------------------
class PedidoBusqueda(BaseModel):
    nombre: str = ""
    origenes: list[str] | str = []
    destinos: list[str] | str = []
    destino_pais: str | None = None
    explorar_top: int | None = None
    tipo: str = "ida_vuelta"
    viajar_desde: str = ""
    viajar_hasta: str = ""
    dias_min: int | None = None
    dias_max: int | None = None
    pasajeros: int = 1
    clase: str = "economy"
    max_escalas: int | None = None
    alerta_precio_persona: float | None = None
    estrategia: str = "ida_vuelta"
    equipaje: str = "ninguno"
    equipajes: list[str] | None = None
    escala_separada: bool = False
    hubs: list[str] | str = []
    conexion_min_horas: int | None = None
    activa: bool = True
    buscar_desde: str = ""
    buscar_hasta: str = ""
    hora: int | None = None
    buscar_ahora: bool = False
    original: str | None = None      # solo para /estimar: nombre de la búsqueda que se está editando


class PedidoEstado(BaseModel):
    activa: bool


def _error_form(e: Exception) -> HTTPException:
    errores = getattr(e, "errores", None) or [str(e)]
    return HTTPException(422, {"errores": errores})


def _regenerar() -> None:
    try:
        ejecutar(solo_reporte=True, enviar_alertas=False)
    except Exception as e:   # el cambio ya quedó guardado; el reporte se regenera en la próxima corrida
        log.info(f"No se pudo regenerar el reporte: {e}")


@app.get("/api/aeropuertos", include_in_schema=False)
def lista_aeropuertos():
    """Base de aeropuertos (OurAirports) para el buscador por país / ciudad del formulario."""
    return FileResponse(aeropuertos.RUTA, media_type="application/json",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/api/busquedas")
def listar_busquedas():
    try:
        cfg = cargar_config()
    except ConfigError as e:
        raise HTTPException(500, str(e))
    return [{**editor.form_desde_meta(b)} for b in cfg.busquedas]


@app.post("/api/busquedas/estimar")
def estimar_busqueda(p: PedidoBusqueda):
    try:
        existente = editor.item_crudo(p.original) if p.original else None
        return editor.estimar(p.model_dump(), existente)
    except (editor.FormError, ValueError) as e:
        raise _error_form(e)


@app.post("/api/busquedas", status_code=201)
def crear_busqueda(p: PedidoBusqueda, x_token: str | None = Header(default=None)):
    _autorizar(x_token)
    try:
        f = editor.crear(p.model_dump())
    except editor.FormError as e:
        raise _error_form(e)
    _regenerar()
    en_curso = _lanzar(f["nombre"]) if p.buscar_ahora and f["activa"] else None
    return {"ok": True, "nombre": f["nombre"], "buscando": bool(p.buscar_ahora and f["activa"] and not en_curso)}


@app.put("/api/busquedas/{nombre}")
def editar_busqueda(nombre: str, p: PedidoBusqueda, x_token: str | None = Header(default=None)):
    _autorizar(x_token)
    try:
        f = editor.actualizar(nombre, p.model_dump())
    except KeyError:
        raise HTTPException(404, f"No existe la búsqueda '{nombre}'")
    except editor.FormError as e:
        raise _error_form(e)
    _regenerar()
    en_curso = _lanzar(f["nombre"]) if p.buscar_ahora and f["activa"] else None
    return {"ok": True, "nombre": f["nombre"], "buscando": bool(p.buscar_ahora and f["activa"] and not en_curso)}


@app.post("/api/busquedas/{nombre}/estado")
def estado_busqueda(nombre: str, p: PedidoEstado, x_token: str | None = Header(default=None)):
    _autorizar(x_token)
    try:
        editor.cambiar_estado(nombre, p.activa)
    except KeyError:
        raise HTTPException(404, f"No existe la búsqueda '{nombre}'")
    except editor.FormError as e:
        raise _error_form(e)
    _regenerar()
    return {"ok": True}


@app.delete("/api/busquedas/{nombre}")
def borrar_busqueda(nombre: str, x_token: str | None = Header(default=None)):
    _autorizar(x_token)
    try:
        editor.borrar(nombre)
    except KeyError:
        raise HTTPException(404, f"No existe la búsqueda '{nombre}'")
    except editor.FormError as e:
        raise _error_form(e)
    _regenerar()
    return {"ok": True}


# ----------------------------------------------------------------------------
# Suscripciones
# ----------------------------------------------------------------------------
_ultimos_envios: dict[str, float] = {}


def _limite(ip: str, cada_seg: int = 10) -> bool:
    ahora = time.time()
    if ahora - _ultimos_envios.get(ip, 0) < cada_seg:
        return False
    _ultimos_envios[ip] = ahora
    return True


DURACIONES_AVISO = [7, 15, 30, 60, 90, 180, 365]


def _info_busquedas(cfg) -> list[dict]:
    """Para el formulario: moneda, equipajes y el precio más barato de hoy de cada búsqueda activa."""
    from ..notifications.mailer import _mejores_de_corrida
    from ..reporting.metrics import filtro_rango
    out = []
    with db.conexion() as con:
        for b in cfg.activas:
            hoy = None
            corridas = db.corridas_utiles(con, b["nombre"])
            if corridas:
                base = _mejores_de_corrida(con, corridas[-1]["id"], None, None, filtro_rango(b))
                hoy = round(base[0]["precio"]) if base else None
            out.append({"nombre": b["nombre"], "moneda": cfg.general.get("moneda", "USD"),
                        "equipajes": b.get("equipajes") or [b.get("equipaje", "ninguno")], "hoy": hoy})
    return out


def _render_sus(request: Request, cfg, sel, datos, error=None, ok=None, status=200):
    info = _info_busquedas(cfg)
    return templates.TemplateResponse(request, "suscribirse.html", {
        "busquedas": [x["nombre"] for x in info], "info": info, "sel": sel, "error": error, "ok": ok,
        "datos": datos, "duraciones": DURACIONES_AVISO}, status_code=status)


@app.get("/suscribirse", response_class=HTMLResponse)
def form_suscribirse(request: Request, busqueda: str | None = None, tipo: str = "mejora"):
    return _render_sus(request, cargar_config(), busqueda, {"tipo": tipo if tipo in ("mejora", "precio") else "mejora"})


@app.post("/suscribirse", response_class=HTMLResponse)
def suscribirse(request: Request, email: str = Form(...), busqueda: str = Form(...),
                desde: str = Form(""), hasta: str = Form(""), web: str = Form(""),
                tipo: str = Form("mejora"), precio_max: str = Form(""), duracion: str = Form("30"),
                dias_min: str = Form(""), dias_max: str = Form(""), solo_directos: str = Form(""),
                equipaje: str = Form(""), repetir: str = Form("bajada")):
    from ..core.config import EQUIPAJES
    from ..notifications.mailer import REPETIR
    cfg = cargar_config()
    nombres = [b["nombre"] for b in cfg.activas]
    datos = {"email": email, "desde": desde, "hasta": hasta, "tipo": tipo, "precio_max": precio_max,
             "duracion": duracion, "dias_min": dias_min, "dias_max": dias_max, "solo_directos": solo_directos,
             "equipaje": equipaje, "repetir": repetir}
    email = email.strip().lower()
    error, aviso = None, None
    if web:   # honeypot anti-bots
        error = "No se pudo procesar el formulario."
    elif not _limite(request.client.host if request.client else "?"):
        error = "Esperá unos segundos antes de volver a enviar."
    elif not EMAIL_RE.match(email) or len(email) > 254:
        error = "Ese email no parece válido."
    elif busqueda not in nombres:
        error = "Elegí una búsqueda de la lista."
    elif tipo not in ("mejora", "precio"):
        error = "Elegí cuándo querés que te avisemos."
    else:
        try:
            d = dt.date.fromisoformat(desde) if desde else None
            h = dt.date.fromisoformat(hasta) if hasta else None
            if d and h and h < d:
                error = "La fecha 'hasta' no puede ser anterior a 'desde'."
        except ValueError:
            error = "Fechas inválidas."
    if not error and tipo == "precio":
        try:
            pm = float(precio_max.replace(",", "."))
            if pm <= 0:
                raise ValueError
            dmin = int(dias_min) if dias_min.strip() else None
            dmax = int(dias_max) if dias_max.strip() else None
            if (dmin is not None and dmin < 1) or (dmax is not None and dmax < 1):
                raise ValueError
            if dmin and dmax and dmax < dmin:
                error = "Los días mínimos no pueden ser más que los máximos."
            if repetir not in REPETIR:
                error = "Elegí cada cuánto avisarte."
            if equipaje and equipaje not in EQUIPAJES:
                error = "Equipaje inválido."
            vence = None
            if duracion and duracion != "0":
                n = int(duracion)
                if n not in DURACIONES_AVISO:
                    raise ValueError
                vence = (dt.date.today() + dt.timedelta(days=n)).isoformat()
            aviso = {"precio_max": round(pm, 2), "vence": vence, "dias_min": dmin, "dias_max": dmax,
                     "solo_directos": bool(solo_directos), "equipaje": equipaje or None, "repetir": repetir}
        except ValueError:
            error = error or "Revisá el precio y los días: tienen que ser números positivos."
    if error:
        return _render_sus(request, cfg, busqueda, datos, error=error, status=400)

    token, confirmado = subs.agregar(email, busqueda, desde or None, hasta or None, confirmado=False,
                                     tipo=tipo, aviso=aviso)
    rango = f" para salidas entre {desde or '…'} y {hasta or '…'}" if desde or hasta else ""
    if tipo == "precio":
        que = (f"cuando '{busqueda}' llegue a {cfg.general.get('moneda', 'USD')} {aviso['precio_max']:g} por persona o menos"
               + (f" (hasta el {dt.date.fromisoformat(aviso['vence']):%d/%m/%Y})" if aviso["vence"] else ""))
    else:
        que = f"cuando baje el precio de '{busqueda}'"
    if confirmado:
        ok = f"Listo: actualizamos tu aviso. Te vamos a escribir a {email} {que}{rango}."
    else:
        try:
            _mail_confirmacion(request, email, busqueda, token)
        except Exception as e:
            log.info(f"No se pudo mandar el mail de confirmación a {email}: {e}")
            return _render_sus(request, cfg, busqueda, datos, status=502,
                               error="No pudimos mandar el mail de confirmación. Probá de nuevo en unos minutos.")
        ok = (f"Casi listo: en 1 o 2 minutos te llega un mail a {email} (mirá también en spam). Abrí el link para "
              f"confirmar: después te avisamos {que}{rango}.")
    return _render_sus(request, cfg, busqueda, {"tipo": tipo}, ok=ok)


def _mail_confirmacion(request: Request, email: str, busqueda: str, token: str) -> None:
    from ..notifications import confirmacion
    if modo_nube():
        # Render gratis bloquea SMTP: el mail lo manda GitHub Actions (workflow confirmacion.yml)
        ok, msg = gh.disparar_workflow("confirmacion.yml", {"ref": confirmacion.referencia(token)})
        if not ok:
            raise RuntimeError(msg)
        return
    base = os.environ.get("VUELOS_URL_WEB") or str(request.base_url)
    confirmacion.enviar(email, busqueda, token, base)


@app.get("/confirmar", response_class=HTMLResponse)
def confirmar(request: Request, token: str = ""):
    s = subs.confirmar(token) if token else None
    msg = (f"Listo, confirmado: te vamos a avisar cuando baje el precio de '{s['busqueda']}'." if s
           else "El link de confirmación no es válido.")
    return templates.TemplateResponse(request, "mensaje.html", {"titulo": "Alertas", "mensaje": msg})


@app.get("/baja", response_class=HTMLResponse)
def baja(request: Request, token: str = ""):
    n = subs.dar_de_baja(token) if token else False
    msg = "Listo, no vas a recibir más alertas de esa búsqueda." if n else "El link de baja no es válido o ya lo usaste."
    return templates.TemplateResponse(request, "mensaje.html", {"titulo": "Alertas", "mensaje": msg})
