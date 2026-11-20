# Construit dist\Debarras\Debarras.exe (Rust + Tauri) et l'installateur Inno Setup.
# Usage : powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path node_modules)) {
    npm ci
    if ($LASTEXITCODE -ne 0) { throw "Échec de npm ci" }
}
npx tauri build --no-bundle          # interface (tsc + vite) puis target\release\Debarras.exe
if ($LASTEXITCODE -ne 0) { throw "Échec de la compilation" }

$out = "dist\Debarras"
if (Test-Path $out) { Remove-Item $out -Recurse -Force }
New-Item -ItemType Directory -Force "$out\licenses" | Out-Null
Copy-Item "target\release\Debarras.exe", "LICENSE" $out
node tools\licenses.mjs "$out\licenses"     # licences des crates et paquets npm embarqués
if ($LASTEXITCODE -ne 0) { throw "Échec du recueil des licences" }

$size = (Get-ChildItem $out -Recurse | Measure-Object Length -Sum).Sum / 1MB
Write-Host ("OK : {0}\Debarras.exe ({1:N1} Mo au total)" -f $out, $size)

# Installateur (Inno Setup 6, installé pour l'utilisateur ou pour tous).
$iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
          "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    $version = [regex]::Match((Get-Content src-tauri\Cargo.toml -Raw), '(?m)^version = "([^"]+)"').Groups[1].Value
    & $iscc /Q "/DAppVersion=$version" installer\debarras.iss
    if ($LASTEXITCODE -ne 0) { throw "Échec d'Inno Setup" }
    Write-Host "OK : dist\Debarras-$version-setup.exe"
} else {
    Write-Host "Inno Setup 6 absent : installateur non construit."
}
