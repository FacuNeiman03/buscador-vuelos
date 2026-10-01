#!/usr/bin/env python3
"""Buscador de vuelos baratos — punto de entrada.

Uso (desde la carpeta del proyecto):
    python src/main.py                         # todas las búsquedas activas (1 vez por día)
    python src/main.py --forzar                # aunque ya haya corrido hoy
    python src/main.py --busqueda "China"      # una sola búsqueda (el índice sigue mostrando todas)
    python src/main.py --simular               # muestra cuántas consultas haría, sin consultar
    python src/main.py --solo-reporte          # regenera los reportes sin consultar precios
    python src/main.py --probar-email yo@mail.com
    python -m src.main ...                     # equivalente
"""
from __future__ import annotations

import sys
from pathlib import Path

# Permite `python src/main.py` además de `python -m src.main`
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "src"  # noqa: A001


def _asegurar_dependencias() -> None:
    """Instalación local sin pasos manuales: si falta algo, lo instala con el mismo Python."""
    import importlib.util
    import subprocess

    from src.core.paths import REQUIREMENTS

    faltan = [m for m in ("yaml", "ruamel", "fast_flights", "selectolax", "primp", "google.protobuf")
              if importlib.util.find_spec(m.split(".")[0]) is None]
    if faltan:
        subprocess.call([sys.executable, "-m", "pip", "install", "-q", "-r", str(REQUIREMENTS)])
    if importlib.util.find_spec("typing_extensions") is None:   # fast_flights solo usa override y Final
        import types
        import typing
        shim = types.ModuleType("typing_extensions")
        shim.override = getattr(typing, "override", lambda f: f)  # type: ignore[attr-defined]
        shim.Final = typing.Final  # type: ignore[attr-defined]
        sys.modules["typing_extensions"] = shim


_asegurar_dependencias()

import argparse  # noqa: E402
import datetime as dt  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import socket  # noqa: E402
import time  # noqa: E402
import webbrowser  # noqa: E402
from typing import Callable  # noqa: E402

from src.core import database as db  # noqa: E402
from src.core.config import AppConfig, ConfigError, cargar_config  # noqa: E402
from src.core.flight_searcher import correr_busqueda, crear_proveedor, estimar  # noqa: E402
from src.core.paths import (ESTADO_PATH, LOCK_PATH, LOG_PATH, LOGS_DIR, asegurar_directorios,  # noqa: E402
                            migrar_legado)
from src.notifications.mailer import email_de_prueba, notificar  # noqa: E402
from src.reporting.generator import generar_reportes  # noqa: E402

log = logging.getLogger("vuelos")


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def configurar_log() -> None:
    if log.handlers:
        return
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    if LOG_PATH.exists() and LOG_PATH.stat().st_size > 2_000_000:
        LOG_PATH.replace(LOG_PATH.with_suffix(".log.1"))
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    log.addHandler(fh)
    if sys.stdout is not None:   # con pythonw no hay consola
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except Exception:
            pass
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        log.addHandler(ch)
    log.setLevel(logging.INFO)


def esperar_internet(max_seg: int = 600) -> bool:
    if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
        return True   # detrás de un proxy no se puede probar con un socket directo
    t0 = time.time()
    while time.time() - t0 < max_seg:
        try:
            socket.create_connection(("www.google.com", 443), timeout=5).close()
            return True
        except OSError:
            time.sleep(15)
    return False


