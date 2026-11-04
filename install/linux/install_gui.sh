#!/usr/bin/env bash
# =============================================================================
#  Interface de contrôle caméra (GUI) - installation sur Linux (sans root)
#
#  Après installation, la commande « camera-gui » fonctionne depuis
#  N'IMPORTE quel dossier.
#
#  Depuis un dossier local (server_gui.py à côté de ce script) :
#      ./install_gui.sh --server https://mon-serveur.com
#
#  Depuis n'importe où, en une seule commande (curl) :
#      curl -fsSL https://mon-serveur.com/gui/install -o /tmp/install_gui.sh \
#        && bash /tmp/install_gui.sh --server https://mon-serveur.com
#
#  Options :
#      --server URL     adresse du serveur (sauvegardée, plus besoin de la retaper)
#      --uninstall      désinstalle tout (programme, configuration)
#      -h, --help       cette aide
#
#  Pré-requis : un affichage graphique (X11/Wayland) et le paquet système
#  python3-tk (tkinter), qui ne s'installe PAS avec pip :
#      Debian/Ubuntu : sudo apt install python3-tk
#      Fedora        : sudo dnf install python3-tkinter
#      Arch          : sudo pacman -S tk
#
#  Emplacements :
#      programme       ~/.local/share/camera-gui/
#      commande        ~/.local/bin/camera-gui
#      configuration   ~/.local/share/camera-gui/config.json
# =============================================================================
set -euo pipefail

APP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/camera-gui"
BIN_DIR="$HOME/.local/bin"
LAUNCHER="$BIN_DIR/camera-gui"
SELF="${BASH_SOURCE[0]:-}"          # vide quand le script arrive par « curl | bash »
SRC_DIR=""
[ -n "$SELF" ] && [ -f "$SELF" ] && SRC_DIR="$(cd "$(dirname "$SELF")/../.." && pwd)"

SERVER="" UNINSTALL=0

say()  { printf '%s\n' "$*"; }
fail() { printf '❌ %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

fetch() {
  if have curl; then curl -fsSL "$1" -o "$2"
  elif have wget; then wget -q "$1" -O "$2"
  else python3 -c 'import sys,urllib.request; urllib.request.urlretrieve(sys.argv[1], sys.argv[2])' "$1" "$2"
  fi
}

# Vérifie que le fichier reçu est bien un module Python (docstring """ en
# tête), et pas une page d'erreur HTML ou un JSON d'erreur Flask.
looks_like_python() {
  head -c 3 "$1" | grep -q '"""'
}

while [ $# -gt 0 ]; do
  case "$1" in
    --server)    [ $# -ge 2 ] || fail "--server attend une valeur"; SERVER="$2"; shift 2 ;;
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
  say "🗑  Désinstallation de la GUI caméra..."
  rm -f "$LAUNCHER"
  rm -rf "$APP_DIR"
  say "✅ Désinstallé (programme, configuration)."
  exit 0
fi

# ---------------------------------------------------------------- vérifications
have python3 || fail "python3 est introuvable. Installez-le (ex. : sudo apt install python3 python3-venv)."
python3 -c "import tkinter" 2>/dev/null \
  || fail "Le module tkinter est manquant. Installez-le (ex. : sudo apt install python3-tk) puis relancez."

# ---------------------------------------------------------------- copie du programme
say "📦 Installation dans $APP_DIR"
mkdir -p "$APP_DIR" "$BIN_DIR"
SERVER="${SERVER%/}"

if [ -n "$SRC_DIR" ] && [ -f "$SRC_DIR/server_gui.py" ]; then
  install -m 755 "$SRC_DIR/server_gui.py" "$APP_DIR/server_gui.py"
  install -m 644 "$SRC_DIR/requirements-gui.txt" "$APP_DIR/requirements-gui.txt"
else
  [ -n "$SERVER" ] || fail "server_gui.py est introuvable à côté du script : ajoutez --server https://mon-serveur.com pour le télécharger."
  say "⬇  Téléchargement de la GUI depuis $SERVER"
  fetch "$SERVER/gui/download" "$APP_DIR/server_gui.py.new" \
    || { rm -f "$APP_DIR/server_gui.py.new"; fail "Téléchargement impossible depuis $SERVER."; }
  looks_like_python "$APP_DIR/server_gui.py.new" \
    || { rm -f "$APP_DIR/server_gui.py.new"; fail "Le serveur n'a pas renvoyé server_gui.py (route /gui/download absente ou en erreur)."; }
  chmod 755 "$APP_DIR/server_gui.py.new"; mv -f "$APP_DIR/server_gui.py.new" "$APP_DIR/server_gui.py"
  fetch "$SERVER/gui/requirements" "$APP_DIR/requirements-gui.txt" 2>/dev/null || true
fi

# ---------------------------------------------------------------- environnement Python
# --system-site-packages : nécessaire pour voir tkinter, qui n'est PAS installable par pip.
say "🐍 Environnement Python"
python3 -m venv --system-site-packages "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip --quiet
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements-gui.txt" --quiet

# ---------------------------------------------------------------- commande globale
cat > "$LAUNCHER" << EOF
#!/usr/bin/env bash
exec "$APP_DIR/venv/bin/python" "$APP_DIR/server_gui.py" "\$@"
EOF
chmod 755 "$LAUNCHER"
say "✅ Commande installée : camera-gui"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) say "⚠️  $BIN_DIR n'est pas dans votre PATH. Ajoutez à votre ~/.bashrc : export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

# ---------------------------------------------------------------- configuration
if [ -n "$SERVER" ]; then
  say "💾 Sauvegarde du serveur ($SERVER)"
  mkdir -p "$APP_DIR"
  python3 -c "import json,sys; open(sys.argv[1],'w').write(json.dumps({'server_url': sys.argv[2]}, indent=2))" \
    "$APP_DIR/config.json" "$SERVER"
fi

say ""
say "Terminé. Commandes utiles :"
say "  camera-gui                         # lancer l'interface"
say "  camera-gui --server https://... --save   # changer de serveur"
say "  ./install_gui.sh --uninstall       # tout désinstaller"