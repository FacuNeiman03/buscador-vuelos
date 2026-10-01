@echo off
chcp 65001 >nul
rem Levanta la interfaz web local en http://localhost:8000 (el boton "Actualizar" funciona directo)
set "RAIZ=%~dp0.."
if exist "%RAIZ%\venv\Scripts\python.exe" (set "PY=%RAIZ%\venv\Scripts\python.exe") else (set "PY=python")
"%PY%" -m pip install -q -r "%RAIZ%\requirements-web.txt"
cd /d "%RAIZ%"
start "" http://localhost:8000
"%PY%" -m uvicorn src.web.app:app --host 127.0.0.1 --port 8000
pause
