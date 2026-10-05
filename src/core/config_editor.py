"""Alta, edición, pausa y baja de búsquedas desde la web, escribiendo en config/config.yaml.

Usa ruamel.yaml en modo "round-trip": se conservan los comentarios, el orden y el formato del
archivo, así el config sigue siendo cómodo de editar a mano.

Modelo del formulario (lo que ve el usuario):
    nombre, origenes, destinos, tipo (ida_vuelta | solo_ida),
    destino_pais, explorar_top  -> "explorar un país": pasada rápida por todos sus aeropuertos
                                   y búsqueda completa solo en los N más baratos
    viajar_desde, viajar_hasta  -> TODO el viaje (ida y vuelta) cae dentro de este rango
    dias_min, dias_max          -> duración del viaje
    pasajeros, clase, max_escalas, alerta_precio_persona, activa
    estrategia (mixta | solo_ida | ida_vuelta), equipaje (ninguno | mano | despachado)
    escala_separada, hubs, conexion_min_horas
"""
from __future__ import annotations

import datetime as dt
import io
import os
import re
import threading
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from . import github_sync
from .config import CLASES, EQUIPAJES, ESTRATEGIAS, ConfigError, _validar_busqueda, cargar_config, lista_duraciones
from .flight_searcher import estimar as estimar_busqueda
from .paths import CONFIG_PATH

ORIGENES_DEFAULT = ["AEP", "EZE"]          # Buenos Aires
MAX_DIAS_RANGO = 366
_IATA = re.compile(r"^[A-Z]{3}$")
_lock = threading.Lock()
# claves de fechas que se reemplazan por viajar_desde/viajar_hasta al guardar desde la web
_CLAVES_FECHAS_VIEJAS = ("salida_desde", "salida_hasta", "meses_permitidos", "vuelta_hasta", "fechas")


class FormError(ValueError):
    def __init__(self, errores: list[str]):
        super().__init__("; ".join(errores))
        self.errores = errores


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 120
    return y


def _ruta() -> Path:
    return Path(CONFIG_PATH)


def _leer() -> CommentedMap:
    return _yaml().load(_ruta().read_text(encoding="utf-8"))


def _volcar(doc: CommentedMap) -> str:
    buf = io.StringIO()
    _yaml().dump(doc, buf)
    return buf.getvalue()


def _guardar(doc: CommentedMap, mensaje: str) -> None:
    """Valida el YAML resultante con el mismo cargador del programa y recién ahí lo escribe."""
    texto = _volcar(doc)
    ruta = _ruta()
    tmp = ruta.with_suffix(".yaml.tmp")
    tmp.write_text(texto, encoding="utf-8")
    try:
        cargar_config(tmp)
    except ConfigError as e:
        tmp.unlink(missing_ok=True)
        raise FormError([str(e)]) from e
    os.replace(tmp, ruta)
    subido = github_sync.subir(ruta, texto.encode("utf-8"), mensaje)
    if not subido and os.environ.get("VUELOS_EJECUCION", "").lower() == "github" and github_sync.habilitado():
        # En la nube el disco local se pierde: si no quedó en GitHub, el cambio no sirve (ni lo ve la búsqueda)
        raise FormError(["Se guardó acá pero no se pudo guardar en GitHub (revisá GITHUB_TOKEN: permiso "
                         "Contents: Read and write). Probá de nuevo."])


def _items(doc: CommentedMap) -> CommentedSeq:
    if doc.get("busquedas") is None:
        doc["busquedas"] = CommentedSeq()
    return doc["busquedas"]


def _indice(doc: CommentedMap, nombre: str) -> int:
    n = nombre.strip().lower()
    for i, b in enumerate(_items(doc)):
        if str(b.get("nombre", "")).strip().lower() == n:
            return i
    raise KeyError(nombre)


# ----------------------------------------------------------------------------
# Formulario <-> config
# ----------------------------------------------------------------------------
def _lista_iata(v: Any) -> list[str]:
    if isinstance(v, str):
        v = re.split(r"[\s,;/]+", v)
    return [str(x).strip().upper() for x in (v or []) if str(x).strip()]


