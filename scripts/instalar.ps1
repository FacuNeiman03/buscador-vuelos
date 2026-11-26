# Instalador del Buscador de vuelos (Windows)
$ErrorActionPreference = 'Stop'
$raiz = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)   # scripts\.. = raíz del proyecto
Set-Location $raiz
$py_venv = Join-Path $raiz 'venv\Scripts\python.exe'
$pyw_venv = Join-Path $raiz 'venv\Scripts\pythonw.exe'
$main = Join-Path $raiz 'src\main.py'

function Buscar-Python {
    foreach ($c in @('py', 'python')) {
        if (Get-Command $c -ErrorAction SilentlyContinue) {
            try { $ok = & $c -c "import sys;print(sys.version_info>=(3,10))" 2>$null; if ($ok -eq 'True') { return (Get-Command $c).Source } } catch {}
        }
    }
    foreach ($v in @('313', '312', '311', '310')) {
        $p = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\python.exe"
        if (Test-Path $p) { return $p }
    }
    return $null
}

Write-Host "`n[1/4] Buscando Python..." -ForegroundColor Cyan
$py = Buscar-Python
if (-not $py) {
    Write-Host "No hay Python. Instalando Python 3.12 con winget..." -ForegroundColor Yellow
    winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
    $py = Buscar-Python
    if (-not $py) { throw "No se pudo instalar Python. Instalalo desde https://www.python.org/downloads/ y volvé a correr scripts\instalar.bat" }
}
Write-Host "Python: $py"

Write-Host "`n[2/4] Creando entorno e instalando dependencias..." -ForegroundColor Cyan
if (-not (Test-Path $py_venv)) { & $py -m venv (Join-Path $raiz 'venv') }
& $py_venv -m pip install --upgrade pip -q
& $py_venv -m pip install -r (Join-Path $raiz 'requirements.txt') -q
if ($LASTEXITCODE -ne 0) { throw "Falló la instalación de dependencias" }

Write-Host "`n[3/4] Configurando para que corra al prender la PC..." -ForegroundColor Cyan
$startup = [Environment]::GetFolderPath('Startup')
$lnk = Join-Path $startup 'Buscador de vuelos.lnk'
$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut($lnk)
$s.TargetPath = $pyw_venv
$s.Arguments = "`"$main`""
$s.WorkingDirectory = $raiz
$s.WindowStyle = 7
$s.Save()
Write-Host "Acceso directo creado en: $lnk"

Write-Host "`n[4/4] Primera búsqueda (puede tardar bastante, podés minimizar esta ventana)..." -ForegroundColor Cyan
& $py_venv $main --simular
& $py_venv $main --forzar
Write-Host "`nListo. A partir de ahora corre solo 1 vez por día al iniciar sesión." -ForegroundColor Green
Write-Host "Reporte: $raiz\reportes\index.html"
