# Construit dist\Debarras\Debarras.exe
# Usage : powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
}
$py = ".venv\Scripts\python.exe"
& $py -m pip install --quiet -r requirements.txt
if (-not (Test-Path "assets\icon.ico")) { & $py assets\make_icon.py }

& $py -m PyInstaller --noconfirm --clean file_analyzer.spec
if ($LASTEXITCODE -ne 0) { throw "Échec de PyInstaller" }
& $py tools\collect_licenses.py dist\Debarras      # LICENSE + licenses\ (MIT, LGPL, tiers)
if ($LASTEXITCODE -ne 0) { throw "Échec de la copie des licences" }

$exe = "dist\Debarras\Debarras.exe"
$size = (Get-ChildItem "dist\Debarras" -Recurse | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("OK : {0} ({1:N0} Mo au total)" -f $exe, $size)
