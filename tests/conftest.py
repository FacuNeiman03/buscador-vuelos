"""Aísla datos, reportes y logs de los tests en una carpeta temporal (nunca toca data/historial.db)."""
import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="vuelos-test-"))
os.environ["VUELOS_DATA"] = str(_TMP / "data")
os.environ["VUELOS_DB"] = str(_TMP / "data" / "historial.db")
os.environ["VUELOS_REPORTES"] = str(_TMP / "reportes")
os.environ["VUELOS_LOGS"] = str(_TMP / "logs")
os.environ["VUELOS_CONFIG"] = str(_TMP / "config.yaml")
for k in ("RESEND_API_KEY", "SMTP_HOST", "SMTP_USER", "SERPAPI_API_KEY", "ALERTA_EMAILS", "WEB_ADMIN_TOKEN"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
TMP = _TMP
