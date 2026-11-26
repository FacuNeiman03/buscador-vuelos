@echo off
chcp 65001 >nul
set "RAIZ=%~dp0.."
if exist "%RAIZ%\venv\Scripts\python.exe" (set "PY=%RAIZ%\venv\Scripts\python.exe") else (set "PY=python")
"%PY%" "%RAIZ%\src\main.py" --forzar %*
pause
