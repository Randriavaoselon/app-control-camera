; Installateur Windows du client caméra.
; Nécessite Inno Setup (gratuit) : https://jrsoftware.org/isinfo.php
; Compilation : ISCC packaging\windows\installer.iss
; (le script build_windows.bat le fait automatiquement s'il trouve ISCC)
;
; Le résultat, CameraClient-Setup.exe, ne nécessite PAS Python sur la
; machine de l'utilisateur : dist\CameraClient.exe (fabriqué par
; PyInstaller) embarque déjà tout l'interpréteur.
;
; IMPORTANT : ce script vit dans packaging\windows\, PAS dans build\windows\.
; build\ est l'espace de travail de PyInstaller (il est nettoyé/recréé à
; chaque build avec --clean) : tout fichier qui y vivrait serait supprimé.
;
; Installation silencieuse, en une seule commande (cmd ou PowerShell) :
;   CameraClient-Setup.exe /VERYSILENT /SUPPRESSMSGBOX /NORESTART /TASKS=autostart /SERVERURL="https://mon-serveur.com"
; Aucune fenêtre ne s'affiche : l'adresse du serveur est prise sur /SERVERURL,
; et /TASKS=autostart active le démarrage automatique (switch natif Inno Setup).

#define MyAppName "Camera Client"
#define MyAppVersion "1.0"
#define MyAppExeName "CameraClient.exe"

[Setup]
AppId={{2F2C5B3E-7A1D-4B7A-9B2E-CAMERACLIENT1}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher=Selon Randriavao
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
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

[Tasks]
Name: "autostart"; Description: "Démarrer automatiquement avec Windows"; GroupDescription: "Options :"; Flags: checkedonce

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Désinstaller {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{userstartup}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: autostart

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--server ""{code:GetServerURL}"" --save --once"; \
    Flags: runhidden waituntilterminated; StatusMsg: "Configuration du serveur..."
Filename: "{app}\{#MyAppExeName}"; Description: "Lancer {#MyAppName} maintenant"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{localappdata}\CameraClient"

[Code]
var
  ServerPage: TInputQueryWizardPage;

function GetParam(const Name: string): string;
var
  I: Integer;
  Prefix: string;
  Value: string;
begin
  Result := '';
  Prefix := '/' + Name + '=';
  for I := 1 to ParamCount do
  begin
    if CompareText(Copy(ParamStr(I), 1, Length(Prefix)), Prefix) = 0 then
    begin
      Value := Copy(ParamStr(I), Length(Prefix) + 1, MaxInt);
      if (Length(Value) >= 2) and (Value[1] = '"') and (Value[Length(Value)] = '"') then
        Value := Copy(Value, 2, Length(Value) - 2);
      Result := Value;
      Exit;
    end;
  end;
end;

procedure InitializeWizard;
begin
  if WizardSilent then
    Exit;

  ServerPage := CreateInputQueryPage(wpSelectTasks,
    'Adresse du serveur', 'À quel serveur ce client doit-il se connecter ?',
    'Indiquez l''URL complète de votre serveur, par exemple :' + #13#10 +
    'https://ton-utilisateur.pythonanywhere.com');
  ServerPage.Add('URL du serveur :', False);

  if GetParam('SERVERURL') <> '' then
    ServerPage.Values[0] := GetParam('SERVERURL')
  else
    ServerPage.Values[0] := 'http://127.0.0.1:5050';
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (ServerPage <> nil) and (CurPageID = ServerPage.ID) then
  begin
    if Trim(ServerPage.Values[0]) = '' then
    begin
      MsgBox('Merci de renseigner l''adresse du serveur.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function GetServerURL(Param: string): string;
begin
  if GetParam('SERVERURL') <> '' then
    Result := GetParam('SERVERURL')
  else if ServerPage <> nil then
    Result := Trim(ServerPage.Values[0])
  else
    Result := 'http://127.0.0.1:5050';
end;