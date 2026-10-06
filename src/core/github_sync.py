"""Guarda archivos en el repositorio de GitHub vía API (opcional).

En Render (plan gratis) el disco se borra en cada deploy: lo que se edite desde la web
(búsquedas en config.yaml, suscriptores) se commitea al repo para que no se pierda y para que
el workflow diario lo use. Requiere GITHUB_TOKEN (fine-grained, Contents: read & write) y
GITHUB_REPOSITORY (usuario/repo). Sin esas variables no hace nada.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from pathlib import Path

from .paths import PROJECT_ROOT

log = logging.getLogger("vuelos")


def habilitado() -> bool:
    return bool(os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOSITORY"))


def subir(ruta_local: Path, contenido: bytes, mensaje: str) -> bool:
    """Crea/actualiza el archivo en el repo. Falla en silencio (el cambio local ya quedó guardado)."""
    if not habilitado():
        return False
    try:
        rel = ruta_local.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return False
    repo, rama = os.environ["GITHUB_REPOSITORY"], os.environ.get("GITHUB_BRANCH", "main")
    url = f"https://api.github.com/repos/{repo}/contents/{rel}"
    headers = {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json",
               "User-Agent": "buscador-vuelos", "X-GitHub-Api-Version": "2022-11-28"}
    for _ in range(3):   # reintenta si otro commit cambió el sha en el medio
        sha = None
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{url}?ref={rama}", headers=headers), timeout=20) as r:
                sha = json.loads(r.read())["sha"]
        except urllib.error.HTTPError as e:
            if e.code != 404:
                log.info(f"GitHub: no se pudo leer {rel} ({e.code})")
                return False
        except OSError as e:
            log.info(f"GitHub: sin conexión ({e})")
            return False
        cuerpo = {"message": f"{mensaje} [skip ci] [skip render]", "branch": rama, "content": base64.b64encode(contenido).decode()}
        if sha:
            cuerpo["sha"] = sha
        req = urllib.request.Request(url, data=json.dumps(cuerpo).encode(), headers=headers, method="PUT")
        try:
            with urllib.request.urlopen(req, timeout=20):
                return True
        except urllib.error.HTTPError as e:
            if e.code != 409:
                log.info(f"GitHub: no se pudo guardar {rel} ({e.code})")
                return False
        except OSError as e:
            log.info(f"GitHub: sin conexión ({e})")
            return False
    return False


# ----------------------------------------------------------------------------
# Modo nube: la web (Render) delega las búsquedas a GitHub Actions y baja los resultados
# Permisos del token fine-grained: Contents (read & write) + Actions (read & write).
# ----------------------------------------------------------------------------
WORKFLOW = os.environ.get("GITHUB_WORKFLOW_FILE", "busqueda_diaria.yml")
RAMA_DATOS = os.environ.get("GITHUB_RAMA_DATOS", "datos")


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}", "Accept": "application/vnd.github+json",
            "User-Agent": "buscador-vuelos", "X-GitHub-Api-Version": "2022-11-28"}


def _api(metodo: str, ruta: str, cuerpo: dict | None = None, timeout: int = 20):
    """Llama a la API de GitHub. Devuelve (status, json|None). Lanza OSError si no hay red."""
    url = f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}{ruta}"
    data = json.dumps(cuerpo).encode() if cuerpo is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(), method=metodo)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read()
            return r.status, (json.loads(txt) if txt else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None


def disparar_busqueda(busqueda: str | None) -> tuple[bool, str]:
    """Lanza la búsqueda en GitHub Actions (workflow_dispatch). Devuelve (ok, mensaje de error)."""
    return disparar_workflow(WORKFLOW, {"busqueda": busqueda or ""})


def disparar_workflow(archivo: str, inputs: dict) -> tuple[bool, str]:
    rama = os.environ.get("GITHUB_BRANCH", "main")
    try:
        st, j = _api("POST", f"/actions/workflows/{archivo}/dispatches", {"ref": rama, "inputs": inputs})
    except OSError as e:
        return False, f"sin conexión con GitHub ({e})"
    if st == 204:
        return True, ""
    detalle = (j or {}).get("message", "") if isinstance(j, dict) else ""
    if st in (401, 403):
        return False, f"GitHub rechazó el token ({st}): revisá que tenga permiso Actions: Read and write. {detalle}"
    if st == 404:
        return False, f"GitHub no encuentra el workflow {archivo} en {os.environ['GITHUB_REPOSITORY']} (404)"
    return False, f"GitHub respondió {st}: {detalle}"


def ejecuciones(n: int = 5) -> list[dict]:
    """Últimas ejecuciones del workflow (más nuevas primero). [] si no se pudo consultar."""
    try:
        st, j = _api("GET", f"/actions/workflows/{WORKFLOW}/runs?per_page={n}")
    except OSError:
        return []
    if st != 200 or not isinstance(j, dict):
        return []
    return [{"id": r["id"], "evento": r["event"], "estado": r["status"], "conclusion": r.get("conclusion"),
             "creado": r["created_at"], "iniciado": r.get("run_started_at") or r["created_at"],
             "url": r["html_url"], "titulo": r.get("display_title") or ""} for r in j.get("workflow_runs", [])]


def sha_datos() -> str | None:
    """Commit actual de la rama de datos (None si todavía no existe o no se pudo consultar)."""
    try:
        st, j = _api("GET", f"/branches/{RAMA_DATOS}")
    except OSError:
        return None
    return j["commit"]["sha"] if st == 200 and isinstance(j, dict) else None


def bajar_datos(destino_data: Path) -> bool:
    """Baja historial.db y estado.json de la rama de datos (tarball) y los deja en destino_data."""
    import io
    import tarfile
    url = f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/tarball/{RAMA_DATOS}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=_headers()), timeout=60) as r:
            blob = r.read()
    except (urllib.error.HTTPError, OSError) as e:
        log.info(f"GitHub: no se pudo bajar la rama {RAMA_DATOS} ({e})")
        return False
    destino_data.mkdir(parents=True, exist_ok=True)
    bajados = 0
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for m in tar.getmembers():
            partes = m.name.split("/", 1)          # "<usuario>-<repo>-<sha>/data/historial.db"
            if len(partes) == 2 and m.isfile() and partes[1] in ("data/historial.db", "data/estado.json"):
                final = destino_data / Path(partes[1]).name
                tmp = final.with_suffix(final.suffix + ".bajando")
                tmp.write_bytes(tar.extractfile(m).read())
                os.replace(tmp, final)
                bajados += 1
    return bajados > 0


def bajar_archivo(ruta_local: Path) -> bool:
    """Trae la versión del repo (rama main) de un archivo versionado (config.yaml, suscriptores).

    En Render el disco vuelve al estado del último deploy cada vez que la instancia se despierta:
    así no se pierden las ediciones hechas desde la web después de ese deploy."""
    try:
        rel = ruta_local.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return False
    rama = os.environ.get("GITHUB_BRANCH", "main")
    url = f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}/contents/{rel}?ref={rama}"
    h = {**_headers(), "Accept": "application/vnd.github.raw"}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=20) as r:
            contenido = r.read()
    except urllib.error.HTTPError as e:
        if e.code != 404:
            log.info(f"GitHub: no se pudo bajar {rel} ({e.code})")
        return False
    except OSError as e:
        log.info(f"GitHub: sin conexión ({e})")
        return False
    ruta_local.parent.mkdir(parents=True, exist_ok=True)
    tmp = ruta_local.with_suffix(ruta_local.suffix + ".bajando")
    tmp.write_bytes(contenido)
    os.replace(tmp, ruta_local)
    return True
