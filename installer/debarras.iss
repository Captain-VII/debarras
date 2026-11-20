; Installateur Windows de Débarras (Inno Setup 6).
; Construit par build.ps1 :  ISCC /DAppVersion=x.y.z installer\debarras.iss
; Installation par utilisateur (sans droits administrateur) dans %LOCALAPPDATA%\Programs\Debarras :
; la mise à jour automatique peut ainsi remplacer le dossier sans élévation.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
; Identifiant fixe : ne jamais le changer (clé de désinstallation, mises à jour par l'installateur).
#define AppId "{{2A8D66B7-37C3-42D3-8B8A-A6B86A51D234}"

[Setup]
AppId={#AppId}
AppName=Débarras
AppVersion={#AppVersion}
AppVerName=Débarras {#AppVersion}
AppPublisher=Captain VII
AppPublisherURL=https://github.com/Captain-VII/debarras
AppSupportURL=https://github.com/Captain-VII/debarras/issues
AppUpdatesURL=https://github.com/Captain-VII/debarras/releases
AppCopyright=Copyright (c) 2026 Captain VII — licence MIT
VersionInfoVersion={#AppVersion}
VersionInfoDescription=Installation de Débarras
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\Debarras
DisableDirPage=auto
DisableProgramGroupPage=yes
DisableReadyPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupIconFile=..\assets\icon.ico
UninstallDisplayIcon={app}\Debarras.exe
UninstallDisplayName=Débarras
WizardStyle=modern
WizardSmallImageFile=..\assets\icon.png
Compression=lzma2/max
SolidCompression=yes
CloseApplications=yes
RestartApplications=no
OutputDir=..\dist
OutputBaseFilename=Debarras-{#AppVersion}-setup

[Languages]
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "Créer un raccourci sur le Bureau"; GroupDescription: "Raccourcis :"; Flags: unchecked

[InstallDelete]
; Version précédente remplacée en entier (pas de fichiers orphelins d'une ancienne version) :
; _internal est le moteur Python des versions 1.x.
Type: filesandordirs; Name: "{app}\_internal"
Type: filesandordirs; Name: "{app}\licenses"

[Files]
Source: "..\dist\Debarras\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Débarras"; Filename: "{app}\Debarras.exe"; Comment: "Voir ce qui encombre le disque, et s'en débarrasser sans risque"
Name: "{autodesktop}\Débarras"; Filename: "{app}\Debarras.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Debarras.exe"; Description: "Lancer Débarras"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Ancienne version gardée en secours par la mise à jour automatique.
Type: filesandordirs; Name: "{app}.old"
; Dossiers vidés par la désinstallation (parfois retenus un instant par l'indexation).
Type: dirifempty; Name: "{app}\licenses"
Type: dirifempty; Name: "{app}"

[Code]
const
  WebView2Key = 'Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';

// L'interface de Débarras 2 s'affiche avec WebView2 (Microsoft Edge), présent sur Windows 11 et
// sur presque tous les Windows 10.
function WebView2Installed: Boolean;
var
  V: String;
begin
  Result := (RegQueryStringValue(HKLM, 'SOFTWARE\WOW6432Node\' + WebView2Key, 'pv', V) and (V <> '') and (V <> '0.0.0.0'))
    or (RegQueryStringValue(HKCU, 'Software\' + WebView2Key, 'pv', V) and (V <> '') and (V <> '0.0.0.0'));
end;

function InitializeSetup: Boolean;
var
  Code: Integer;
begin
  Result := True;
  if not WebView2Installed and not WizardSilent then
    if MsgBox('Débarras a besoin de Microsoft Edge WebView2, absent de ce PC.' + #13#10 +
              'Ouvrir la page de téléchargement de Microsoft ? Installez-le, puis lancez Débarras.',
              mbConfirmation, MB_YESNO) = IDYES then
      ShellExec('open', 'https://developer.microsoft.com/microsoft-edge/webview2/', '', '', SW_SHOWNORMAL, ewNoWait, Code);
end;

// Désinstallation : propose (sans l'imposer) d'effacer aussi cache, paramètres et journal.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := ExpandConstant('{localappdata}\Debarras');
    if DirExists(DataDir) and not UninstallSilent then
      if MsgBox('Supprimer aussi les données de Débarras (cache des scans, paramètres, journal des actions) ?' + #13#10 +
                'Vos fichiers ne sont pas concernés.', mbConfirmation, MB_YESNO or MB_DEFBUTTON2) = IDYES then
        DelTree(DataDir, True, True, True);
  end;
end;
