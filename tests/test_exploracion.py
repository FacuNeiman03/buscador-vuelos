from src.core import aeropuertos
from src.core import config_editor as ce
from src.core import database as db
from src.core.config import cargar_config
from src.core.flight_searcher import correr_busqueda, estimar
from src.reporting.metrics import datos_busqueda
from tests.test_buscador import Falso, escribir_config
from tests.test_editor import AÑO, CFG


class PorDestino(Falso):
    """Precio según el destino: cuanto más 'alto' el índice en la lista, más barato."""
    def __init__(self, orden):
        super().__init__()
        self.orden = orden

    def buscar(self, b, g, origen, destino, ida, vuelta):
        ops, link = super().buscar(b, g, origen, destino, ida, vuelta)
        ops[0]["precio"] = 1000 - 10 * self.orden.index(destino) + ida.day
        return ops, link


def test_base_de_aeropuertos():
    assert aeropuertos.info("NQN")["pais"] == "AR"
    assert aeropuertos.nombre_pais("BR") == "Brasil"
    br = aeropuertos.candidatos_pais("BR")
    assert "GRU" in br and "GIG" in br and len(br) <= aeropuertos.MAX_CANDIDATOS_PAIS
    assert len(aeropuertos.candidatos_pais("UY")) >= 2, "países con pocos grandes suman medianos"


def test_explorar_pais_y_buscar_detalle_de_los_mas_baratos():
    escribir_config(CFG)
    destinos = aeropuertos.candidatos_pais("BR")[:8]
    ce.crear({"nombre": "Brasil", "destinos": destinos, "destino_pais": "BR", "explorar_top": 2,
              "viajar_desde": f"{AÑO}-01-01", "viajar_hasta": f"{AÑO}-01-20", "dias_min": 7, "dias_max": 7,
              "estrategia": "ida_vuelta"})
    cfg = cargar_config()
    b = cfg.buscar("Brasil")
    e = estimar(b)
    assert e["exploracion"] == 8 * 3 and e["destinos_detalle"] == 2
    assert e["detalle"] == e["combinaciones"] * 2 * 2

    prov = PorDestino(destinos)
    with db.conexion() as con:
        assert correr_busqueda(con, b, cfg.general, [5000], prov, dormir=lambda s: None)
        cid = con.execute("SELECT MAX(id) FROM corridas WHERE busqueda='Brasil'").fetchone()[0]
        exp = db.exploracion(con, cid)
        detalle = {r[0] for r in con.execute("SELECT DISTINCT destino FROM precios WHERE corrida_id=?", (cid,))}
        d = datos_busqueda(con, "Brasil", b)
    assert len(exp) == 8
    assert detalle == set(destinos[-2:]), "solo los 2 más baratos se buscan en detalle"
    assert {x["destino"] for x in exp if x["elegido"]} == set(destinos[-2:])
    assert prov.llamadas >= e["consultas"] - e["horarios_vuelta"]
    assert d["multi_destino"] and len(d["exploracion"]) == 8 and "Brasil" in d["ruta"]
    assert d["mejor"]["destino"] == destinos[-1]


class SinVuelos(Falso):
    def buscar(self, b, g, origen, destino, ida, vuelta):
        self.llamadas += 1
        return [], "https://example.com"


def test_pais_preselecciona_por_trafico_y_no_alfabetico():
    cn = aeropuertos.candidatos_pais("CN")
    assert {"PVG", "PEK", "CAN", "SZX"} <= set(cn), "Shanghái/Shenzhen no pueden quedar afuera por orden alfabético"
    assert cn.index("PEK") < cn.index("CSX")


def test_exploracion_sin_resultados_no_busca_detalle():
    escribir_config(CFG)
    destinos = aeropuertos.candidatos_pais("BR")[:6]
    ce.crear({"nombre": "Nada", "destinos": destinos, "destino_pais": "BR", "explorar_top": 2,
              "viajar_desde": f"{AÑO}-01-01", "viajar_hasta": f"{AÑO}-01-20", "dias_min": 7, "dias_max": 7,
              "estrategia": "ida_vuelta"})
    cfg = cargar_config()
    b = cfg.buscar("Nada")
    prov = SinVuelos()
    with db.conexion() as con:
        correr_busqueda(con, b, cfg.general, [5000], prov, dormir=lambda s: None)
    assert prov.llamadas == 6 * 3, "solo la exploración; antes seguía con los 2 primeros de la lista"


def test_compactar_deja_la_mejor_opcion_por_combinacion():
    import sqlite3
    with db.conexion() as con:
        cid = db.iniciar_corrida(con, "Compactar", "test")
        con.execute("UPDATE corridas SET inicio='2000-01-01 10:00', fin='2000-01-01 11:00', completa=1 WHERE id=?",
                    (cid,))
        ops = [{"precio": p, "aerolineas": "X", "escalas": 1, "duracion_min": 600, "salida": "", "llegada": "",
                "ruta": "EZE → GRU"} for p in (300, 100, 200)]
        import datetime as dt
        db.guardar_opciones(con, cid, "Compactar", "2000-01-01 10:00", "EZE", "GRU", dt.date(2000, 2, 1),
                            dt.date(2000, 2, 8), ops, "USD", 1, "l", flexible=False)
        antes = db.evolucion(con, "Compactar")
        assert db.compactar(con) == 2
        precios = [r[0] for r in con.execute("SELECT precio FROM precios WHERE corrida_id=?", (cid,))]
        assert precios == [100]
        assert db.evolucion(con, "Compactar") == antes


