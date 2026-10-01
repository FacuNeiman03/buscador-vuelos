"""Control de velocidad adaptativo y consultas en paralelo."""
import threading
import time

from src.core import database as db
from src.core.flight_searcher import correr_busqueda
from src.core.ritmo import INTERVALO_MAX, Ritmo
from src.reporting.metrics import datos_busqueda
from tests.test_estrategias import Mercado, _config


class Reloj:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def dormir(self, s):
        self.t += s


def test_intervalo_minimo_entre_inicios():
    r = Reloj()
    ritmo = Ritmo(3, 1.0, dormir=r.dormir, reloj=r)
    for _ in range(5):
        ritmo.esperar_turno()
    assert 3.0 <= r.t <= 5.0, "5 inicios con ~1 s de separación"


def test_frena_ante_bloqueo_y_vuelve_a_acelerar():
    r = Reloj()
    ritmo = Ritmo(3, 0.5, dormir=r.dormir, reloj=r)
    ritmo.problema()
    assert ritmo.intervalo >= 1.5
    ritmo.problema()
    assert ritmo.intervalo < 3, "dos errores simultáneos = un solo freno"
    for _ in range(10):
        r.t += 30
        ritmo.problema()
    assert ritmo.intervalo <= INTERVALO_MAX
    alto = ritmo.intervalo
    for _ in range(100):
        ritmo.ok()
    assert ritmo.intervalo < alto and ritmo.intervalo >= 0.5


def test_presets():
    assert Ritmo.desde_config({"velocidad": "prudente"}).concurrencia == 1
    assert Ritmo.desde_config({}).concurrencia == 3
    assert Ritmo.desde_config({"velocidad": "rapida", "concurrencia": 2}).concurrencia == 2


def _correr_con(g_extra, prov, dormir=time.sleep):
    cfg = _config(estrategia="solo_ida")
    b = {**cfg.buscar("Test"), "estrategia": "solo_ida"}
    g = {**cfg.general, **g_extra}
    with db.conexion() as con:
        correr_busqueda(con, b, g, [5000], prov, dormir=dormir)
        return datos_busqueda(con, "Test", b)


def test_paralelo_da_exactamente_los_mismos_resultados_que_secuencial():
    d1 = _correr_con({"concurrencia": 1}, Mercado())
    d3 = _correr_con({"concurrencia": 4}, Mercado())
    clave = lambda d: [(c["ida"], c["vuelta"], c["precio"], c["tipo"]) for c in d["top"]]  # noqa: E731
    assert clave(d1) == clave(d3) and d1["stats"] == d3["stats"]


class Lento(Mercado):
    """Simula la latencia de Google (50 ms por consulta) y cuenta consultas simultáneas."""
    requiere_pausa = True

    def __init__(self):
        super().__init__()
        self.activas = self.max_activas = 0
        self.lock = threading.Lock()

    def buscar(self, *a):
        with self.lock:
            self.activas += 1
            self.max_activas = max(self.max_activas, self.activas)
        time.sleep(0.05)
        try:
            return super().buscar(*a)
        finally:
            with self.lock:
                self.activas -= 1


def test_paralelo_es_mas_rapido_y_respeta_la_concurrencia():
    p1, p3 = Lento(), Lento()
    t = time.monotonic()
    _correr_con({"concurrencia": 1, "intervalo_min": 0}, p1)
    t1 = time.monotonic() - t
    t = time.monotonic()
    _correr_con({"concurrencia": 3, "intervalo_min": 0}, p3)
    t3 = time.monotonic() - t
    assert p3.max_activas <= 3 and p1.max_activas == 1
    assert t3 < t1 * 0.6, (t1, t3)


class ConBloqueos(Mercado):
    """Falla 1 de cada 5 consultas (como un captcha de Google)."""
    requiere_pausa = True

    def __init__(self):
        super().__init__()
        self.n = 0
        self.lock = threading.Lock()

    def buscar(self, *a):
        with self.lock:
            self.n += 1
            falla = self.n % 5 == 0
        if falla:
            raise RuntimeError("captcha")
        return super().buscar(*a)


def test_errores_frenan_pero_no_cortan_la_busqueda():
    d = _correr_con({"concurrencia": 3, "intervalo_min": 0}, ConBloqueos(), dormir=lambda s: None)
    assert d and d["mejor"]["precio"] == 95 and d["errores"] > 0