def _leer_estado() -> dict:
    try:
        return json.loads(ESTADO_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


class Lock:
    """Evita dos corridas simultáneas (PC + botón web, o dos .bat). Expira a las 3 h."""

    @staticmethod
    def activo() -> bool:
        return LOCK_PATH.exists() and time.time() - LOCK_PATH.stat().st_mtime < 3 * 3600

    def __enter__(self):
        if self.activo():
            raise RuntimeError("Ya hay una búsqueda corriendo.")
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOCK_PATH.write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        LOCK_PATH.unlink(missing_ok=True)


def simular(cfg: AppConfig, busquedas: list[dict]) -> None:
    total = 0
    print("Consultas previstas (estimación; el total real depende de los resultados):\n")
    for b in busquedas:
        e = estimar(b, g=cfg.general)
        total += e["consultas"]
        rango = f"{e['primera_ida']} a {e['ultima_ida']}" if e["combinaciones"] else "sin fechas válidas"
        explora = (f" (exploración {e['exploracion']} + detalle de {e['destinos_detalle']} destinos)"
                   if e["exploracion"] else "")
        print(f"  {b['nombre']:<28} {e['combinaciones']:>4} combinaciones · {e['consultas']:>5} consultas{explora}"
              f"  · ~{max(1, round(e['segundos'] / 60))} min · salidas {rango}")
    print(f"\n  TOTAL ≈ {total} consultas (límite por corrida: {cfg.general['max_consultas_por_corrida']}) · "
          f"velocidad: {cfg.general['velocidad']}")


# ----------------------------------------------------------------------------
# Pipeline reutilizable (CLI, servidor web, GitHub Actions)
# ----------------------------------------------------------------------------
def ejecutar(config: str | None = None, busqueda: str | None = None, forzar: bool = False,
             solo_reporte: bool = False, enviar_alertas: bool = True, proveedor: str | None = None,
             progreso: Callable[[str], None] | None = None, programado: bool = False) -> dict:
    """Corre las búsquedas, genera los reportes y manda alertas. Devuelve un resumen."""
    configurar_log()
    for m in migrar_legado():
        log.info(f"Migrado: {m}")
    asegurar_directorios()
    cfg = cargar_config(config)
    g = cfg.general

    if busqueda:
        b = cfg.buscar(busqueda)
        if not b:
            raise ConfigError(f"No existe la búsqueda '{busqueda}' en {cfg.ruta.name}. "
                              f"Disponibles: {', '.join(x['nombre'] for x in cfg.busquedas)}")
        busquedas = [b]
    elif programado:
        from src.core.programacion import pendientes
        busquedas = pendientes(cfg)
        log.info("Programado: " + (", ".join(b["nombre"] for b in busquedas) or "nada para correr ahora"))
    else:
        busquedas = cfg.activas

    resumen = {"corrio": False, "corridas": {}, "novedad": False, "emails": 0, "mensaje": ""}
    with db.conexion() as con:
        if not solo_reporte:
            hoy = dt.date.today().isoformat()
            if programado and not busquedas:
                resumen["mensaje"] = "No hay búsquedas programadas para esta hora."
            elif g["una_vez_por_dia"] and not forzar and not busqueda and not programado \
                    and _leer_estado().get("ultima_corrida") == hoy:
                resumen["mensaje"] = "Ya corrió hoy. Usá --forzar para correr de nuevo."
                log.info(resumen["mensaje"])
            else:
                with Lock():
                    if db.cerrar_corridas_colgadas(con):
                        log.info("Se cerraron corridas interrumpidas anteriores (sus datos quedan como parciales).")
                    if not esperar_internet():
                        raise RuntimeError("Sin internet, no se pudo buscar.")
                    prov = crear_proveedor(proveedor or g["proveedor"])
                    presupuesto = [g["max_consultas_por_corrida"]]
                    ok = []
                    for b in busquedas:
                        if progreso:
                            progreso(f"Buscando {b['nombre']}…")
                        ok.append(correr_busqueda(con, b, g, presupuesto, prov))
                        ult = con.execute("SELECT id, completa FROM corridas WHERE busqueda=? ORDER BY id DESC LIMIT 1",
                                          (b["nombre"],)).fetchone()
                        if ult and ult["completa"] in (db.COMPLETA, db.PARCIAL):
                            resumen["corridas"][b["nombre"]] = ult["id"]
                    resumen["corrio"] = True
                    if any(ok) and not busqueda:   # si todo falló (bloqueo/red) reintenta en el próximo arranque
                        ESTADO_PATH.write_text(json.dumps({"ultima_corrida": hoy}), encoding="utf-8")

        if resumen["corrio"]:
            try:
                n = db.compactar(con)
                if n:
                    log.info(f"Base compactada: {n} opciones secundarias de corridas viejas borradas.")
            except Exception as e:   # nunca debe frenar la corrida
                log.info(f"No se pudo compactar la base: {e}")
        if not resumen["corrio"] and not Lock.activo() and db.cerrar_corridas_colgadas(con):
            log.info("Se cerraron corridas interrumpidas anteriores (sus datos quedan como parciales).")
        if progreso:
            progreso("Generando reportes…")
        indice, novedad, _ = generar_reportes(con, cfg)
        resumen["indice"] = str(indice)
        resumen["novedad"] = novedad

        if enviar_alertas and resumen["corridas"]:
            if progreso:
                progreso("Revisando alertas…")
            resumen["emails"] = notificar(con, cfg, resumen["corridas"])
    return resumen


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Buscador de vuelos baratos (Google Flights)")
    ap.add_argument("--config", help="ruta a otro config.yaml (por defecto config/config.yaml)")
    ap.add_argument("--busqueda", help="correr solo la búsqueda con este nombre")
    ap.add_argument("--forzar", action="store_true", help="correr aunque ya haya corrido hoy")
    ap.add_argument("--programado", action="store_true",
                    help="nube: correr solo las búsquedas a las que les toca ahora (hora y ventana de cada una)")
    ap.add_argument("--solo-reporte", action="store_true", help="solo regenerar los reportes")
    ap.add_argument("--simular", action="store_true", help="mostrar cuántas consultas haría, sin consultar")
    ap.add_argument("--no-abrir", action="store_true", help="no abrir el reporte al terminar")
    ap.add_argument("--sin-email", action="store_true", help="no enviar alertas por email")
    ap.add_argument("--proveedor", choices=["auto", "fast_flights", "serpapi"], help="pisa general.proveedor")
    ap.add_argument("--probar-email", metavar="EMAIL", help="enviar un email de prueba con los datos actuales")
    args = ap.parse_args(argv)

    configurar_log()
    try:
        if args.simular or args.probar_email:
            cfg = cargar_config(args.config)
            if args.simular:
                bs = [cfg.buscar(args.busqueda)] if args.busqueda else cfg.activas
                simular(cfg, [b for b in bs if b])
                return 0
            migrar_legado()
            with db.conexion() as con:
                asunto = email_de_prueba(con, cfg, args.probar_email)
            log.info(f"Email de prueba enviado a {args.probar_email}: {asunto}")
            return 0

        res = ejecutar(args.config, args.busqueda, args.forzar, args.solo_reporte,
                       enviar_alertas=not args.sin_email, proveedor=args.proveedor, programado=args.programado)
    except ConfigError as e:
        log.error(str(e))
        return 2
    except RuntimeError as e:
        log.info(str(e))
        return 1

    cfg = cargar_config(args.config)
    modo = cfg.general["abrir_reporte"]
    en_ci = os.environ.get("CI") == "true"
    if not args.no_abrir and not en_ci and res.get("indice") and \
            (modo == "siempre" or (modo == "si_hay_novedad" and res["novedad"])):
        webbrowser.open(Path(res["indice"]).as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
