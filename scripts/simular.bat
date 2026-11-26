@echo off
chcp 65001 >nul
rem Muestra cuantas consultas haria cada busqueda con la config actual, sin consultar nada.
set "RAIZ=%~dp0.."
if exist "%RAIZ%\venv\Scripts\python.exe" (set "PY=%RAIZ%\venv\Scripts\python.exe") else (set "PY=python")
"%PY%" "%RAIZ%\src\main.py" --simular
pause
