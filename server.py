"""
Serveur de contrôle caméra — API HTTP.

Ce serveur est le point central : le client caméra et l'interface de
contrôle (GUI) s'y connectent tous les deux en HTTP, même s'ils ne sont
pas sur le même réseau Wi-Fi. Il suffit que ce serveur soit accessible
publiquement (déployé sur Render, par exemple, comme pour Wifi Tracker).
"""

import time
import threading
from datetime import datetime

from pathlib import Path

from flask import Flask, request, jsonify, Response, send_from_directory

app = Flask(__name__)

# Dossier de ce fichier : sert client.py et requirements.txt tels quels,
# pour que les scripts d'installation puissent les télécharger avec curl.
PROJECT_DIR = Path(__file__).resolve().parent

# État partagé entre les requêtes, protégé par un verrou
state_lock = threading.Lock()
state = {
    "command": "STOP_CAMERA",
    "camera_on": False,
    "latest_frame": None,
    "last_seen": None,
}

CLIENT_TIMEOUT_SECONDS = 10


def _client_is_alive() -> bool:
    if state["last_seen"] is None:
        return False
    return (time.time() - state["last_seen"]) < CLIENT_TIMEOUT_SECONDS


# ---------------------------------------------------------------------
# Routes consommées par le CLIENT caméra
# ---------------------------------------------------------------------

@app.route("/api/command", methods=["GET"])
def get_command():
    """Le client interroge cette route régulièrement pour savoir quoi faire."""
    with state_lock:
        state["last_seen"] = time.time()
        return jsonify({"command": state["command"]})


@app.route("/api/frame", methods=["POST"])
def post_frame():
    """Le client envoie une image JPEG (corps brut) à chaque capture."""
    with state_lock:
        state["latest_frame"] = request.get_data()
        state["last_seen"] = time.time()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------
# Routes consommées par l'INTERFACE DE CONTRÔLE (GUI)
# ---------------------------------------------------------------------

@app.route("/api/command", methods=["POST"])
def set_command():
    """La GUI envoie {'command': 'START_CAMERA'} ou {'command': 'STOP_CAMERA'}."""
    payload = request.get_json(force=True, silent=True) or {}
    command = payload.get("command")
    if command not in ("START_CAMERA", "STOP_CAMERA"):
        return jsonify({"error": "commande invalide"}), 400

    with state_lock:
        state["command"] = command
        state["camera_on"] = command == "START_CAMERA"
    return jsonify({"ok": True})


@app.route("/api/status", methods=["GET"])
def get_status():
    with state_lock:
        last_seen = (
            datetime.fromtimestamp(state["last_seen"]).strftime("%H:%M:%S")
            if state["last_seen"] else None
        )
        return jsonify({
            "camera_on": state["camera_on"],
            "client_connected": _client_is_alive(),
            "last_seen": last_seen,
        })


@app.route("/api/frame/latest", methods=["GET"])
def get_latest_frame():
    with state_lock:
        frame = state["latest_frame"]
    if frame is None:
        return "", 204
    return Response(frame, mimetype="image/jpeg")


# ---------------------------------------------------------------------
# Téléchargement du client (pour `curl | bash` depuis les scripts d'installation)
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
    Sert l'installateur Windows (CameraClient-Setup.exe).

    Ce fichier n'est PAS construit ici : il est fabriqué une fois, sur un PC
    Windows, avec build/windows/build_windows.bat (PyInstaller + Inno Setup),
    puis déposé dans le dossier releases/ du projet (et donc envoyé sur
    GitHub / PythonAnywhere avec `git add releases/CameraClient-Setup.exe`).
    """
    releases_dir = PROJECT_DIR / "releases"
    filename = "CameraClient-Setup.exe"
    if not (releases_dir / filename).exists():
        return jsonify({"error": "Installateur Windows pas encore publié"}), 404
    return send_from_directory(releases_dir, filename, as_attachment=True)


if __name__ == "__main__":
    # host="0.0.0.0" pour être joignable depuis l'extérieur (ou via Render)
    app.run(host="0.0.0.0", port=5050, threaded=True)
