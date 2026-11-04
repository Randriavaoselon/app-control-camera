"""
Serveur de contrôle caméra — API HTTP.

Ce serveur est le point central : il peut piloter PLUSIEURS clients caméra
(appareils) en même temps. Chaque appareil appartient à un seul serveur,
mais un serveur peut gérer autant d'appareils que nécessaire, identifiés
chacun par un device_id unique envoyé par client.py. L'interface de
contrôle (GUI) s'y connecte elle aussi en HTTP, même si elle n'est pas sur
le même réseau Wi-Fi. Il suffit que ce serveur soit accessible
publiquement (déployé sur Render, par exemple, comme pour Wifi Tracker).
"""

import time
import threading
from datetime import datetime

from pathlib import Path

from flask import Flask, request, jsonify, Response, send_from_directory

app = Flask(__name__)

# Dossier de ce fichier : sert client.py, server_gui.py et leurs
# requirements tels quels, pour que les scripts d'installation puissent
# les télécharger avec curl.
PROJECT_DIR = Path(__file__).resolve().parent
GUI_INSTALL_DIR = PROJECT_DIR / "install" / "linux"

# Un serveur -> plusieurs clients : chaque appareil a son propre état,
# rangé dans ce dictionnaire sous son device_id. Protégé par un verrou.
devices_lock = threading.Lock()
devices: dict[str, dict] = {}

CLIENT_TIMEOUT_SECONDS = 10


def _default_device_state(name: str | None) -> dict:
    return {
        "name": name or "Appareil inconnu",
        "command": "STOP_CAMERA",
        "camera_on": False,
        "latest_frame": None,
        "last_seen": None,
    }


def _get_or_create_device(device_id: str, name: str | None = None) -> dict:
    """Appelé uniquement depuis le code du CLIENT : un appareil encore
    inconnu est automatiquement enregistré (c'est ainsi qu'un nouveau
    client rejoint le serveur)."""
    device = devices.get(device_id)
    if device is None:
        device = _default_device_state(name)
        devices[device_id] = device
    elif name:
        device["name"] = name
    return device


def _device_is_alive(device: dict) -> bool:
    if device["last_seen"] is None:
        return False
    return (time.time() - device["last_seen"]) < CLIENT_TIMEOUT_SECONDS


# ---------------------------------------------------------------------
# Routes consommées par un CLIENT caméra précis (device_id obligatoire)
# ---------------------------------------------------------------------

@app.route("/api/command", methods=["GET"])
def get_command():
    """Chaque client interroge cette route régulièrement avec son device_id."""
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400

    with devices_lock:
        device = _get_or_create_device(device_id, request.args.get("name"))
        device["last_seen"] = time.time()
        return jsonify({"command": device["command"]})


@app.route("/api/frame", methods=["POST"])
def post_frame():
    """Le client envoie une image JPEG (corps brut) à chaque capture, pour SON device_id."""
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400

    with devices_lock:
        device = _get_or_create_device(device_id)
        device["latest_frame"] = request.get_data()
        device["last_seen"] = time.time()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------
# Routes consommées par l'INTERFACE DE CONTRÔLE (GUI)
# ---------------------------------------------------------------------

@app.route("/api/devices", methods=["GET"])
def list_devices():
    """Liste tous les appareils déjà vus par ce serveur."""
    with devices_lock:
        result = []
        for device_id, device in devices.items():
            last_seen = (
                datetime.fromtimestamp(device["last_seen"]).strftime("%H:%M:%S")
                if device["last_seen"] else None
            )
            result.append({
                "device_id": device_id,
                "name": device["name"],
                "camera_on": device["camera_on"],
                "connected": _device_is_alive(device),
                "last_seen": last_seen,
            })
    return jsonify({"devices": result})


