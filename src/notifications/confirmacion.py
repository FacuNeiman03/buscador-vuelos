"""Mail de confirmación de suscripciones (doble opt-in).

Render (plan gratis) bloquea los puertos SMTP, así que en modo nube la web no manda el mail:
dispara el workflow confirmacion.yml con una REFERENCIA (hash del token, nunca el email ni el
token, porque los parámetros de un workflow se ven en los repos públicos) y este módulo, corriendo
en GitHub Actions, busca la suscripción pendiente y le manda el link.

Uso:  python -m src.notifications.confirmacion <referencia>
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys
from html import escape
from urllib.parse import quote

from ..core import suscriptores as subs

log = logging.getLogger("vuelos")


def referencia(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:24]


def componer(busqueda: str, token: str, base: str) -> tuple[str, str, str]:
    link = f"{base.rstrip('/')}/confirmar?token={quote(token)}"
    asunto = f"Confirmá tus alertas de vuelos: {busqueda}"
    texto = (f"Alguien (seguramente vos) pidió alertas de precio para '{busqueda}' a este mail.\n\n"
             f"Para confirmarlo abrí: {link}\n\nSi no fuiste vos, ignorá este mail y no vas a recibir nada.")
    html = (f"<p>Alguien (seguramente vos) pidió alertas de precio para <b>{escape(busqueda)}</b> a este mail.</p>"
            f'<p><a href="{escape(link)}" style="display:inline-block;padding:10px 16px;background:#0b57d0;'
            f'color:#fff;border-radius:8px;text-decoration:none">Confirmar alertas</a></p>'
            f"<p style='color:#666'>Si no fuiste vos, ignorá este mail y no vas a recibir nada.</p>")
    return asunto, html, texto


def enviar(email: str, busqueda: str, token: str, base: str) -> None:
    from .mailer import enviar_email
    asunto, html, texto = componer(busqueda, token, base)
    enviar_email(email, asunto, html, texto)


def enviar_pendiente(ref: str) -> bool:
    base = os.environ.get("VUELOS_URL_WEB", "")
    if not base:
        raise RuntimeError("Falta la variable VUELOS_URL_WEB (la URL de la web, para armar el link)")
    for s in subs.cargar():
        if s.get("token") and referencia(s["token"]) == ref:
            if s.get("confirmado", True) and s.get("activo", True):
                print("Esa suscripción ya estaba confirmada: no se manda nada.")
                return True
            enviar(s["email"], s["busqueda"], s["token"], base)
            print("Mail de confirmación enviado.")
            return True
    print("No se encontró la suscripción (¿VUELOS_CLAVE distinta en Render y GitHub?)", file=sys.stderr)
    return False


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(0 if enviar_pendiente(sys.argv[1].strip()) else 1)
