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

# Installateur (Inno Setup 6, installé pour l'utilisateur ou pour tous).
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    $version = [regex]::Match((Get-Content version.py -Raw), '__version__ = "([^"]+)"').Groups[1].Value
    & $iscc /Q "/DAppVersion=$version" installer\debarras.iss
    if ($LASTEXITCODE -ne 0) { throw "Échec d'Inno Setup" }
    Write-Host "OK : dist\Debarras-$version-setup.exe"
} else {
    Write-Host "Inno Setup 6 absent : installateur non construit."
}
