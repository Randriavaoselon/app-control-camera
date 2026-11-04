# app-control-camera

Système de contrôle de caméra à distance, composé de trois programmes : un serveur central (`server.py`), un client caméra installé sur chaque appareil à surveiller (`client.py`), et une interface de contrôle (`server_gui.py`) qui permet de choisir un appareil et de lui envoyer des commandes. Un seul serveur peut piloter plusieurs clients en même temps, chacun identifié par un `device_id` unique généré automatiquement à la première exécution.

Le serveur est le seul composant qui doit être accessible publiquement (déployé sur Render, PythonAnywhere, ou toute autre plateforme). Le client et la GUI s'y connectent en HTTP, même depuis des réseaux Wi-Fi différents, et ne communiquent jamais directement entre eux.

## Lancer le serveur

En local, pour tester :

```bash
python -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python server.py
```

Le serveur démarre sur `http://0.0.0.0:5050`. Une fois déployé publiquement (par exemple sur Render), récupère son URL stable (ex. `https://mon-serveur.onrender.com`) : c'est elle qui sera utilisée partout ci-dessous, à la place de `https://mon-serveur.com`.

## Installer le client (l'appareil avec la caméra)

### Windows, en une seule commande, sans aucune fenêtre (PowerShell)

```powershell
Invoke-WebRequest -Uri "https://mon-serveur.com/download/windows" -OutFile "$env:TEMP\CameraClient-Setup.exe"; Start-Process "$env:TEMP\CameraClient-Setup.exe" -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOX','/NORESTART','/TASKS=autostart','/SERVERURL=https://mon-serveur.com' -Wait
```

### Windows, équivalent pour l'Invite de commandes (cmd.exe)

```bat
curl -L -o "%TEMP%\CameraClient-Setup.exe" "https://mon-serveur.com/download/windows" && "%TEMP%\CameraClient-Setup.exe" /VERYSILENT /SUPPRESSMSGBOX /NORESTART /TASKS=autostart /SERVERURL=https://mon-serveur.com
```

Les deux commandes installent le client dans `%LOCALAPPDATA%\CameraClient`, enregistrent l'URL du serveur, et créent une tâche planifiée qui démarre le client à chaque ouverture de session Windows. Aucune fenêtre ne s'affiche à aucun moment.

Désinstallation (depuis `cmd` ou PowerShell) :

```bat
"%LOCALAPPDATA%\CameraClient\unins000.exe" /VERYSILENT
```

### Linux, en une seule commande (curl)

```bash
curl -fsSL https://mon-serveur.com/client/download -o /tmp/install.sh \
  && bash /tmp/install.sh --server https://mon-serveur.com --autostart
```

Installe la commande `camera-client`, utilisable ensuite depuis n'importe quel dossier du terminal, sauvegarde l'URL du serveur, et active un service systemd utilisateur pour le démarrage automatique.

Commandes utiles une fois installé :

```bash
camera-client --server https://autre-serveur.com --save   # changer de serveur
camera-client --once --debug                               # tester une connexion, logs détaillés
camera-client --uninstall                                  # tout désinstaller (ou : /tmp/install.sh --uninstall)
```

## Installer la GUI (le poste de supervision)

### Windows

Récupérer `CameraServerGUI.exe` depuis `releases/` (ou `https://mon-serveur.com/download/windows/gui`) et le lancer directement : c'est un exécutable autonome, aucune installation n'est nécessaire.

### Linux, en une seule commande (curl)

```bash
curl -fsSL https://mon-serveur.com/gui/install -o /tmp/install_gui.sh \
  && bash /tmp/install_gui.sh --server https://mon-serveur.com
```

Installe la commande `camera-gui`, utilisable ensuite depuis n'importe quel dossier. Nécessite un affichage graphique (X11/Wayland) et le paquet système `python3-tk` (`sudo apt install python3-tk` sur Debian/Ubuntu, non installable par pip).

Lancement et désinstallation :

```bash
camera-gui                                             # lancer l'interface
camera-gui --server https://autre-serveur.com --save   # changer de serveur
/tmp/install_gui.sh --uninstall                         # tout désinstaller
```

## Utilisation

Au lancement, la GUI se connecte au serveur configuré et affiche dans le menu déroulant « Appareil » tous les clients déjà vus, avec leur nom de machine et leur état (connecté ou hors ligne). Il suffit de sélectionner l'appareil à piloter : le bouton « Allumer la caméra » devient actif dès que l'appareil est détecté comme connecté. Le cliquer démarre la capture sur ce client précis ; « Éteindre la caméra » l'arrête. Le flux vidéo s'affiche automatiquement dans la fenêtre, rafraîchi plusieurs fois par seconde.

Pour ajouter un nouvel appareil, pas besoin de revenir sur GitHub : le bouton « Télécharger le client (.exe) » dans la GUI récupère `CameraClient-Setup.exe` directement depuis le serveur déjà configuré et demande où l'enregistrer. Il suffit ensuite de l'exécuter sur la nouvelle machine (ou d'utiliser l'une des commandes silencieuses ci-dessus) ; l'appareil apparaît automatiquement dans le menu déroulant dès sa première connexion, avec son propre `device_id`.

## Construire les exécutables Windows

En local, sur un PC Windows avec Python 3.10+ :

```bat
packaging\windows\build_windows.bat
```

Produit `dist\CameraClient.exe` (client, sans fenêtre), `dist\CameraClient-console.exe` (client, diagnostic avec console), `dist\CameraServerGUI.exe` (GUI), et, si Inno Setup est installé, `releases\CameraClient-Setup.exe` (installateur silencieux du client).

Sur GitHub, deux workflows séparés et indépendants construisent et publient automatiquement ces exécutables dans `releases/` à chaque modification des fichiers concernés, ou manuellement depuis l'onglet « Actions » : `build-windows-client.yml` pour le client (et son installateur Inno Setup), `build-windows-gui.yml` pour la GUI.

## Dépannage rapide

Le script `packaging/windows/installer.iss` doit impérativement vivre en dehors du dossier `build/` (utilisé comme espace de travail par PyInstaller et nettoyé à chaque build) : c'est pour ça qu'il est rangé dans `packaging/windows/` et non `build/windows/`. Si la compilation Inno Setup échoue avec « The system cannot find the path specified », vérifier en premier lieu que `installer.iss` est bien à cet emplacement et que `[Languages]` utilise `compiler:Default.isl` (l'anglais, toujours présent) plutôt qu'un fichier de traduction optionnel comme `French.isl`.
