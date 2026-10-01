@echo off
rem Copia el workflow de GitHub Actions a su ubicacion (.github\workflows) antes de subir el repo.
set "RAIZ=%~dp0.."
if not exist "%RAIZ%\.github\workflows" mkdir "%RAIZ%\.github\workflows"
copy /Y "%RAIZ%\cloud\busqueda_diaria.yml" "%RAIZ%\.github\workflows\busqueda_diaria.yml" && echo Workflow listo en .github\workflows\busqueda_diaria.yml
pause
