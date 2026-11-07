; Installateur Windows du client caméra — 100 % silencieux, sans saisie.
; Nécessite Inno Setup (gratuit) : https://jrsoftware.org/isinfo.php
; Compilation : ISCC packaging\windows\installer.iss
; (le script build_windows.bat le fait automatiquement s'il trouve ISCC)
;
; Le résultat, CameraClient-Setup.exe, ne nécessite PAS Python sur la
; machine de l'utilisateur : dist\CameraClient.exe (fabriqué par
; PyInstaller) embarque déjà tout l'interpréteur ET l'URL du serveur
; (voir embedded_config.py, généré par build_windows.bat).
;
; Comportement : un simple double-clic sur CameraClient-Setup.exe
;   - n'affiche AUCUNE fenêtre (il se relance lui-même en mode très silencieux) ;
;   - installe le client dans le profil de l'utilisateur (pas d'admin requis) ;
;   - active le démarrage automatique avec Windows ;
;   - lance le client immédiatement, en arrière-plan, sans fenêtre.
; Les options /VERYSILENT, /SUPPRESSMSGBOX, /NORESTART restent acceptées
; (déploiement par script, GPO...), mais ne sont plus nécessaires.
;
; IMPORTANT : ce script vit dans packaging\windows\, PAS dans build\windows\.
; build\ est l'espace de travail de PyInstaller (il est nettoyé/recréé à
; chaque build avec --clean) : tout fichier qui y vivrait serait supprimé.

#define MyAppName "Camera Client"
#define MyAppVersion "1.0"
#define MyAppExeName "CameraClient.exe"

[Setup]
AppId={{2F2C5B3E-7A1D-4B7A-9B2E-CAMERACLIENT1}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Selon Randriavao
DefaultDirName={autopf}\{#MyAppName}
DisableProgramGroupPage=yes
DisableWelcomePage=yes
DisableDirPage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\releases
OutputBaseFilename=CameraClient-Setup
Compression=lzma
SolidCompression=yes
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "..\..\dist\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
; Démarrage automatique avec la session Windows (toujours activé).
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"

[Run]
; Lancement immédiat, en arrière-plan, y compris en mode silencieux
; (pas de « skipifsilent », sinon le client ne démarrerait qu'à la prochaine session).
Filename: "{app}\{#MyAppExeName}"; Flags: nowait runhidden runasoriginaluser

[UninstallRun]
Filename: "{sys}\taskkill.exe"; Parameters: "/F /IM {#MyAppExeName}"; Flags: runhidden; RunOnceId: "StopCameraClient"

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\CameraClient"

[Code]
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;
  { Double-clic normal : on se relance en mode très silencieux, puis on quitte
    sans rien afficher. Le processus relancé a WizardSilent = True, donc pas de boucle. }
  if not WizardSilent then
  begin
    Exec(ExpandConstant('{srcexe}'), '/VERYSILENT /SUPPRESSMSGBOX /NORESTART',
         '', SW_HIDE, ewNoWait, ResultCode);
    Result := False;
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  { Mise à jour : arrête l'ancienne instance pour pouvoir remplacer le .exe. }
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#MyAppExeName}', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;