@app.route("/api/command", methods=["POST"])
def set_command():
    """La GUI envoie {'device_id': ..., 'command': 'START_CAMERA'|'STOP_CAMERA'}."""
    payload = request.get_json(force=True, silent=True) or {}
    device_id = payload.get("device_id")
    command = payload.get("command")

    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400
    if command not in ("START_CAMERA", "STOP_CAMERA"):
        return jsonify({"error": "commande invalide"}), 400

    with devices_lock:
        device = devices.get(device_id)
        if device is None:
            return jsonify({"error": "appareil inconnu"}), 404
        device["command"] = command
        device["camera_on"] = command == "START_CAMERA"
    return jsonify({"ok": True})


@app.route("/api/status", methods=["GET"])
def get_status():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400

    with devices_lock:
        device = devices.get(device_id)
        if device is None:
            return jsonify({"error": "appareil inconnu"}), 404
        last_seen = (
            datetime.fromtimestamp(device["last_seen"]).strftime("%H:%M:%S")
            if device["last_seen"] else None
        )
        return jsonify({
            "name": device["name"],
            "camera_on": device["camera_on"],
            "client_connected": _device_is_alive(device),
            "last_seen": last_seen,
        })


@app.route("/api/frame/latest", methods=["GET"])
def get_latest_frame():
    device_id = request.args.get("device_id")
    if not device_id:
        return "", 400

    with devices_lock:
        device = devices.get(device_id)
        frame = device["latest_frame"] if device else None

    if frame is None:
        return "", 204
    return Response(frame, mimetype="image/jpeg")


# ---------------------------------------------------------------------
# Téléchargement du CLIENT (source + installateur Windows)
# ---------------------------------------------------------------------

@app.route("/client/download", methods=["GET"])
def download_client():
    return send_from_directory(PROJECT_DIR, "client.py", as_attachment=False)


@app.route("/client/requirements", methods=["GET"])
def download_requirements():
    return send_from_directory(PROJECT_DIR, "requirements.txt", as_attachment=False)


@app.route("/download/windows", methods=["GET"])
def download_windows_installer():
    """
    Sert l'installateur Windows du CLIENT (CameraClient-Setup.exe).

    Ce fichier n'est PAS construit ici : il est fabriqué une fois, sur un PC
    Windows (ou via GitHub Actions), avec build/windows/build_windows.bat
    (PyInstaller + Inno Setup), puis déposé dans releases/.
    C'est ce fichier que le bouton « Télécharger le client » de server_gui.py
    récupère.
    """
    releases_dir = PROJECT_DIR / "releases"
    filename = "CameraClient-Setup.exe"
    if not (releases_dir / filename).exists():
        return jsonify({"error": "Installateur Windows du client pas encore publié"}), 404
    return send_from_directory(releases_dir, filename, as_attachment=True)


# ---------------------------------------------------------------------
# Téléchargement de la GUI (installation sous Linux via curl)
# ---------------------------------------------------------------------

@app.route("/gui/install", methods=["GET"])
def download_gui_installer_script():
    """Le script d'installation Linux de la GUI lui-même : c'est lui qu'on
    récupère avec curl pour installer la commande `camera-gui`."""
    return send_from_directory(GUI_INSTALL_DIR, "install_gui.sh", as_attachment=False)


@app.route("/gui/download", methods=["GET"])
def download_gui_source():
    return send_from_directory(PROJECT_DIR, "server_gui.py", as_attachment=False)


@app.route("/gui/requirements", methods=["GET"])
def download_gui_requirements():
    return send_from_directory(PROJECT_DIR, "requirements-gui.txt", as_attachment=False)


@app.route("/download/windows/gui", methods=["GET"])
def download_windows_gui_exe():
    """Sert directement CameraServerGUI.exe, si l'admin préfère le récupérer sans curl."""
    releases_dir = PROJECT_DIR / "releases"
    filename = "CameraServerGUI.exe"
    if not (releases_dir / filename).exists():
        return jsonify({"error": "Exécutable Windows de la GUI pas encore publié"}), 404
    return send_from_directory(releases_dir, filename, as_attachment=True)


if __name__ == "__main__":
    # host="0.0.0.0" pour être joignable depuis l'extérieur (ou via Render)
    app.run(host="0.0.0.0", port=5050, threaded=True)