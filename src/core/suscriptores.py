"""Suscriptores a alertas guardados en data/suscriptores.json.

¿Por qué un JSON y no la base SQLite? En la nube gratuita (Render) el disco se borra en cada
deploy, y la base la reescribe GitHub Actions todos los días. El JSON vive en el repositorio:
  * el servidor web lo actualiza localmente y lo commitea al repo (ver github_sync.py);
  * el workflow diario lo lee para saber a quién avisar.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import secrets
import threading

from . import github_sync
from .paths import DATA_DIR

log = logging.getLogger("vuelos")
_lock = threading.Lock()


def ruta():
    return DATA_DIR / "suscriptores.json"


_ruta = ruta


# --- Cifrado opcional -------------------------------------------------------------------------
# Si el repo es público, los emails no pueden quedar a la vista: con VUELOS_CLAVE definida (la misma
# en Render y en los secretos de GitHub Actions) el archivo se guarda cifrado: {"cifrado": "..."}.
def _fernet():
    clave = os.environ.get("VUELOS_CLAVE", "")
    if not clave:
        return None
    import base64
    import hashlib
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(clave.encode()).digest()))


def _serializar(lista: list[dict]) -> str:
    txt = json.dumps(lista, ensure_ascii=False, indent=2)
    f = _fernet()
    return json.dumps({"cifrado": f.encrypt(txt.encode()).decode()}, indent=2) if f else txt


def cargar() -> list[dict]:
    try:
        datos = json.loads(_ruta().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if isinstance(datos, dict) and "cifrado" in datos:
        f = _fernet()
        if f is None:
            log.info("suscriptores.json está cifrado pero falta VUELOS_CLAVE: no se avisa a suscriptores")
            return []
        try:
            datos = json.loads(f.decrypt(datos["cifrado"].encode()))
        except Exception:
            log.info("No se pudo descifrar suscriptores.json (¿VUELOS_CLAVE distinta en Render y GitHub?)")
            return []
    return datos if isinstance(datos, list) else []


def _guardar(lista: list[dict]) -> None:
    r = _ruta()
    r.parent.mkdir(parents=True, exist_ok=True)
    texto = _serializar(lista)
    tmp = r.with_suffix(".tmp")
    tmp.write_text(texto, encoding="utf-8")
    os.replace(tmp, r)
    github_sync.subir(r, texto.encode(), "Actualizar suscriptores")


def agregar(email: str, busqueda: str, desde: str | None, hasta: str | None,
            confirmado: bool = True) -> tuple[str, bool]:
    """Alta o actualización. Devuelve (token, ya_confirmado).

    Con confirmado=False (la web pública) la suscripción queda pendiente hasta que la persona abre el
    link del mail de confirmación: así nadie puede anotar emails ajenos para que les lleguen alertas."""
    email = email.strip().lower()
    with _lock:
        lista = cargar()
        for s in lista:
            if s["email"] == email and s["busqueda"] == busqueda:
                s.update(desde=desde or None, hasta=hasta or None)
                if s.get("confirmado", True) and s.get("activo", True):
                    _guardar(lista)
                    return s["token"], True
                s.update(activo=confirmado, confirmado=confirmado)
                _guardar(lista)
                return s["token"], confirmado
        token = secrets.token_urlsafe(24)
        lista.append({"email": email, "busqueda": busqueda, "desde": desde or None, "hasta": hasta or None,
                      "token": token, "activo": confirmado, "confirmado": confirmado,
                      "creado": dt.datetime.now().strftime("%Y-%m-%d %H:%M")})
        _guardar(lista)
        return token, confirmado


def confirmar(token: str) -> dict | None:
    with _lock:
        lista = cargar()
        for s in lista:
            if s.get("token") == token:
                if not (s.get("confirmado", True) and s.get("activo", True)):
                    s.update(confirmado=True, activo=True)
                    _guardar(lista)
                return s
    return None


def dar_de_baja(token: str) -> bool:
    with _lock:
        lista = cargar()
        for s in lista:
            if s.get("token") == token and s.get("activo", True):
                s["activo"] = False
                _guardar(lista)
                return True
    return False


def de_busqueda(busqueda: str) -> list[dict]:
    return [s for s in cargar() if s.get("busqueda") == busqueda and s.get("activo", True)
            and s.get("confirmado", True)]


def renombrar_busqueda(viejo: str, nuevo: str) -> None:
    with _lock:
        lista = cargar()
        if any(s.get("busqueda") == viejo for s in lista):
            for s in lista:
                if s.get("busqueda") == viejo:
                    s["busqueda"] = nuevo
            _guardar(lista)