def test_ventana_de_ejecucion(tmp_path):
    import datetime as dt
    from src.core.config import cargar_config
    y = tmp_path / "c.yaml"
    y.write_text("""general: {moneda: USD}
busquedas:
  - {nombre: A, origenes: [EZE], destinos: [BKK], fechas: [{ida: 2030-01-01, vuelta: 2030-01-15}], buscar_hasta: 2020-01-01}
  - {nombre: B, origenes: [EZE], destinos: [BKK], fechas: [{ida: 2030-01-01, vuelta: 2030-01-15}], buscar_desde: 2020-01-01, buscar_hasta: 2999-01-01}
  - {nombre: C, origenes: [EZE], destinos: [BKK], fechas: [{ida: 2030-01-01, vuelta: 2030-01-15}]}
""", encoding="utf-8")
    cfg = cargar_config(y)
    assert [b["nombre"] for b in cfg.activas] == ["B", "C"]


def test_ejecutar_respeta_ventana(tmp_path, monkeypatch):
    """End-to-end: 'ejecutar' solo corre las búsquedas cuya ventana incluye hoy."""
    import src.main as m
    y = tmp_path / "c.yaml"
    y.write_text("""general: {moneda: USD, una_vez_por_dia: false}
busquedas:
  - {nombre: Futura, origenes: [EZE], destinos: [PEK], fechas: [{ida: 2030-01-01, vuelta: 2030-01-15}], buscar_desde: 2999-01-01}
  - {nombre: Vencida, origenes: [EZE], destinos: [PEK], fechas: [{ida: 2030-01-01, vuelta: 2030-01-15}], buscar_hasta: 2020-01-01}
  - {nombre: Vigente, origenes: [EZE], destinos: [PEK], fechas: [{ida: 2030-01-01, vuelta: 2030-01-15}], buscar_desde: 2020-01-01, buscar_hasta: 2999-01-01}
""", encoding="utf-8")
    corridas = []
    monkeypatch.setattr(m, "crear_proveedor", lambda *a, **k: object())
    monkeypatch.setattr(m, "esperar_internet", lambda *a, **k: True)
    monkeypatch.setattr(m, "correr_busqueda", lambda con, b, g, pres, prov: corridas.append(b["nombre"]) or True)
    m.ejecutar(config=str(y), forzar=True, enviar_alertas=False)
    assert corridas == ["Vigente"]


def test_programacion_hora_ventana_y_reintentos(tmp_path):
    import datetime as dt
    from src.core import database as db
    from src.core.config import cargar_config
    from src.core.programacion import pendientes, proxima_ejecucion
    y = tmp_path / "c.yaml"
    y.write_text("""general: {moneda: USD, hora: 6}
busquedas:
  - {nombre: Temprano, origenes: [EZE], destinos: [BKK], fechas: [{ida: 2030-01-01, vuelta: 2030-01-15}]}
  - {nombre: Tarde, hora: 20, origenes: [EZE], destinos: [BKK], fechas: [{ida: 2030-01-01, vuelta: 2030-01-15}]}
  - {nombre: Futura, buscar_desde: 2999-01-01, origenes: [EZE], destinos: [BKK], fechas: [{ida: 2030-01-01, vuelta: 2030-01-15}]}
  - {nombre: Pausada, activa: false, origenes: [EZE], destinos: [BKK], fechas: [{ida: 2030-01-01, vuelta: 2030-01-15}]}
""", encoding="utf-8")
    cfg = cargar_config(y)
    hoy = dt.date.today()
    a_las = lambda h: dt.datetime.combine(hoy, dt.time(h, 17))
    nombres = lambda h: [b["nombre"] for b in pendientes(cfg, a_las(h))]
    assert nombres(5) == []
    assert nombres(7) == ["Temprano"]                # ya es su hora (y recupera si se salteó la de las 6)
    assert nombres(21) == ["Temprano", "Tarde"]
    with db.conexion() as con:                        # Temprano corrió bien hoy -> no se repite
        cid = db.iniciar_corrida(con, "Temprano", "test")
        db.finalizar_corrida(con, cid, 10, 0, db.COMPLETA)
        for _ in range(2):                            # Tarde fue bloqueada 2 veces -> no insiste más hoy
            cid = db.iniciar_corrida(con, "Tarde", "test")
            db.finalizar_corrida(con, cid, 3, 3, 0)
    assert nombres(21) == []
    t = next(b for b in cfg.busquedas if b["nombre"] == "Temprano")
    assert proxima_ejecucion(t, a_las(9)).startswith(f"{hoy + dt.timedelta(days=1):%d/%m/%Y} desde las 06:00")
    assert proxima_ejecucion(next(b for b in cfg.busquedas if b["nombre"] == "Futura")).startswith("01/01/2999")
    assert proxima_ejecucion(next(b for b in cfg.busquedas if b["nombre"] == "Pausada")) is None