def normalizar_form(data: dict, hoy: dt.date | None = None) -> dict:
    """Valida lo que llega del formulario. Lanza FormError con todos los problemas juntos."""
    hoy = hoy or dt.date.today()
    err: list[str] = []
    f: dict[str, Any] = {}

    f["nombre"] = str(data.get("nombre") or "").strip()
    if not f["nombre"]:
        err.append("Poné un nombre para la búsqueda (ej: Cipolletti verano).")
    elif len(f["nombre"]) > 60:
        err.append("El nombre puede tener hasta 60 caracteres.")

    f["origenes"] = _lista_iata(data.get("origenes")) or list(ORIGENES_DEFAULT)
    f["destinos"] = _lista_iata(data.get("destinos"))
    if not f["destinos"]:
        err.append("Falta el destino (código de aeropuerto, ej: NQN).")
    for c in f["origenes"] + f["destinos"]:
        if not _IATA.match(c):
            err.append(f"'{c}' no es un código de aeropuerto válido (son 3 letras, ej: EZE).")
    if set(f["origenes"]) & set(f["destinos"]):
        err.append("El origen y el destino no pueden ser el mismo aeropuerto.")

    if len(f["destinos"]) > 60:
        err.append("Máximo 60 aeropuertos de destino.")
    pais = str(data.get("destino_pais") or "").strip().upper()
    f["destino_pais"] = pais if re.fullmatch(r"[A-Z]{2}", pais) else None
    try:
        top = data.get("explorar_top")
        f["explorar_top"] = int(top) if top not in (None, "") else (3 if f["destino_pais"] else 0)
        if not 0 <= f["explorar_top"] <= 10:
            raise ValueError
    except (TypeError, ValueError):
        err.append("Destinos a buscar en detalle: de 1 a 10.")
        f["explorar_top"] = 3

    f["tipo"] = data.get("tipo") or "ida_vuelta"
    if f["tipo"] not in ("ida_vuelta", "solo_ida"):
        err.append("Tipo de viaje inválido.")

    try:
        f["viajar_desde"] = dt.date.fromisoformat(str(data.get("viajar_desde")))
        f["viajar_hasta"] = dt.date.fromisoformat(str(data.get("viajar_hasta")))
    except ValueError:
        err.append("Completá las fechas 'desde' y 'hasta'.")
        f["viajar_desde"] = f["viajar_hasta"] = None
    d, h = f["viajar_desde"], f["viajar_hasta"]

    try:
        f["dias_min"] = int(data.get("dias_min") or 0)
        f["dias_max"] = int(data.get("dias_max") or f["dias_min"])
    except (TypeError, ValueError):
        err.append("Los días de viaje tienen que ser números.")
        f["dias_min"] = f["dias_max"] = 0

    if d and h:
        if h < d:
            err.append("La fecha 'hasta' es anterior a 'desde'.")
        elif h <= hoy:
            err.append("El rango ya pasó: elegí fechas futuras.")
        elif (h - d).days > MAX_DIAS_RANGO:
            err.append(f"El rango puede ser de hasta {MAX_DIAS_RANGO} días.")
        if f["tipo"] == "ida_vuelta" and h >= d:
            if f["dias_min"] < 1 or f["dias_max"] < f["dias_min"]:
                err.append("Días de viaje: el mínimo tiene que ser al menos 1 y no mayor al máximo.")
            elif f["dias_min"] > (h - max(d, hoy + dt.timedelta(days=1))).days:
                err.append(f"Entre {max(d, hoy + dt.timedelta(days=1)):%d/%m/%Y} y {h:%d/%m/%Y} no entra un viaje de "
                           f"{f['dias_min']} días (ida y vuelta tienen que quedar dentro del rango).")
            elif f["dias_max"] > 60:
                err.append("Días de viaje: máximo 60.")

    try:
        f["pasajeros"] = int(data.get("pasajeros") or 1)
        if not 1 <= f["pasajeros"] <= 9:
            raise ValueError
    except (TypeError, ValueError):
        err.append("Pasajeros: de 1 a 9.")
        f["pasajeros"] = 1

    f["clase"] = data.get("clase") or "economy"
    if f["clase"] not in CLASES:
        err.append("Clase inválida.")

    me = data.get("max_escalas")
    f["max_escalas"] = None if me in (None, "", "null", "cualquiera") else int(me)
    if f["max_escalas"] is not None and not 0 <= f["max_escalas"] <= 3:
        err.append("Escalas máximas: 0, 1, 2 o cualquiera.")

    al = data.get("alerta_precio_persona")
    try:
        f["alerta_precio_persona"] = float(al) if al not in (None, "") else None
        if f["alerta_precio_persona"] is not None and f["alerta_precio_persona"] <= 0:
            raise ValueError
    except (TypeError, ValueError):
        err.append("El precio de alerta tiene que ser un número mayor a 0.")
        f["alerta_precio_persona"] = None

    f["estrategia"] = data.get("estrategia") or "ida_vuelta"
    if f["estrategia"] not in ESTRATEGIAS:
        err.append("Estrategia inválida.")
    eqs = data.get("equipajes") or data.get("equipaje") or ["ninguno"]
    eqs = [eqs] if isinstance(eqs, str) else list(eqs)
    eqs = sorted(dict.fromkeys(eqs), key=lambda x: EQUIPAJES.get(x, 9))   # mochila, carry on, despachada
    if not eqs or any(x not in EQUIPAJES for x in eqs):
        err.append("Equipaje inválido: elegí al menos uno.")
        eqs = ["ninguno"]
    f["equipajes"] = eqs
    f["equipaje"] = eqs[0]
    f["escala_separada"] = bool(data.get("escala_separada", False))
    f["hubs"] = _lista_iata(data.get("hubs"))
    for c in f["hubs"]:
        if not _IATA.match(c):
            err.append(f"'{c}' no es un código de aeropuerto válido para la escala.")
    try:
        f["conexion_min_horas"] = int(data.get("conexion_min_horas") or 4)
        if not 1 <= f["conexion_min_horas"] <= 24:
            raise ValueError
    except (TypeError, ValueError):
        err.append("Conexión mínima: entre 1 y 24 horas.")
        f["conexion_min_horas"] = 4

    f["activa"] = bool(data.get("activa", True))
    for k in ("buscar_desde", "buscar_hasta"):
        v = str(data.get(k) or "").strip()
        f[k] = None
        if v:
            try:
                f[k] = dt.date.fromisoformat(v).isoformat()
            except ValueError:
                err.append("Las fechas de ejecución tienen que ser válidas (AAAA-MM-DD).")
    if f["buscar_desde"] and f["buscar_hasta"] and f["buscar_hasta"] < f["buscar_desde"]:
        err.append("'Buscar hasta' no puede ser anterior a 'Buscar desde'.")
    h = data.get("hora")
    f["hora"] = None
    if h not in (None, ""):
        try:
            f["hora"] = int(h)
            if not 0 <= f["hora"] <= 23:
                raise ValueError
        except (TypeError, ValueError):
            err.append("La hora de búsqueda tiene que estar entre 0 y 23.")
            f["hora"] = None
    if err:
        raise FormError(err)
    return f


