"""Acceso a SQLite: esquema con migraciones versionadas, escritura de corridas y consultas.

Estados de una corrida (columna `completa`):
    0 = en curso o fallida (no se usa en reportes)
    1 = completa
    2 = parcial: se cortó a mitad (PC apagada, proceso cerrado) pero dejó precios útiles
"""
from __future__ import annotations

import datetime as dt
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .paths import DB_PATH

COMPLETA, PARCIAL = 1, 2
ESQUEMA_VERSION = 4


def _ahora() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M")


def _columnas(con: sqlite3.Connection, tabla: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({tabla})")}


def _migrar(con: sqlite3.Connection) -> None:
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS corridas(
            id INTEGER PRIMARY KEY AUTOINCREMENT, busqueda TEXT, inicio TEXT, fin TEXT,
            consultas INT DEFAULT 0, errores INT DEFAULT 0, completa INT DEFAULT 0);
        CREATE TABLE IF NOT EXISTS precios(
            corrida_id INT, busqueda TEXT, consultado TEXT, origen TEXT, destino TEXT,
            ida TEXT, vuelta TEXT, precio REAL, moneda TEXT, pasajeros INT, aerolineas TEXT,
            escalas INT, duracion_min INT, salida TEXT, llegada TEXT, ruta TEXT, link TEXT);
        CREATE INDEX IF NOT EXISTS ix_precios ON precios(busqueda, corrida_id);
        """)
        if "flexible" not in _columnas(con, "precios"):
            con.execute("ALTER TABLE precios ADD COLUMN flexible INT DEFAULT 0")
    if version < 2:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS notificaciones(
            id INTEGER PRIMARY KEY AUTOINCREMENT, enviado TEXT, busqueda TEXT, email TEXT,
            corrida_id INT, precio REAL, escalas INT, duracion_min INT, ida TEXT, vuelta TEXT,
            motivo TEXT, ok INT);
        CREATE INDEX IF NOT EXISTS ix_precios_min ON precios(busqueda, flexible, precio);
        CREATE INDEX IF NOT EXISTS ix_corridas ON corridas(busqueda, completa, id);
        """)
        if "proveedor" not in _columnas(con, "corridas"):
            con.execute("ALTER TABLE corridas ADD COLUMN proveedor TEXT")
    if version < 3:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS exploraciones(
            corrida_id INT, busqueda TEXT, consultado TEXT, origen TEXT, destino TEXT, ida TEXT, vuelta TEXT,
            precio REAL, pasajeros INT, moneda TEXT, aerolineas TEXT, escalas INT, link TEXT, elegido INT DEFAULT 0);
        CREATE INDEX IF NOT EXISTS ix_exploraciones ON exploraciones(corrida_id);
        """)
    if version < 4:
        # tipo: ida_vuelta | dos_solo_ida | escala_separada · equipaje: 0 ninguno, 1 mano, 2 despachado
        # detalle: JSON con los tramos cuando el precio se arma con varios pasajes
        cols = _columnas(con, "precios")
        for col, decl in (("tipo", "TEXT DEFAULT 'ida_vuelta'"), ("equipaje", "INT DEFAULT 0"), ("detalle", "TEXT")):
            if col not in cols:
                con.execute(f"ALTER TABLE precios ADD COLUMN {col} {decl}")
    con.execute(f"PRAGMA user_version = {ESQUEMA_VERSION}")
    con.commit()


def abrir_db(ruta: Path | str | None = None) -> sqlite3.Connection:
    ruta = Path(ruta) if ruta else DB_PATH
    ruta.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(ruta, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=DELETE")   # un solo archivo .db (fácil de commitear en git)
    _migrar(con)
    return con


@contextmanager
def conexion(ruta: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    con = abrir_db(ruta)
    try:
        yield con
    finally:
        con.close()


# ----------------------------------------------------------------------------
# Escritura de corridas
# ----------------------------------------------------------------------------
def cerrar_corridas_colgadas(con: sqlite3.Connection) -> int:
    """Corridas que quedaron abiertas (se apagó la PC). Si guardaron precios pasan a PARCIAL."""
    colgadas = con.execute("SELECT id FROM corridas WHERE fin IS NULL").fetchall()
    for r in colgadas:
        tiene = con.execute("SELECT 1 FROM precios WHERE corrida_id=? LIMIT 1", (r["id"],)).fetchone()
        ultimo = con.execute("SELECT MAX(consultado) FROM precios WHERE corrida_id=?", (r["id"],)).fetchone()[0]
        con.execute("UPDATE corridas SET fin=?, completa=? WHERE id=?",
                    (ultimo or _ahora(), PARCIAL if tiene else 0, r["id"]))
    con.commit()
    return len(colgadas)


def iniciar_corrida(con: sqlite3.Connection, busqueda: str, proveedor: str) -> int:
    cur = con.execute("INSERT INTO corridas(busqueda, inicio, proveedor) VALUES (?, ?, ?)",
                      (busqueda, _ahora(), proveedor))
    con.commit()
    return int(cur.lastrowid)


def finalizar_corrida(con: sqlite3.Connection, corrida_id: int, consultas: int, errores: int, completa: int) -> None:
    con.execute("UPDATE corridas SET fin=?, consultas=?, errores=?, completa=? WHERE id=?",
                (_ahora(), consultas, errores, completa, corrida_id))
    con.commit()


def guardar_opciones(con: sqlite3.Connection, corrida_id: int, busqueda: str, consultado: str,
                     origen: str, destino: str, ida: dt.date, vuelta: dt.date | None,
                     opciones: list[dict], moneda: str, pasajeros: int, link: str, flexible: bool,
                     equipaje: int = 0, commit: bool = True) -> None:
    """Guarda opciones de precio. Cada opción puede traer 'tipo' y 'detalle' (lista de tramos)."""
    con.executemany(
        "INSERT INTO precios(corrida_id, busqueda, consultado, origen, destino, ida, vuelta, precio, moneda, "
        "pasajeros, aerolineas, escalas, duracion_min, salida, llegada, ruta, link, flexible, tipo, equipaje, detalle) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [(corrida_id, busqueda, consultado, origen, destino, ida.isoformat(),
          vuelta.isoformat() if vuelta else None, op["precio"], moneda, pasajeros, op["aerolineas"],
          op["escalas"], op["duracion_min"], op["salida"], op["llegada"], op["ruta"],
          op.get("link") or link, 1 if flexible else 0, op.get("tipo") or "ida_vuelta", equipaje,
          json.dumps(op["detalle"], ensure_ascii=False) if op.get("detalle") else None) for op in opciones])
    if commit:
        con.commit()


def guardar_exploracion(con: sqlite3.Connection, corrida_id: int, busqueda: str, consultado: str, origen: str,
                        destino: str, ida: dt.date, vuelta: dt.date | None, opcion: dict, pasajeros: int,
                        moneda: str, link: str) -> None:
    con.execute(
        "INSERT INTO exploraciones(corrida_id, busqueda, consultado, origen, destino, ida, vuelta, precio, pasajeros, "
        "moneda, aerolineas, escalas, link) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (corrida_id, busqueda, consultado, origen, destino, ida.isoformat(), vuelta.isoformat() if vuelta else None,
         opcion["precio"], pasajeros, moneda, opcion["aerolineas"], opcion["escalas"], link))
    con.commit()


def marcar_elegidos(con: sqlite3.Connection, corrida_id: int, destinos: list[str]) -> None:
    con.executemany("UPDATE exploraciones SET elegido=1 WHERE corrida_id=? AND destino=?",
                    [(corrida_id, d) for d in destinos])
    con.commit()


def exploracion(con: sqlite3.Connection, corrida_id: int) -> list[dict]:
    """Mejor precio de muestra por destino en la exploración de esa corrida (más barato primero)."""
    filas = [dict(r) for r in con.execute(
        "SELECT *, precio*1.0/pasajeros pp FROM exploraciones WHERE corrida_id=? ORDER BY pp", (corrida_id,))]
    mejor: dict[str, dict] = {}
    for f in filas:
        mejor.setdefault(f["destino"], f)
    return list(mejor.values())


# ----------------------------------------------------------------------------
# Consultas para reportes y alertas
# ----------------------------------------------------------------------------
def busquedas_con_datos(con: sqlite3.Connection) -> list[str]:
    """Nombres de búsquedas con al menos una corrida utilizable, la más reciente primero."""
    return [r[0] for r in con.execute(
        "SELECT busqueda FROM corridas WHERE completa IN (1,2) "
        "AND EXISTS (SELECT 1 FROM precios p WHERE p.corrida_id=corridas.id) "
        "GROUP BY busqueda ORDER BY MAX(id) DESC")]


def corridas_utiles(con: sqlite3.Connection, busqueda: str) -> list[sqlite3.Row]:
    """Corridas completas + parciales "representativas" (con al menos la mitad de las combinaciones
    que suele tener una corrida completa). Una parcial de 3 consultas no debe tapar a la completa de ayer."""
    filas = con.execute(
        "SELECT c.*, (SELECT COUNT(DISTINCT ida || '|' || COALESCE(vuelta,'') || origen || destino) "
        "             FROM precios p WHERE p.corrida_id=c.id) AS combos "
        "FROM corridas c WHERE busqueda=? AND completa IN (1,2) ORDER BY id", (busqueda,)).fetchall()
    filas = [f for f in filas if f["combos"]]
    completas = [f["combos"] for f in filas if f["completa"] == COMPLETA]
    if not completas:
        return filas
    umbral = max(completas) * 0.5
    return [f for f in filas if f["completa"] == COMPLETA or f["combos"] >= umbral]


def filas_corrida(con: sqlite3.Connection, corrida_id: int) -> list[dict]:
    filas = [dict(r) for r in con.execute(
        "SELECT * FROM precios WHERE corrida_id=? ORDER BY precio*1.0/pasajeros, escalas, duracion_min",
        (corrida_id,))]
    for f in filas:
        f["tipo"] = f.get("tipo") or "ida_vuelta"
        f["equipaje"] = f.get("equipaje") or 0
        try:
            f["detalle"] = json.loads(f["detalle"]) if f.get("detalle") else None
        except ValueError:
            f["detalle"] = None
    return filas


def _en(f, filtro) -> bool:
    """filtro(fila) -> bool, con fila = dict o sqlite3.Row (ida, vuelta, equipaje...)."""
    return filtro is None or filtro(f)


def evolucion(con: sqlite3.Connection, busqueda: str, filtro=None) -> list[dict]:
    """Mínimo / promedio / máximo por persona de cada corrida (solo combinaciones que pasan el filtro).

    Igual que en los KPIs, se toma la opción más barata de CADA combinación de fechas: si no, la 2da y 3ra
    opción de cada consulta inflaban el promedio y el máximo (y cambiaban al compactar la base)."""
    out = []
    for c in corridas_utiles(con, busqueda):
        por_combo: dict[tuple, float] = {}
        for r in con.execute(
                "SELECT precio*1.0/pasajeros pp, ida, vuelta, origen, destino, COALESCE(equipaje,0) equipaje "
                "FROM precios WHERE corrida_id=? AND COALESCE(flexible,0)=0", (c["id"],)):
            if _en(r, filtro):
                k = (r["ida"], r["vuelta"], r["origen"], r["destino"], r["equipaje"])
                por_combo[k] = min(por_combo.get(k, r["pp"]), r["pp"])
        precios = list(por_combo.values())
        if precios:
            out.append({"id": c["id"], "dia": c["inicio"][:10], "precio": min(precios),
                        "maximo": max(precios), "promedio": sum(precios) / len(precios)})
    return out


def compactar(con: sqlite3.Connection, dias: int = 14) -> int:
    """Achica la base: en corridas de hace más de `dias` deja solo la opción más barata de cada combinación
    (fechas, ruta, equipaje, tipo). Los reportes de corridas viejas (evolución, mínimo histórico, alertas)
    solo usan esa fila, así que no cambia nada visible. Sin esto la base crece ~1,5 MB por día y, si se
    commitea a GitHub, a los ~2 meses pasa el límite de 100 MB por archivo y el push falla."""
    limite = (dt.datetime.now() - dt.timedelta(days=dias)).strftime("%Y-%m-%d %H:%M")
    cur = con.execute("""
        DELETE FROM precios WHERE rowid IN (
          SELECT rid FROM (
            SELECT p.rowid rid, ROW_NUMBER() OVER (
                     PARTITION BY p.corrida_id, p.ida, COALESCE(p.vuelta, ''), p.origen, p.destino,
                                  COALESCE(p.equipaje, 0), COALESCE(p.flexible, 0), COALESCE(p.tipo, 'ida_vuelta')
                     ORDER BY p.precio, p.escalas, p.duracion_min) rn
            FROM precios p JOIN corridas c ON c.id = p.corrida_id
            WHERE c.inicio < ? AND c.fin IS NOT NULL)
          WHERE rn > 1)""", (limite,))
    borradas = cur.rowcount or 0
    con.commit()
    if borradas >= 2000:
        con.execute("VACUUM")   # devuelve el espacio al disco (el archivo .db se achica)
    return borradas


def minimo_historico(con: sqlite3.Connection, busqueda: str, antes_de_corrida: int | None = None,
                     desde: str | None = None, hasta: str | None = None, filtro=None) -> dict | None:
    """Precio/persona más bajo registrado (a igual precio: menos escalas y menor duración).

    desde/hasta acotan la fecha de ida; `filtro(ida, vuelta)` aplica el rango vigente de la búsqueda.
    """
    sql = ("SELECT precio*1.0/pasajeros precio, escalas, duracion_min, ida, vuelta, consultado, aerolineas, "
           "COALESCE(equipaje,0) equipaje, COALESCE(tipo,'ida_vuelta') tipo "
           "FROM precios WHERE busqueda=? AND COALESCE(flexible,0)=0")
    args: list = [busqueda]
    if antes_de_corrida is not None:
        sql += " AND corrida_id < ?"
        args.append(antes_de_corrida)
    if desde:
        sql += " AND ida >= ?"
        args.append(desde)
    if hasta:
        sql += " AND ida <= ?"
        args.append(hasta)
    for r in con.execute(sql + " ORDER BY precio*1.0/pasajeros, escalas, duracion_min", args):
        if _en(r, filtro):
            return dict(r)
    return None


def renombrar_busqueda(con: sqlite3.Connection, viejo: str, nuevo: str) -> None:
    """Mueve todo el historial de una búsqueda a su nombre nuevo."""
    for tabla in ("corridas", "precios", "notificaciones", "exploraciones"):
        con.execute(f"UPDATE {tabla} SET busqueda=? WHERE busqueda=?", (nuevo, viejo))
    con.commit()


# ----------------------------------------------------------------------------
# Registro de notificaciones (evita repetir avisos)
# ----------------------------------------------------------------------------
def registrar_notificacion(con: sqlite3.Connection, busqueda: str, email: str, corrida_id: int,
                           opcion: dict, motivo: str, ok: bool) -> None:
    con.execute(
        "INSERT INTO notificaciones(enviado, busqueda, email, corrida_id, precio, escalas, duracion_min, ida, "
        "vuelta, motivo, ok) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (_ahora(), busqueda, email, corrida_id, opcion["precio"], opcion["escalas"], opcion["duracion_min"],
         opcion["ida"], opcion["vuelta"], motivo, 1 if ok else 0))
    con.commit()


def ya_notificado(con: sqlite3.Connection, busqueda: str, email: str, corrida_id: int) -> bool:
    return con.execute("SELECT 1 FROM notificaciones WHERE busqueda=? AND email=? AND corrida_id=? AND ok=1",
                       (busqueda, email, corrida_id)).fetchone() is not None
