"""Control de velocidad adaptativo para las consultas a Google Flights.

Antes: una pausa FIJA de 2-5 s después de cada consulta y un descanso de 10-25 s cada 20-30
consultas. Eso hacía que ~75 % del tiempo de una corrida fuera espera, aunque Google no se quejara.

Ahora:
  * varias consultas en paralelo (cada hilo con su propio "navegador");
  * un intervalo mínimo entre el INICIO de dos consultas (con variación aleatoria);
  * el intervalo se ADAPTA: si Google devuelve bloqueo/captcha o fallan consultas, se duplica
    al instante (y la consulta se reintenta como siempre); con consultas sanas vuelve a bajar
    de a poco hasta el mínimo del preset.

Mismas consultas, mismos resultados: solo cambia cuánto se espera entre una y otra.
"""
from __future__ import annotations

import logging
import random
import threading
import time
from typing import Callable

log = logging.getLogger("vuelos")

# preset -> (consultas en paralelo, intervalo mínimo en segundos entre inicios de consulta)
PRESETS = {
    "rapida": (4, 0.4),
    "normal": (3, 0.8),
    "prudente": (1, 3.5),    # ≈ el comportamiento anterior (pausas fijas de 2-5 s)
}
INTERVALO_MAX = 20.0          # tope al que puede subir el intervalo si Google se pone estricto
SANAS_PARA_ACELERAR = 8       # consultas sin problemas antes de volver a acelerar un paso


class Ritmo:
    def __init__(self, concurrencia: int, intervalo_min: float, dormir: Callable[[float], None] = time.sleep,
                 reloj: Callable[[], float] = time.monotonic, activo: bool = True):
        self.concurrencia = max(1, int(concurrencia))
        self.intervalo_min = max(0.0, float(intervalo_min))
        self.intervalo = self.intervalo_min
        self.activo = activo           # False: proveedores por API (SerpApi) no necesitan esperar
        self._dormir, self._reloj = dormir, reloj
        self._lock = threading.Lock()
        self._proximo = 0.0
        self._sanas = 0
        self.frenadas = 0

    @classmethod
    def desde_config(cls, g: dict, activo: bool = True, dormir: Callable[[float], None] = time.sleep) -> "Ritmo":
        conc, intervalo = PRESETS.get(g.get("velocidad", "normal"), PRESETS["normal"])
        return cls(g.get("concurrencia") or conc, g.get("intervalo_min", intervalo), dormir=dormir, activo=activo)

    def esperar_turno(self) -> None:
        """Bloquea el hilo hasta que le toque iniciar una consulta."""
        if not self.activo:
            return
        with self._lock:
            ahora = self._reloj()
            turno = max(ahora, self._proximo)
            self._proximo = turno + self.intervalo * random.uniform(0.75, 1.25)
        espera = turno - ahora
        if espera > 0:
            self._dormir(espera)

    def ok(self) -> None:
        with self._lock:
            self._sanas += 1
            if self._sanas >= SANAS_PARA_ACELERAR and self.intervalo > self.intervalo_min:
                self.intervalo = max(self.intervalo_min, self.intervalo * 0.8)
                self._sanas = 0

    def problema(self) -> None:
        """Google bloqueó o falló una consulta: frenar enseguida.
        Varios errores casi simultáneos (de hilos en paralelo) cuentan como un solo evento."""
        with self._lock:
            ahora = self._reloj()
            if ahora - getattr(self, "_ultimo_problema", -1e9) < max(1.0, self.intervalo):
                self._sanas = 0
                return
            self._ultimo_problema = ahora
            antes = self.intervalo
            self.intervalo = min(INTERVALO_MAX, max(self.intervalo * 2, self.intervalo_min + 1.0))
            self._sanas = 0
            self._proximo = max(self._proximo, ahora + self.intervalo)
            self.frenadas += 1
        if self.intervalo > antes:
            log.info(f"  ⏸ Google se puso estricto: bajo la velocidad (una consulta cada ~{self.intervalo:.1f} s)")

    def segundos_por_consulta(self, latencia: float = 1.5) -> float:
        """Estimación para mostrar al usuario."""
        if not self.activo:
            return latencia / self.concurrencia
        return max(self.intervalo_min, latencia / self.concurrencia)
