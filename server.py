"""
Serveur de contrôle caméra / écran — API HTTP.

Ce serveur est le point central : il peut piloter PLUSIEURS clients
(appareils) en même temps. Chaque appareil appartient à un seul serveur,
mais un serveur peut gérer autant d'appareils que nécessaire, identifiés
chacun par un device_id unique envoyé par client.py. Pour chaque appareil,
DEUX flux indépendants sont suivis : la caméra et la capture d'écran.
L'interface de contrôle (GUI) s'y connecte elle aussi en HTTP, même si
elle n'est pas sur le même réseau Wi-Fi.
"""

import time
import threading
from datetime import datetime

from pathlib import Path

from flask import Flask, request, jsonify, Response, send_from_directory, redirect

app = Flask(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
GUI_INSTALL_DIR = PROJECT_DIR / "install" / "linux"

# Les installateurs Windows (.exe) ne sont plus commités dans le dépôt
# (ils gonflaient l'historique Git à chaque build) : ils sont publiés comme
# assets d'une GitHub Release, mise à jour à chaque build via le workflow.
# Changez ces constantes si le nom du dépôt ou les tags de Release changent.
GITHUB_RELEASE_BASE = "https://github.com/Randriavaoselon/app-control-camera/releases/download/client-latest"
GITHUB_RELEASE_BASE_GUI = "https://github.com/Randriavaoselon/app-control-camera/releases/download/client-latest"

devices_lock = threading.Lock()
devices: dict[str, dict] = {}

CLIENT_TIMEOUT_SECONDS = 10
STREAM_NAMES = ("camera", "screen")


@app.route("/health", methods=["GET"])
def health():
    """Simple vérification que le serveur répond, indépendamment de tout appareil connecté."""
    return jsonify({"status": "ok"})


def _default_stream_state() -> dict:
    return {"command": "STOP", "active": False, "latest_frame": None}


def _default_device_state(name: str | None) -> dict:
    return {
        "name": name or "Appareil inconnu",
        "last_seen": None,
        "streams": {stream: _default_stream_state() for stream in STREAM_NAMES},
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
# Routes consommées par un CLIENT précis (device_id obligatoire)
# ---------------------------------------------------------------------

@app.route("/api/command", methods=["GET"])
def get_command():
    """Le client interroge cette route régulièrement avec son device_id ;
    reçoit l'état voulu pour SES DEUX flux en une seule requête."""
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400

    with devices_lock:
        device = _get_or_create_device(device_id, request.args.get("name"))
        device["last_seen"] = time.time()
        commands = {stream: device["streams"][stream]["command"] for stream in STREAM_NAMES}
        return jsonify({"commands": commands})


@app.route("/api/frame", methods=["POST"])
def post_frame():
    """Le client envoie une image JPEG (corps brut) pour un flux précis
    (camera ou screen) de SON device_id."""
    device_id = request.args.get("device_id")
    stream = request.args.get("stream")
    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400
    if stream not in STREAM_NAMES:
        return jsonify({"error": "stream invalide (camera ou screen attendu)"}), 400

    with devices_lock:
        device = _get_or_create_device(device_id)
        device["streams"][stream]["latest_frame"] = request.get_data()
        device["last_seen"] = time.time()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------
# Routes consommées par l'INTERFACE DE CONTRÔLE (GUI)
# ---------------------------------------------------------------------

@app.route("/api/devices", methods=["GET"])
def list_devices():
    """Liste tous les appareils déjà vus par ce serveur, avec l'état de leurs deux flux."""
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
                "camera_on": device["streams"]["camera"]["active"],
                "screen_on": device["streams"]["screen"]["active"],
                "connected": _device_is_alive(device),
                "last_seen": last_seen,
            })
    return jsonify({"devices": result})


@app.route("/api/command", methods=["POST"])
def set_command():
    """La GUI envoie {'device_id': ..., 'stream': 'camera'|'screen', 'command': 'START'|'STOP'}."""
    payload = request.get_json(force=True, silent=True) or {}
    device_id = payload.get("device_id")
    stream = payload.get("stream")
    command = payload.get("command")

    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400
    if stream not in STREAM_NAMES:
        return jsonify({"error": "stream invalide (camera ou screen attendu)"}), 400
    if command not in ("START", "STOP"):
        return jsonify({"error": "commande invalide (START ou STOP attendu)"}), 400

    with devices_lock:
        device = devices.get(device_id)
        if device is None:
            return jsonify({"error": "appareil inconnu"}), 404
        device["streams"][stream]["command"] = command
        device["streams"][stream]["active"] = command == "START"
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
            "client_connected": _device_is_alive(device),
            "last_seen": last_seen,
            "camera_on": device["streams"]["camera"]["active"],
            "screen_on": device["streams"]["screen"]["active"],
        })


@app.route("/api/frame/latest", methods=["GET"])
def get_latest_frame():
    device_id = request.args.get("device_id")
    stream = request.args.get("stream")
    if not device_id or stream not in STREAM_NAMES:
        return "", 400

    with devices_lock:
        device = devices.get(device_id)
        frame = device["streams"][stream]["latest_frame"] if device else None

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
    """L'installateur n'est plus stocké sur ce serveur : il est publié comme
    asset d'une GitHub Release à chaque build (voir le workflow CI), donc on
    redirige simplement vers son URL stable."""
    return redirect(f"{GITHUB_RELEASE_BASE}/CameraClient-Setup.exe")


# ---------------------------------------------------------------------
# Téléchargement de la GUI (installation sous Linux via curl)
# ---------------------------------------------------------------------

@app.route("/gui/install", methods=["GET"])
def download_gui_installer_script():
    return send_from_directory(GUI_INSTALL_DIR, "install_gui.sh", as_attachment=False)


@app.route("/gui/download", methods=["GET"])
def download_gui_source():
    return send_from_directory(PROJECT_DIR, "server_gui.py", as_attachment=False)


@app.route("/gui/requirements", methods=["GET"])
def download_gui_requirements():
    return send_from_directory(PROJECT_DIR, "requirements-gui.txt", as_attachment=False)


@app.route("/download/windows/gui", methods=["GET"])
def download_windows_gui_exe():
    """Même principe que /download/windows : l'exe de la GUI est aussi
    publié en GitHub Release plutôt que stocké sur ce serveur."""
    return redirect(f"{GITHUB_RELEASE_BASE_GUI}/CameraServerGUI.exe")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5050, threaded=True)