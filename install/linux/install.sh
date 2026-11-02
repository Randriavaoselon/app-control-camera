#!/usr/bin/env bash
# =============================================================================
#  Client caméra - installation sur Linux (par utilisateur, sans root)
#
#  Après installation, la commande « camera-client » fonctionne depuis
#  N'IMPORTE quel dossier.
#
#  Depuis un dossier local (client.py à côté de ce script) :
#      ./install.sh --server https://mon-serveur.com --autostart
#
#  Depuis n'importe où, en téléchargeant le client depuis le serveur :
#      curl -fsSL https://mon-serveur.com/client/download -o /tmp/install.sh \
#        && bash /tmp/install.sh --server https://mon-serveur.com --autostart
#      (fonctionne aussi en pipe : curl ... | bash -s -- --server ... --autostart)
#
#  Options :
#      --server URL     adresse du serveur (sauvegardée, plus besoin de la retaper)
#      --autostart      démarre le client automatiquement au démarrage (systemd utilisateur)
#      --uninstall      désinstalle tout (programme, service, configuration)
#      -h, --help       cette aide
#
#  Emplacements :
#      programme       ~/.local/share/camera-client/
#      commande        ~/.local/bin/camera-client
#      configuration   ~/.local/share/camera-client/config.json   (droits 600)
#      service         ~/.config/systemd/user/camera-client.service
# =============================================================================
set -euo pipefail

APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/camera-client"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/camera-client"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_FILE="$UNIT_DIR/camera-client.service"
SELF="${BASH_SOURCE[0]:-}"          # vide quand le script arrive par « curl | bash »
SRC_DIR=""
[ -n "$SELF" ] && [ -f "$SELF" ] && SRC_DIR="$(cd "$(dirname "$SELF")/../.." && pwd)"

SERVER="" AUTOSTART=0 UNINSTALL=0

say()  { printf '%s\n' "$*"; }
fail() { printf '❌ %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

fetch() {
  if have curl; then curl -fsSL "$1" -o "$2"
  elif have wget; then wget -q "$1" -O "$2"
  else python3 -c 'import sys,urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$1" "$2"
  fi
}

while [ $# -gt 0 ]; do
  case "$1" in
    --server)    [ $# -ge 2 ] || fail "--server attend une valeur"; SERVER="$2"; shift 2 ;;
    --autostart) AUTOSTART=1; shift ;;
    --uninstall) UNINSTALL=1; shift ;;
    -h|--help)
      sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) fail "Option inconnue : $1 (voir --help)" ;;
  esac
done

[ "$(id -u)" -ne 0 ] || fail "Lancez ce script avec votre utilisateur normal (pas root, pas sudo)."

# ---------------------------------------------------------------- désinstallation
if [ "$UNINSTALL" -eq 1 ]; then
  say "🗑  Désinstallation du client caméra..."
  if have systemctl; then
    systemctl --user disable --now camera-client 2>/dev/null || true
    systemctl --user daemon-reload 2>/dev/null || true
  fi
  rm -f "$UNIT_FILE" "$LAUNCHER"
  pkill -u "$(id -u)" -f "$APP_DIR/client.py" 2>/dev/null || true
  rm -rf "$APP_DIR"
  say "✅ Désinstallé (programme, service, configuration)."
  exit 0
fi

# ---------------------------------------------------------------- vérifications
have python3 || fail "python3 est introuvable. Installez-le (ex. : sudo apt install python3 python3-venv)."

# ---------------------------------------------------------------- copie du programme
say "📦 Installation dans $APP_DIR"
mkdir -p "$APP_DIR" "$BIN_DIR"
SERVER="${SERVER%/}"

if [ -n "$SRC_DIR" ] && [ -f "$SRC_DIR/client.py" ]; then
  install -m 755 "$SRC_DIR/client.py" "$APP_DIR/client.py"
  install -m 644 "$SRC_DIR/requirements.txt" "$APP_DIR/requirements.txt"
else
  [ -n "$SERVER" ] || fail "client.py est introuvable à côté du script : ajoutez --server https://mon-serveur.com pour le télécharger."
  say "⬇  Téléchargement du client depuis $SERVER"
  fetch "$SERVER/client/download" "$APP_DIR/client.py.new" \
    || { rm -f "$APP_DIR/client.py.new"; fail "Téléchargement impossible depuis $SERVER."; }
  head -c 64 "$APP_DIR/client.py.new" | grep -q python || { rm -f "$APP_DIR/client.py.new"; fail "Le serveur n'a pas renvoyé client.py."; }
  chmod 755 "$APP_DIR/client.py.new"; mv -f "$APP_DIR/client.py.new" "$APP_DIR/client.py"
  fetch "$SERVER/client/requirements" "$APP_DIR/requirements.txt" 2>/dev/null || true
fi

# ---------------------------------------------------------------- environnement Python
say "🐍 Environnement Python"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip --quiet
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" --quiet

# ---------------------------------------------------------------- commande globale
cat > "$LAUNCHER" << EOF
#!/usr/bin/env bash
exec "$APP_DIR/venv/bin/python" "$APP_DIR/client.py" "\$@"
EOF
chmod 755 "$LAUNCHER"
say "✅ Commande installée : camera-client"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "⚠️  $BIN_DIR n'est pas dans votre PATH. Ajoutez à votre ~/.bashrc : export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# ---------------------------------------------------------------- configuration
if [ -n "$SERVER" ]; then
  say "💾 Sauvegarde du serveur ($SERVER)"
  "$LAUNCHER" --server "$SERVER" --save --once
fi

# ---------------------------------------------------------------- démarrage automatique
if [ "$AUTOSTART" -eq 1 ]; then
  have systemctl || fail "systemd introuvable : --autostart nécessite systemctl --user."
  say "🚀 Configuration du démarrage automatique (service systemd utilisateur)"
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_FILE" << EOF
[Unit]
Description=Client caméra - connexion automatique au serveur
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$LAUNCHER
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now camera-client
  loginctl enable-linger "$USER" 2>/dev/null || true
  say "✅ Service activé : systemctl --user status camera-client"
fi

say ""
say "Terminé. Commandes utiles :"
say "  camera-client --server https://... --save   # changer de serveur"
say "  camera-client --once --debug                # tester"
say "  ./install.sh --uninstall                     # tout désinstaller"