def _aplicar(item: CommentedMap, f: dict, nueva: bool) -> CommentedMap:
    """Escribe los campos del formulario sobre el bloque YAML, conservando el resto de sus claves."""
    def poner(clave, valor, despues_de=None):
        if clave in item:
            item[clave] = valor
        else:
            pos = list(item.keys()).index(despues_de) + 1 if despues_de in item else len(item)
            item.insert(pos, clave, valor)

    def flujo(lista):
        s = CommentedSeq(lista)
        s.fa.set_flow_style()
        return s

    poner("nombre", f["nombre"])
    poner("activa", f["activa"], "nombre")
    for k, pos in (("buscar_desde", "activa"), ("buscar_hasta", "buscar_desde" if f.get("buscar_desde") else "activa")):
        if f.get(k):
            poner(k, f[k], pos)
        else:
            item.pop(k, None)
    previa = "buscar_hasta" if f.get("buscar_hasta") else ("buscar_desde" if f.get("buscar_desde") else "activa")
    if f.get("hora") is not None:
        poner("hora", f["hora"], previa)
        previa = "hora"
    else:
        item.pop("hora", None)
    poner("origenes", flujo(f["origenes"]), previa)
    poner("destinos", flujo(f["destinos"]), "origenes")
    if f["destino_pais"] or f["explorar_top"]:
        if f["destino_pais"]:
            poner("destino_pais", f["destino_pais"], "destinos")
        else:
            item.pop("destino_pais", None)
        poner("explorar_top", f["explorar_top"], "destino_pais" if f["destino_pais"] else "destinos")
    else:
        item.pop("destino_pais", None)
        item.pop("explorar_top", None)
    poner("tipo", f["tipo"], "explorar_top" if "explorar_top" in item else "destinos")
    poner("pasajeros", f["pasajeros"], "tipo")
    poner("clase", f["clase"], "pasajeros")
    poner("max_escalas", f["max_escalas"], "clase")
    for k in _CLAVES_FECHAS_VIEJAS:
        item.pop(k, None)
    poner("viajar_desde", f["viajar_desde"].isoformat(), "max_escalas")
    poner("viajar_hasta", f["viajar_hasta"].isoformat(), "viajar_desde")
    if f["tipo"] == "ida_vuelta":
        if f["dias_min"] == f["dias_max"]:
            dur: Any = flujo([f["dias_min"]])
        else:
            dur = CommentedMap([("min", f["dias_min"]), ("max", f["dias_max"])])
            dur.fa.set_flow_style()
        poner("duracion_dias", dur, "viajar_hasta")
    if f["tipo"] == "ida_vuelta":
        poner("estrategia", f["estrategia"])
    else:
        item.pop("estrategia", None)
    item.pop("equipaje_despachado", None)
    poner("equipaje", flujo(f["equipajes"]) if len(f["equipajes"]) > 1 else f["equipajes"][0])
    if f["escala_separada"] and f["tipo"] == "ida_vuelta":
        poner("escala_separada", True)
        if f["hubs"]:
            poner("hubs", flujo(f["hubs"]))
        else:
            item.pop("hubs", None)
        poner("conexion_min_horas", f["conexion_min_horas"])
    else:
        for k in ("escala_separada", "hubs", "conexion_min_horas"):
            item.pop(k, None)
    if nueva:
        # rangos acotados: se prueba cada día (no hace falta refinar) y la duración ya la da min-max
        poner("paso_dias", 1)
        poner("refinar_top", 6)
        poner("tolerancia_dias", 0)
        poner("ahorro_minimo_pct", 5)
    if f["alerta_precio_persona"] is not None:
        v = f["alerta_precio_persona"]
        poner("alerta_precio_persona", int(v) if v == int(v) else v)
    else:
        item.pop("alerta_precio_persona", None)
        item.pop("alerta_precio_total", None)
    return item


