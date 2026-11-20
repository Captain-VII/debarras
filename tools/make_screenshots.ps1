# Captures d'écran du README sur un disque de démonstration fictif (aucune donnée réelle).
# Fichiers « creux » : tailles réalistes, quasi aucune place sur le disque. Le dossier de démo est
# monté sur une lettre (subst) ; cache, paramètres, Temp et caches « navigateurs » sont factices.
# L'appli est pilotée par le port de débogage de WebView2 (tools\screenshots.mjs), fenêtre sur
# l'écran secondaire, sans jamais prendre le focus.
#   powershell -ExecutionPolicy Bypass -File tools\make_screenshots.ps1   -> docs\screenshots\*.png
param([switch]$Keep)   # -Keep : laisse l'appli et le disque de démo après le premier scan (mise au point)
$ErrorActionPreference = "Stop"
$repo = Split-Path $PSScriptRoot
$exe = Join-Path $repo "target\release\Debarras.exe"
if (-not (Test-Path $exe)) { throw "Compilez d'abord l'appli (build.ps1)." }

Add-Type @'
using System; using System.IO; using System.Runtime.InteropServices; using Microsoft.Win32.SafeHandles;
public static class Sparse {
  [DllImport("kernel32.dll", SetLastError = true)]
  static extern bool DeviceIoControl(SafeFileHandle h, uint code, IntPtr i, uint il, IntPtr o, uint ol, out uint r, IntPtr ov);
  public static void Make(string path, long size, DateTime mtime) {
    Directory.CreateDirectory(Path.GetDirectoryName(path));
    using (var f = new FileStream(path, FileMode.Create)) {
      uint r;
      if (!DeviceIoControl(f.SafeFileHandle, 0x900C4, IntPtr.Zero, 0, IntPtr.Zero, 0, out r, IntPtr.Zero))
        throw new IOException("fichier creux impossible : " + path);
      f.SetLength(size);
    }
    File.SetLastWriteTime(path, mtime); File.SetLastAccessTime(path, mtime);
  }

