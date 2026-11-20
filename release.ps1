# Prépare (et publie avec -Publish) une release GitHub de Débarras.
#   powershell -ExecutionPolicy Bypass -File release.ps1            -> tests + build + zip + empreinte
#   powershell -ExecutionPolicy Bypass -File release.ps1 -Publish   -> idem puis tag, push et release
# Version lue dans src-tauri\Cargo.toml, dépôt dans debarras-core\src\updater.rs ;
# notes lues dans CHANGELOG.md (section « ## <version> »).
param([switch]$Publish)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$version = [regex]::Match((Get-Content src-tauri\Cargo.toml -Raw), '(?m)^version = "([^"]+)"').Groups[1].Value
$repo = [regex]::Match((Get-Content debarras-core\src\updater.rs -Raw), 'GITHUB_REPO: &str = "([^"]+)"').Groups[1].Value
$tag = "v$version"
if (-not $version -or -not $repo) { throw "Version ou dépôt illisible" }
if (git status --porcelain) { throw "Modifications non commitées : commitez avant de publier." }
if (git tag -l $tag) { throw "Le tag $tag existe déjà : augmentez la version dans src-tauri\Cargo.toml." }

# Notes de version : section du CHANGELOG correspondant exactement à la version.
$changelog = Get-Content CHANGELOG.md -Raw -Encoding UTF8
$m = [regex]::Match($changelog, "(?ms)^## $([regex]::Escape($version))\b[^\n]*\n(.*?)(?=^## |\z)")
if (-not $m.Success) { throw "Pas de section « ## $version » dans CHANGELOG.md" }
$notes = Join-Path $env:TEMP "debarras-notes-$version.md"
[IO.File]::WriteAllText($notes, $m.Groups[1].Value.Trim(), [Text.UTF8Encoding]::new($false))

cargo test --locked
if ($LASTEXITCODE -ne 0) { throw "Tests en échec : release annulée." }
cargo clippy --locked --all-targets -- -D warnings
if ($LASTEXITCODE -ne 0) { throw "Clippy en échec : release annulée." }
& .\build.ps1

$name = "Debarras-$version-win64.zip"
$zip = Join-Path "dist" $name
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "dist\Debarras" -DestinationPath $zip    # racine de l'archive : Debarras\
$sha = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
[IO.File]::WriteAllText("$zip.sha256", "$sha  $name`n", [Text.Encoding]::ASCII)
$mb = [math]::Round((Get-Item $zip).Length / 1MB)
Write-Host "Archive : $zip ($mb Mo) — SHA-256 $sha"
$setup = Join-Path "dist" "Debarras-$version-setup.exe"
if (-not (Test-Path $setup)) { throw "Installateur absent : installez Inno Setup 6." }
$ssha = (Get-FileHash $setup -Algorithm SHA256).Hash.ToLower()
[IO.File]::WriteAllText("$setup.sha256", "$ssha  Debarras-$version-setup.exe`n", [Text.Encoding]::ASCII)
Write-Host "Installateur : $setup — SHA-256 $ssha"

if (-not $Publish) {
    Write-Host "Prêt. Relancez avec -Publish pour créer le tag $tag et la release sur $repo."
    exit 0
}
git tag -a $tag -m "Débarras $version"
git push origin HEAD $tag
gh release create $tag $zip "$zip.sha256" $setup "$setup.sha256" --repo $repo --title "Débarras $version" --notes-file $notes
if ($LASTEXITCODE -ne 0) { throw "Échec de gh release create" }
Write-Host "Release publiée : https://github.com/$repo/releases/tag/$tag"