def form_desde_meta(meta: dict) -> dict:
    """Valores para precargar el formulario a partir de una búsqueda ya normalizada por config.py."""
    tipo = meta.get("tipo", "ida_vuelta")
    try:
        durs = lista_duraciones(meta) if tipo == "ida_vuelta" else [0]
    except (KeyError, TypeError, ValueError):
        durs = [7]
    if meta.get("fechas"):
        idas = [x["ida"] for x in meta["fechas"]]
        vtas = [x["vuelta"] or x["ida"] for x in meta["fechas"]]
        desde, hasta = min(idas), max(vtas)
    else:
        desde = meta.get("_desde") or dt.date.today() + dt.timedelta(days=1)
        hasta = meta.get("_vuelta_hasta") or ((meta.get("_hasta") or desde) + dt.timedelta(days=max(durs)))
    return {
        "nombre": meta["nombre"], "activa": meta.get("activa", True),
        "buscar_desde": meta["buscar_desde"].isoformat() if meta.get("buscar_desde") else "",
        "buscar_hasta": meta["buscar_hasta"].isoformat() if meta.get("buscar_hasta") else "",
        "hora": meta.get("hora") if meta.get("_hora_propia") else None,
        "origenes": meta.get("origenes", ORIGENES_DEFAULT), "destinos": meta.get("destinos", []),
        "destino_pais": meta.get("destino_pais"), "explorar_top": meta.get("explorar_top", 0),
        "tipo": tipo, "viajar_desde": desde.isoformat(), "viajar_hasta": hasta.isoformat(),
        "dias_min": min(durs), "dias_max": max(durs), "pasajeros": meta.get("pasajeros", 1),
        "clase": meta.get("clase", "economy"), "max_escalas": meta.get("max_escalas"),
        "alerta_precio_persona": meta.get("alerta_precio_persona"),
        "estrategia": meta.get("estrategia", "ida_vuelta"), "equipaje": meta.get("equipaje", "ninguno"),
        "equipajes": meta.get("equipajes") or [meta.get("equipaje", "ninguno")],
        "escala_separada": bool(meta.get("escala_separada")), "hubs": meta.get("hubs", []),
        "conexion_min_horas": meta.get("conexion_min_horas", 4),
    }