  // Lance l'appli réduite sans activation (SW_SHOWMINNOACTIVE) : jamais par-dessus un jeu.
  [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
  struct SI { public int cb; public string r, d, t; public int x, y, xs, ys, xc, yc, fill, flags; public short show, r2; public IntPtr r3, i, o, e; }
  [StructLayout(LayoutKind.Sequential)] struct PI { public IntPtr p, t; public int pid, tid; }
  [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  static extern bool CreateProcessW(string app, string cmd, IntPtr pa, IntPtr ta, bool inherit, uint flags, IntPtr env, string dir, ref SI si, out PI pi);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
  public static int Launch(string exe) {
    var si = new SI(); si.cb = Marshal.SizeOf(si); si.flags = 1; si.show = 7;
    PI pi;
    if (!CreateProcessW(exe, "\"" + exe + "\"", IntPtr.Zero, IntPtr.Zero, false, 0, IntPtr.Zero, null, ref si, out pi))
      throw new System.ComponentModel.Win32Exception();
    CloseHandle(pi.p); CloseHandle(pi.t);
    return pi.pid;
  }
}
'@

$GB = 1GB; $MB = 1MB
$old = (Get-Date).AddDays(-40)
$rand = New-Object System.Random 7
function Many($dir, $pattern, $n, $lo, $hi, $when = $old) {
    for ($i = 1; $i -le $n; $i++) { [Sparse]::Make((Join-Path $dir ($pattern -f $i)), [long]($lo + $rand.NextDouble() * ($hi - $lo)), $when) }
}

$scratch = Join-Path ([IO.Path]::GetTempPath()) ("debarras-demo-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
$demo = "$scratch\disque"; $appdata = "$scratch\appdata"; $temp = "$scratch\temp"
New-Item -ItemType Directory $demo, $appdata, $temp | Out-Null
$letter = "XYWVUT".ToCharArray() | ForEach-Object { "${_}:" } | Where-Object { -not (Test-Path "$_\") } | Select-Object -First 1
subst $letter $demo
$app = $null
try {
    # --- disque de démonstration
    [Sparse]::Make("$demo\pagefile.sys", 16 * $GB, $old)
    $common = "$demo\Jeux\SteamLibrary\steamapps\common"
    foreach ($game in @(@("Aventure Céleste", @(38, 22, 9)), @("Rallye Extrême", @(14, 6)), @("Cité Pixel", @(4, 2)))) {
        $g = Join-Path $common $game[0]
        [Sparse]::Make("$g\$($game[0].Split(' ')[0]).exe", 90 * $MB, $old)
        [Sparse]::Make("$g\bin\engine.dll", 40 * $MB, $old)
        [Sparse]::Make("$g\bin\physics.dll", 12 * $MB, $old)
        for ($i = 0; $i -lt $game[1].Count; $i++) { [Sparse]::Make("$g\data\pak{0:d2}.pak" -f $i, $game[1][$i] * $GB, $old) }
        Many "$g\shaders" "shader_{0:d3}.bin" 40 (1 * $MB) (20 * $MB)
    }
    $ed = "$demo\Programmes\Studio Photo"
    [Sparse]::Make("$ed\studio.exe", 120 * $MB, $old)
    Many $ed "module{0}.dll" 12 (5 * $MB) (60 * $MB)
    Many "$ed\ressources" "pack{0}.res" 8 (100 * $MB) (400 * $MB)
    $u = "$demo\Utilisateur"
    Many "$u\Documents\Factures" "facture-2025-{0:d2}.pdf" 24 200000 (2 * $MB)
    Many "$u\Documents\Projets" "rapport-{0}.docx" 15 (1 * $MB) (30 * $MB)
    Many "$u\Documents" "présentation-{0}.pptx" 6 (20 * $MB) (180 * $MB)
    foreach ($y in @(@("2023", 400), @("2024", 650), @("2025", 500))) { Many "$u\Photos\$($y[0])" "IMG_{0:d4}.jpg" $y[1] (3 * $MB) (9 * $MB) }
    Many "$u\Photos\2024\Mariage RAW" "DSC_{0:d4}.nef" 300 (25 * $MB) (30 * $MB)
    Many "$u\Vidéos\Vacances" "clip-{0:d2}.mp4" 30 (300 * $MB) (1500 * $MB)
    Many "$u\Vidéos\Films" "film-{0:d2}.mkv" 8 (3 * $GB) (7 * $GB)
    Many "$u\Musique" "piste-{0:d3}.flac" 300 (20 * $MB) (45 * $MB)
    Many "$u\Téléchargements" "installeur-{0}.exe" 6 (50 * $MB) (900 * $MB)
    Many "$u\Téléchargements" "archive-{0}.zip" 5 (100 * $MB) (2 * $GB)
    $p = "$demo\Projets"
    Many "$p\site-web\node_modules\paquets" "lib{0:d3}.js" 300 (1 * $MB) (8 * $MB)
    Many "$p\site-web\.git\objects" "pack-{0}.pack" 5 (50 * $MB) (120 * $MB)
    Many "$p\site-web\src" "page{0}.tsx" 40 5000 80000
    Many "$p\outil-python\.venv\Lib" "dep{0}.pyd" 30 (2 * $MB) (30 * $MB)
    Many "$p\outil-python\outil\__pycache__" "mod{0}.pyc" 30 20000 400000
    # Caches et temporaires (onglet Nettoyage)
    Many "$temp\installation-1a2b" "fichier{0}.tmp" 20 (10 * $MB) (60 * $MB)
    Many "$temp\maj-pilote" "paquet{0}.cab" 6 (80 * $MB) (200 * $MB)
    Many $temp "log{0}.tmp" 10 100000 (3 * $MB)
    foreach ($prof in "Default", "Profile 1") {
        Many "$appdata\Google\Chrome\User Data\$prof\Cache\Cache_Data" "f_{0:x6}" 120 (1 * $MB) (6 * $MB)
        Many "$appdata\Google\Chrome\User Data\$prof\Code Cache\js" "{0:x16}_0" 60 200000 (2 * $MB)
    }
    Many "$appdata\Microsoft\Edge\User Data\Default\Cache\Cache_Data" "f_{0:x6}" 80 (1 * $MB) (4 * $MB)
    Many "$appdata\CrashDumps" "Aventure.exe.{0}.dmp" 4 (150 * $MB) (400 * $MB)
    Many "$appdata\NVIDIA\DXCache" "{0:x16}.nvph" 60 (20 * $MB) (120 * $MB)

    # --- appli : réduite sans activation, puis sur l'écran secondaire
    $env:LOCALAPPDATA = $appdata; $env:TEMP = $temp; $env:TMP = $temp
    $env:DEBARRAS_NO_UPDATE_CHECK = "1"
    $env:WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS = "--remote-debugging-port=9334"
    $app = Get-Process -Id ([Sparse]::Launch($exe))
    & "$env:USERPROFILE\.claude\tools\second-screen.ps1" -Id $app.Id | Out-Null
    $shots = Join-Path $repo "docs\screenshots"
    node "$PSScriptRoot\screenshots.mjs" scan $letter
    if ($Keep) { Write-Host "Appli $($app.Id) ouverte, disque $letter, données $scratch"; return }
    if ($LASTEXITCODE -ne 0) { throw "Premier scan en échec (appli fermée : $($app.HasExited) $($app.ExitCode))" }
    # Ce qui change entre deux scans (coloriage Évolution)
    $now = Get-Date
    [Sparse]::Make("$common\Aventure Céleste\data\pak03_extension.pak", 24 * $GB, $now)
    Many "$u\Vidéos\Vacances 2025" "clip-{0:d2}.mp4" 25 (400 * $MB) (1800 * $MB) $now
    Many "$u\Téléchargements" "nouveau-{0}.zip" 3 (1 * $GB) (3 * $GB) $now
    node "$PSScriptRoot\screenshots.mjs" shots $letter $shots
    if ($LASTEXITCODE -ne 0) { throw "Captures en échec (appli fermée : $($app.HasExited) $($app.ExitCode))" }
} finally {
  if (-not $Keep) {
    if (Test-Path "$appdata\Debarras\crash.log") { Get-Content "$appdata\Debarras\crash.log" }
    if ($app -and -not $app.HasExited) { $app.CloseMainWindow() | Out-Null; $app.WaitForExit(10000) | Out-Null }
    subst $letter /D
    Remove-Item $scratch -Recurse -Force -ErrorAction SilentlyContinue  # uniquement des fichiers générés ici
  }
}
Get-ChildItem (Join-Path $repo "docs\screenshots") -Filter *.png | ForEach-Object { $_.Name }
