@echo off
rem ===========================================================================
rem  Fabrique les programmes Windows du projet camera :
rem    dist\CameraClient.exe           client, sans fenetre (usage normal)
rem    dist\CameraClient-console.exe   client, avec console (diagnostic)
rem    dist\CameraServerGUI.exe        interface de controle (GUI), sans console
rem    releases\CameraClient-Setup.exe installateur silencieux (si Inno Setup present)
rem
rem  L'URL du serveur est INTEGREE dans le client via le fichier
rem  embedded_config.py (a la racine du projet, versionne dans GitHub).
rem  Pour changer de serveur : modifiez ce fichier, ou passez l'URL en argument :
rem      build_windows.bat https://autre-serveur.com
rem
rem  Fonctionne en local (double-clic) et dans GitHub Actions (variable CI :
rem  aucune pause, aucune question).
rem
rem  IMPORTANT : ce script vit dans packaging\windows\, PAS dans build\windows\.
rem  build\ est l'espace de travail de PyInstaller (nettoye/recree a chaque
rem  build avec --clean) : tout fichier qui y vivrait serait supprime.
rem ===========================================================================
cd /d "%~dp0..\.."

set "SERVER_URL=%~1"
if not "%SERVER_URL%"=="" goto :write_config
if exist embedded_config.py goto :config_ready
if defined CI (
  echo ERREUR : embedded_config.py est introuvable.
  exit /b 1
)
set /p "SERVER_URL=URL du serveur (ex. https://mon-serveur.com) : "
if "%SERVER_URL%"=="" (
  echo ERREUR : l'URL du serveur est obligatoire.
  pause
  exit /b 1
)

:write_config
if "%SERVER_URL:~-1%"=="/" set "SERVER_URL=%SERVER_URL:~0,-1%"
> embedded_config.py echo SERVER_URL = "%SERVER_URL%"
echo URL du serveur integree au client : %SERVER_URL%

:config_ready
echo Contenu de embedded_config.py :
type embedded_config.py

echo [1/4] Environnement de construction...
python -m venv build\venv || goto :error
call build\venv\Scripts\activate.bat || goto :error
python -m pip install --upgrade pip || goto :error
python -m pip install --upgrade pyinstaller -r requirements.txt || goto :error

echo [2/4] Construction de CameraClient.exe (sans fenetre)...
pyinstaller --noconfirm --clean --onefile --noconsole --name CameraClient ^
  --specpath build --workpath build\work-client-gui --distpath dist "client.py" || goto :error

echo [3/4] Construction de CameraClient-console.exe (diagnostic)...
pyinstaller --noconfirm --onefile --console --name CameraClient-console ^
  --specpath build --workpath build\work-client-console --distpath dist "client.py" || goto :error

echo [4/4] Construction de CameraServerGUI.exe (interface de controle)...
pyinstaller --noconfirm --clean --onefile --noconsole --name CameraServerGUI ^
  --specpath build --workpath build\work-server-gui --distpath dist "server_gui.py" || goto :error

where ISCC >nul 2>nul
if errorlevel 1 (
  echo.
  echo [Info] Inno Setup ^(ISCC^) introuvable : l'installateur CameraClient-Setup.exe
  echo        n'a pas ete cree. Installez Inno Setup ^(https://jrsoftware.org/isinfo.php^)
  echo        puis relancez ce script pour obtenir l'installateur complet.
  echo        En attendant, dist\CameraClient.exe et dist\CameraServerGUI.exe
  echo        fonctionnent deja seuls ^(pas besoin de Python^).
  if not defined CI pause
  exit /b 0
)

echo [Bonus] Construction de l'installateur silencieux du client (CameraClient-Setup.exe)...
pushd packaging\windows
ISCC installer.iss
set ISCC_RESULT=%errorlevel%
popd
if not "%ISCC_RESULT%"=="0" goto :error

echo.
echo Termine. Fichiers crees :
echo   dist\CameraClient.exe              (client seul, aucun Python requis)
echo   dist\CameraClient-console.exe      (diagnostic : --debug --once)
echo   dist\CameraServerGUI.exe           (interface de controle, aucun Python requis)
echo   releases\CameraClient-Setup.exe    (installateur : double-clic = installation invisible)
if not defined CI pause
exit /b 0

:error
echo.
echo ERREUR : la construction a echoue (voir les messages ci-dessus).
if not defined CI pause
exit /b 1