def estimar(data: dict, existente: dict | None = None) -> dict:
    """Cuántas consultas hará la 1ra pasada con estos datos (y las combinaciones de fechas)."""
    f = normalizar_form(data)
    item = _aplicar(CommentedMap(existente or {}), f, nueva=existente is None)
    err: list[str] = []
    b = _validar_busqueda(dict(item), 0, dt.date.today(), err)
    if err:
        raise FormError(err)
    try:
        g = cargar_config().general
    except ConfigError:
        g = {}
    return estimar_busqueda(b, g=g)


# ----------------------------------------------------------------------------
# Operaciones
# ----------------------------------------------------------------------------
def crear(data: dict) -> dict:
    f = normalizar_form(data)
    with _lock:
        doc = _leer()
        try:
            _indice(doc, f["nombre"])
            raise FormError([f"Ya existe una búsqueda llamada '{f['nombre']}'."])
        except KeyError:
            pass
        item = _aplicar(CommentedMap(), f, nueva=True)
        _items(doc).append(item)
        _guardar(doc, f"Nueva búsqueda: {f['nombre']}")
    return f


def actualizar(nombre: str, data: dict) -> dict:
    """Edita una búsqueda. Si cambia el nombre, mueve su historial y suscriptores al nombre nuevo."""
    from . import database as db
    from . import suscriptores

    f = normalizar_form(data)
    with _lock:
        doc = _leer()
        i = _indice(doc, nombre)
        if f["nombre"].lower() != nombre.strip().lower():
            try:
                _indice(doc, f["nombre"])
                raise FormError([f"Ya existe una búsqueda llamada '{f['nombre']}'."])
            except KeyError:
                pass
        _aplicar(_items(doc)[i], f, nueva=False)
        _guardar(doc, f"Editar búsqueda: {f['nombre']}")
    if f["nombre"] != nombre:
        with db.conexion() as con:
            db.renombrar_busqueda(con, nombre, f["nombre"])
        suscriptores.renombrar_busqueda(nombre, f["nombre"])
    return f


def cambiar_estado(nombre: str, activa: bool) -> None:
    with _lock:
        doc = _leer()
        item = _items(doc)[_indice(doc, nombre)]
        item["activa"] = bool(activa)
        _guardar(doc, f"{'Reanudar' if activa else 'Pausar'} búsqueda: {nombre}")


def borrar(nombre: str) -> None:
    """Saca la búsqueda del config. El historial de precios queda en la base (no se pierde)."""
    with _lock:
        doc = _leer()
        del _items(doc)[_indice(doc, nombre)]
        _guardar(doc, f"Borrar búsqueda: {nombre}")


def item_crudo(nombre: str) -> dict | None:
    try:
        doc = _leer()
        return dict(_items(doc)[_indice(doc, nombre)])
    except KeyError:
        return None
