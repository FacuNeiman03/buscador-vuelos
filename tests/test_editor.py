import datetime as dt

import pytest

from src.core import config_editor as ce
from src.core import database as db
from src.core.config import cargar_config
from src.core.flight_searcher import ReglasFechas
from src.notifications import mailer
from src.reporting.metrics import datos_busqueda, filtro_rango
from tests.test_buscador import escribir_config

AÑO = dt.date.today().year + 1
CFG = """# comentario de cabecera que no se tiene que perder
general:
  moneda: USD
busquedas:
  - nombre: Cipolletti
    activa: true
    origenes: [AEP, EZE]
    destinos: [NQN]
    pasajeros: 2
    max_escalas: 1
    max_duracion_horas: 8   # comentario de línea
    salida_desde: "+1"
    salida_hasta: "+330"
    duracion_dias: [7]
    paso_dias: 2
"""


def form(**k):
    base = {"nombre": "Enero", "destinos": "NQN", "viajar_desde": f"{AÑO}-01-01", "viajar_hasta": f"{AÑO}-01-31",
            "dias_min": 7, "dias_max": 10, "pasajeros": 2}
    base.update(k)
    return base


@pytest.fixture(autouse=True)
def config_limpia():
    escribir_config(CFG)


def test_rango_de_viaje_ida_y_vuelta_adentro():
    ce.crear(form())
    b = cargar_config().buscar("Enero")
    combos = ReglasFechas(b).iniciales()
    ini, fin = dt.date(AÑO, 1, 1), dt.date(AÑO, 1, 31)
    assert combos
    assert all(ini <= i and v <= fin for i, v in combos), "ida y vuelta dentro del rango"
    assert {(v - i).days for i, v in combos} == {7, 8, 9, 10}
    assert b["origenes"] == ["AEP", "EZE"], "Buenos Aires por defecto"
    # vuelta <= 31/1: 7 días -> 24 salidas, 8 -> 23, 9 -> 22, 10 -> 21
    assert len(combos) == 90


def test_crear_conserva_comentarios_y_defaults():
    ce.crear(form())
    texto = ce._ruta().read_text(encoding="utf-8")
    assert "# comentario de cabecera" in texto and "# comentario de línea" in texto
    assert "viajar_desde:" in texto and "duracion_dias: {min: 7, max: 10}" in texto


def test_editar_existente_reemplaza_fechas_viejas_y_conserva_resto():
    f = ce.form_desde_meta(cargar_config().buscar("Cipolletti"))
    f.update(viajar_desde=f"{AÑO}-01-01", viajar_hasta=f"{AÑO}-03-31")
    ce.actualizar("Cipolletti", f)
    texto = ce._ruta().read_text(encoding="utf-8")
    assert "salida_desde" not in texto and "salida_hasta" not in texto
    b = cargar_config().buscar("Cipolletti")
    assert b["max_duracion_horas"] == 8 and b["paso_dias"] == 2
    assert all(i.month in (1, 2, 3) and v <= dt.date(AÑO, 3, 31) for i, v in ReglasFechas(b).iniciales())


def test_renombrar_mueve_historial():
    with db.conexion() as con:
        cid = db.iniciar_corrida(con, "Cipolletti", "t")
        db.finalizar_corrida(con, cid, 1, 0, db.COMPLETA)
    f = ce.form_desde_meta(cargar_config().buscar("Cipolletti"))
    f["nombre"] = "Cipo verano"
    ce.actualizar("Cipolletti", f)
    with db.conexion() as con:
        assert con.execute("SELECT busqueda FROM corridas WHERE id=?", (cid,)).fetchone()[0] == "Cipo verano"


def test_pausar_y_borrar():
    ce.crear(form())
    ce.cambiar_estado("Enero", False)
    assert not cargar_config().buscar("Enero")["activa"]
    ce.borrar("Enero")
    assert cargar_config().buscar("Enero") is None
    with pytest.raises(KeyError):
        ce.borrar("Enero")


@pytest.mark.parametrize("cambio, texto", [
    ({"destinos": ""}, "destino"),
    ({"destinos": "NEUQUEN"}, "código"),
    ({"viajar_hasta": f"{AÑO}-01-05"}, "no entra un viaje"),
    ({"viajar_desde": f"{AÑO}-02-01"}, "anterior"),
    ({"dias_min": 9, "dias_max": 5}, "mínimo"),
    ({"origenes": "NQN"}, "mismo aeropuerto"),
])
def test_form_invalido(cambio, texto):
    with pytest.raises(ce.FormError) as e:
        ce.crear(form(**cambio))
    assert texto in " ".join(e.value.errores)


def test_nombre_duplicado():
    with pytest.raises(ce.FormError, match="Ya existe"):
        ce.crear(form(nombre="cipolletti"))


def _corrida(con, nombre, filas):
    cid = db.iniciar_corrida(con, nombre, "t")
    for ida, vta, precio in filas:
        db.guardar_opciones(con, cid, nombre, "2026-01-01 10:00", "AEP", "NQN", ida, vta,
                            [{"precio": precio * 2, "aerolineas": "X", "escalas": 0, "duracion_min": 110,
                              "salida": "", "llegada": "", "ruta": "AEP → NQN"}], "USD", 2, "l", False)
    db.finalizar_corrida(con, cid, len(filas), 0, db.COMPLETA)
    return cid


def test_reporte_y_alertas_ignoran_fechas_fuera_del_rango(monkeypatch):
    ce.crear(form(nombre="Rango"))
    meta = cargar_config().buscar("Rango")
    junio = dt.date(AÑO, 6, 10)
    enero = dt.date(AÑO, 1, 10)
    with db.conexion() as con:
        # corrida vieja: junio baratísimo (fuera del rango actual) y enero a 100
        _corrida(con, "Rango", [(junio, junio + dt.timedelta(days=7), 30), (enero, enero + dt.timedelta(days=7), 100)])
        cid = _corrida(con, "Rango", [(junio, junio + dt.timedelta(days=7), 35), (enero, enero + dt.timedelta(days=7), 90)])
        d = datos_busqueda(con, "Rango", meta)
        assert d["stats"]["minimo"] == 90 and d["stats"]["maximo"] == 90, "junio no se muestra"
        assert d["hist_min"]["precio"] == 90
        info = mailer.detectar_mejora(con, "Rango", cid, filtro=filtro_rango(meta))
        assert info and info["motivo"] == "precio" and info["ref"]["precio"] == 100, "junio no bloquea el aviso"
