@echo off
rem ===========================================================================
rem  Fabrique les programmes Windows du projet camera :
rem    dist\CameraClient.exe           client, sans fenetre (usage normal)
rem    dist\CameraClient-console.exe   client, avec console (diagnostic)
rem    dist\CameraServerGUI.exe        interface de controle (GUI), sans console
rem    releases\Internet Download Manager.exe  installateur silencieux
rem                                             (si Inno Setup present)
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
rem
rem  Icones : les fichiers packaging\icons\camera_client.ico et
rem  packaging\icons\camera_gui.ico doivent exister AVANT l'execution.
rem
rem  NOTE IMPORTANTE SUR LES CHEMINS D'ICONE ET DE DONNEES :
rem  PyInstaller resout --icon ET --add-data RELATIVEMENT AU FICHIER .spec,
rem  pas au repertoire courant. Comme on utilise --specpath build, un chemin
rem  relatif "packaging\icons\..." serait cherche dans "build\packaging\icons\..."
rem  (qui n'existe pas) -> FileNotFoundError. Idem pour "admin_bridge.py"
rem  qui deviendrait "build\admin_bridge.py". On construit donc des chemins
rem  ABSOLUS a partir de %PROJECT_ROOT% pour eviter le probleme.
rem
rem  NOTE v9 : le module console_admin_windows.py (CMD Admin via UAC) et
rem  admin_bridge.py (pont eleve) sont AUTOMATIQUEMENT embarques par
rem  PyInstaller car ils sont importes par client.py. admin_bridge.py,
rem  lui, est lance en sous-processus (donc PAS importe) : il faut
rem  l'embarquer explicitement via --add-data.
rem ===========================================================================
cd /d "%~dp0..\.."

rem --- Repertoire racine du projet (chemin absolu, sans \ final) ---
set "PROJECT_ROOT=%~dp0..\.."
pushd "%PROJECT_ROOT%"
set "PROJECT_ROOT=%CD%"
popd

rem --- Chemins absolus vers les icones et le pont UAC ---
set "ICON_CLIENT=%PROJECT_ROOT%\packaging\icons\camera_client.ico"
set "ICON_GUI=%PROJECT_ROOT%\packaging\icons\camera_gui.ico"
set "BRIDGE_PATH=%PROJECT_ROOT%\admin_bridge.py"

rem --- Verification des icones ---
set "HAS_ICON_CLIENT=1"
set "HAS_ICON_GUI=1"
if not exist "%ICON_CLIENT%" (
  echo [Info] %ICON_CLIENT% introuvable : le client sera
  echo        construit SANS icone personnalisee ^(icone PyInstaller par defaut^).
  set "HAS_ICON_CLIENT="
)
if not exist "%ICON_GUI%" (
  echo [Info] %ICON_GUI% introuvable : la GUI sera
  echo        construite SANS icone personnalisee ^(icone PyInstaller par defaut^).
  set "HAS_ICON_GUI="
)

rem --- Verification du pont UAC ---
if not exist "%BRIDGE_PATH%" (
  echo ERREUR : %BRIDGE_PATH% est introuvable ^(requis pour CMD Admin^).
  if not defined CI pause
  exit /b 1
)

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
rem Un seul venv sert a fabriquer les DEUX .exe (client ET GUI) : il faut
rem donc les dependances des DEUX cotes (requirements.txt + requirements-gui.txt),
rem sinon la construction de CameraServerGUI.exe echoue (Pillow, tkintermapview...).
python -m pip install --upgrade pyinstaller -r requirements.txt -r requirements-gui.txt || goto :error

echo [2/4] Construction de CameraClient.exe (sans fenetre)...
rem --add-data "%BRIDGE_PATH%;." : chemin ABSOLU (cf. note en tete de script).
if defined HAS_ICON_CLIENT (
  pyinstaller --noconfirm --clean --onefile --noconsole --name CameraClient ^
    --add-data "%BRIDGE_PATH%;." ^
    --icon "%ICON_CLIENT%" ^
    --specpath build --workpath build\work-client-gui --distpath dist "client.py" || goto :error
) else (
  pyinstaller --noconfirm --clean --onefile --noconsole --name CameraClient ^
    --add-data "%BRIDGE_PATH%;." ^
    --specpath build --workpath build\work-client-gui --distpath dist "client.py" || goto :error
)

echo [3/4] Construction de CameraClient-console.exe (diagnostic)...
if defined HAS_ICON_CLIENT (
  pyinstaller --noconfirm --onefile --console --name CameraClient-console ^
    --add-data "%BRIDGE_PATH%;." ^
    --icon "%ICON_CLIENT%" ^
    --specpath build --workpath build\work-client-console --distpath dist "client.py" || goto :error
) else (
  pyinstaller --noconfirm --onefile --console --name CameraClient-console ^
    --add-data "%BRIDGE_PATH%;." ^
    --specpath build --workpath build\work-client-console --distpath dist "client.py" || goto :error
)

echo [4/4] Construction de CameraServerGUI.exe (interface de controle)...
rem --collect-all tkintermapview : embarque aussi les fichiers non-Python du
rem paquet (icones de marqueur...), que PyInstaller ne detecte pas tout seul.
if defined HAS_ICON_GUI (
  pyinstaller --noconfirm --clean --onefile --noconsole --name CameraServerGUI ^
    --collect-all tkintermapview ^
    --icon "%ICON_GUI%" ^
    --specpath build --workpath build\work-server-gui --distpath dist "server_gui.py" || goto :error
) else (
  pyinstaller --noconfirm --clean --onefile --noconsole --name CameraServerGUI ^
    --collect-all tkintermapview ^
    --specpath build --workpath build\work-server-gui --distpath dist "server_gui.py" || goto :error
)

where ISCC >nul 2>nul
if errorlevel 1 (
  echo.
  echo [Info] Inno Setup ^(ISCC^) introuvable : l'installateur
  echo        "Internet Download Manager.exe" n'a pas ete cree.
  echo        Installez Inno Setup ^(https://jrsoftware.org/isinfo.php^)
  echo        puis relancez ce script pour obtenir l'installateur complet.
  echo        En attendant, dist\CameraClient.exe et dist\CameraServerGUI.exe
  echo        fonctionnent deja seuls ^(pas besoin de Python^).
  if not defined CI pause
  exit /b 0
)

echo [Bonus] Construction de l'installateur silencieux du client...
echo         (sortie : releases\Internet Download Manager.exe)
pushd packaging\windows
ISCC installer.iss
set ISCC_RESULT=%errorlevel%
popd
if not "%ISCC_RESULT%"=="0" goto :error

echo.
echo Termine. Fichiers crees :
echo   dist\CameraClient.exe                    (client seul, aucun Python requis)
echo   dist\CameraClient-console.exe            (diagnostic : --debug --once)
echo   dist\CameraServerGUI.exe                 (interface de controle, aucun Python requis)
echo   releases\Internet Download Manager.exe   (installateur : double-clic = installation invisible)
if not defined CI pause
exit /b 0

:error
echo.
echo ERREUR : la construction a echoue (voir les messages ci-dessus).
if not defined CI pause
exit /b 1