@echo off
rem ===========================================================================
rem  Fabrique les programmes Windows du projet camera :
rem    dist\CameraClient.exe           client, sans fenetre (usage normal)
rem    dist\CameraClient-console.exe   client, avec console (diagnostic)
rem    dist\CameraServerGUI.exe        interface de controle (GUI), sans console
rem    releases\CameraClient-Setup.exe installateur 100%% silencieux (si Inno Setup present)
rem
rem  Usage :
rem      build_windows.bat https://mon-serveur.com
rem
rem  L'URL du serveur est INTEGREE dans le client (fichier embedded_config.py
rem  genere a la racine du projet). Si vous ne la donnez pas, le script reutilise
rem  celle du dernier build, ou vous la demande.
rem
rem  A lancer sur un PC Windows avec Python 3.10+ (python.org).
rem  Double-cliquez, ou lancez depuis une invite de commandes.
rem
rem  IMPORTANT : ce script vit dans packaging\windows\, PAS dans build\windows\.
rem  build\ est l'espace de travail de PyInstaller (nettoye/recree a chaque
rem  build avec --clean) : tout fichier qui y vivrait serait supprime.
rem ===========================================================================
cd /d "%~dp0..\.."

set "SERVER_URL=%~1"
if not "%SERVER_URL%"=="" goto :write_config
if exist embedded_config.py goto :config_ready
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
  pause
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
pause
exit /b 0

:error
echo.
echo ERREUR : la construction a echoue (voir les messages ci-dessus).
pause
exit /b 1
