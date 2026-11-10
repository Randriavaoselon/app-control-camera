LOCATION_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Partage de position</title>
<style>
  body {{
    margin: 0; min-height: 100vh; display: flex; flex-direction: column;
    align-items: center; justify-content: center; text-align: center;
    background: #0b1020; color: #e8ecf8; font-family: -apple-system, "Segoe UI", sans-serif;
    padding: 24px; box-sizing: border-box;
  }}
  h1 {{ font-size: 1.15rem; margin-bottom: 4px; }}
  p {{ color: #8b95b5; font-size: 0.9rem; margin: 6px 0; }}
  #dot {{
    width: 14px; height: 14px; border-radius: 50%; background: #f59e0b;
    margin: 18px auto; box-shadow: 0 0 0 0 rgba(245,158,11,0.6);
    animation: pulse 1.6s infinite;
  }}
  #dot.ok {{ background: #22c55e; box-shadow: 0 0 0 0 rgba(34,197,94,0.6); animation: none; }}
  #dot.err {{ background: #ef4444; animation: none; }}
  @keyframes pulse {{
    0% {{ box-shadow: 0 0 0 0 rgba(245,158,11,0.5); }}
    70% {{ box-shadow: 0 0 0 14px rgba(245,158,11,0); }}
    100% {{ box-shadow: 0 0 0 0 rgba(245,158,11,0); }}
  }}
  #coords {{ font-family: Consolas, monospace; font-size: 0.85rem; color: #c5cce3; }}
</style>
</head>
<body>
  <h1>📍 Partage de position en cours</h1>
  <p>Cette fenêtre peut être laissée ouverte en arrière-plan.</p>
  <div id="dot"></div>
  <p id="status">Demande d'autorisation de localisation...</p>
  <p id="coords"></p>
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
    statusEl.textContent = "Position transmise au serveur.";
    coordsEl.textContent = lat.toFixed(6) + ", " + lon.toFixed(6) + "  (± " + Math.round(acc) + " m)";
    const now = Date.now();
    if (now - lastSent > 2000) {{
      lastSent = now;
      send(lat, lon, acc);
    }}
  }}

  function onError(err) {{
    dot.className = "err";
    statusEl.textContent = "Localisation refusée ou indisponible (" + err.message + ").";
  }}

  if (!navigator.geolocation) {{
    dot.className = "err";
    statusEl.textContent = "Ce navigateur ne supporte pas la géolocalisation.";
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