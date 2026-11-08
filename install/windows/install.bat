@echo off
setlocal enabledelayedexpansion

REM =============================================================================
REM  Client camera - installation Windows
REM
REM  Usage :
REM      install.bat --server https://mon-serveur.com --autostart
REM      install.bat --uninstall
REM
REM  Options :
REM      --server URL     adresse du serveur (sauvegardee, plus besoin de la retaper)
REM      --autostart      demarre le client automatiquement a l'ouverture de session
REM      --uninstall      desinstalle tout (programme, tache planifiee, configuration)
REM
REM  Localisation (commande OPEN/CLOSE envoyee depuis le server-gui) :
REM  Chrome ou Edge sont utilises en mode fenetre dediee s'ils sont installes
REM  (cas normal sur Windows, Edge est preinstalle) : la fermeture a distance
REM  depuis le server-gui fonctionne alors directement, sans rien configurer.
REM =============================================================================

set "SCRIPT_DIR=%~dp0"
set "PROJECT_DIR=%SCRIPT_DIR%..\.."
set "APP_DIR=%LOCALAPPDATA%\CameraClient"
set "TASK_NAME=CameraClient"
set "LAUNCHER=%APP_DIR%\camera-client.bat"

set "SERVER="
set "AUTOSTART=0"
set "UNINSTALL=0"

:parse_args
if "%~1"=="" goto after_parse
if /I "%~1"=="--server" (
    set "SERVER=%~2"
    shift
    shift
    goto parse_args
)
if /I "%~1"=="--autostart" (
    set "AUTOSTART=1"
    shift
    goto parse_args
)
if /I "%~1"=="--uninstall" (
    set "UNINSTALL=1"
    shift
    goto parse_args
)
echo Option inconnue : %~1
exit /b 1

:after_parse

if "%UNINSTALL%"=="1" (
    echo == Desinstallation du client camera ==
    schtasks /delete /tn "%TASK_NAME%" /f >nul 2>&1
    rmdir /s /q "%APP_DIR%" >nul 2>&1
    echo Desinstalle : tache planifiee, programme, configuration supprimes.
    exit /b 0
)

echo == Installation du client camera ==

if not exist "%APP_DIR%" mkdir "%APP_DIR%"
copy /Y "%PROJECT_DIR%\client.py" "%APP_DIR%\" >nul
copy /Y "%PROJECT_DIR%\requirements.txt" "%APP_DIR%\" >nul

echo -- Creation de l'environnement virtuel --
python -m venv "%APP_DIR%\venv"
if errorlevel 1 (
    echo ERREUR : Python introuvable. Installez Python puis relancez ce script.
    exit /b 1
)

"%APP_DIR%\venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
"%APP_DIR%\venv\Scripts\python.exe" -m pip install -r "%APP_DIR%\requirements.txt" --quiet

REM -- Commande globale (facultative, pratique pour --once/--debug depuis un terminal)
(
  echo @echo off
  echo "%APP_DIR%\venv\Scripts\python.exe" "%APP_DIR%\client.py" %%*
) > "%LAUNCHER%"
echo Commande installee : %LAUNCHER%

REM -- Sauvegarde du serveur
if not "%SERVER%"=="" (
    echo -- Sauvegarde du serveur ^(%SERVER%^) --
    call "%LAUNCHER%" --server "%SERVER%" --save --once
)

REM -- Demarrage automatique
if "%AUTOSTART%"=="1" (
    echo -- Creation de la tache planifiee --
    schtasks /create /tn "%TASK_NAME%" ^
        /tr "\"%APP_DIR%\venv\Scripts\pythonw.exe\" \"%APP_DIR%\client.py\"" ^
        /sc onlogon /rl limited /f
    echo Tache planifiee creee : %TASK_NAME%
)

echo.
echo Termine. Fichiers dans : %APP_DIR%
echo Logs : %APP_DIR%\client.log
echo.
echo Commandes utiles :
echo   "%LAUNCHER%" --server https://... --save   ^&  changer de serveur
echo   "%LAUNCHER%" --once --debug                ^&  tester
echo   install.bat --uninstall                     ^&  tout desinstaller

endlocal