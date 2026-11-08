"""
Interface graphique de contrôle — consomme l'API HTTP du serveur.

Cette GUI ne parle jamais directement aux clients : elle passe toujours
par l'API du serveur. Les flux caméra et écran s'affichent EN PERMANENCE
côte à côte, chacun avec ses propres contrôles (démarrer/arrêter/
enregistrer) : ce sont deux panneaux totalement indépendants l'un de
l'autre, y compris pour l'enregistrement (on peut enregistrer l'un sans
toucher à l'autre).

Enregistrement : un thread dédié par flux (VideoRecorder) écrit les images
à cadence FIXE dans le temps réel (la dernière image reçue est répétée
tant qu'aucune nouvelle n'arrive), toutes à la même taille, puis le
fichier est relu et vérifié à l'arrêt pour signaler un problème (vidéo
vide, écran noir...).
"""

import argparse
import io
import json
import os
import platform
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import requests
from PIL import Image, ImageDraw, ImageFilter, ImageTk

try:
    # Carte OpenStreetMap intégrée (pip install tkintermapview). Optionnelle :
    # si absente, le panneau de localisation reste fonctionnel (boutons +
    # coordonnées en texte), seule la carte visuelle est remplacée par un message.
    from tkintermapview import TkinterMapView
    _HAS_MAP = True
except ImportError:
    TkinterMapView = None
    _HAS_MAP = False

DEFAULT_SERVER_URL = "http://127.0.0.1:5050"
POLL_DEVICES_INTERVAL = 2.0
POLL_STATUS_INTERVAL = 1.0
POLL_LOCATION_INTERVAL = 2.0
DEFAULT_MAP_LAT = -18.8792    # Antananarivo : position affichée tant qu'aucune position n'est reçue
DEFAULT_MAP_LON = 47.5079
POLL_FRAME_INTERVAL = 0.03   # pause après une réponse vide (204) : 0.1 ajoutait jusqu'à 100 ms de latence
# Attente côté serveur d'une nouvelle image (long-polling). 0 = désactivé : OBLIGATOIRE sur
# PythonAnywhere, dont le nombre de workers est très limité (une requête qui attend bloquerait
# les envois du client). Avec un VPS / gunicorn --threads 8, mettre 2.0 réduit encore la latence.
FRAME_LONG_POLL_SECONDS = 0.0
REQUEST_TIMEOUT = 5
DOWNLOAD_TIMEOUT = 30
RECORD_FPS_CAMERA = 10
RECORD_FPS_SCREEN = 5        # l'écran arrive bien plus lentement que la caméra
MAX_RECORD_WIDTH = 1920      # on réduit les écrans plus larges (CPU + taille du fichier)
STALL_WARN_SECONDS = 10.0    # aucune nouvelle image reçue depuis ce délai -> avertissement
BLACK_FRAME_STD = 2.0        # écart-type en dessous duquel une image est considérée noire/uniforme
LOG_THROTTLE_SECONDS = 5.0

# --- Thème « Midnight » : fond bleu nuit, cartes sombres, accents néon doux ---
COLOR_BG = "#0b1020"        # fond de la fenêtre
COLOR_CARD = "#141b30"      # cartes (panneaux, journal)
COLOR_CARD_HI = "#1b2440"   # survol / éléments surélevés
COLOR_BORDER = "#263150"
COLOR_VIDEO_BG = "#070a14"
COLOR_TEXT = "#e8ecf8"
COLOR_MUTED = "#8b95b5"
COLOR_ACCENT = "#4f8cff"    # bleu principal
COLOR_ACCENT_HI = "#6aa0ff"
COLOR_CAMERA = "#22d3ee"    # cyan : identité visuelle du flux caméra
COLOR_SCREEN = "#a78bfa"    # violet : identité visuelle du flux écran
COLOR_OK = "#22c55e"
COLOR_OK_HI = "#34d874"
COLOR_KO = "#ef4444"
COLOR_KO_HI = "#f56565"
COLOR_WARN = "#f59e0b"
COLOR_REC = "#ef4444"
COLOR_DISABLED_BG = "#222b45"
COLOR_DISABLED_FG = "#5d6788"

FONT = "Segoe UI" if platform.system() == "Windows" else "DejaVu Sans"
FONT_MONO = "Consolas" if platform.system() == "Windows" else "DejaVu Sans Mono"
BANNER_HEIGHT = 96
STREAM_ACCENTS = {"camera": COLOR_CAMERA, "screen": COLOR_SCREEN}

# Codecs essayés dans l'ordre, selon l'extension choisie par l'utilisateur.
# cv2.VideoWriter ne lève AUCUNE exception si un codec est indisponible :
# il faut vérifier isOpened() soi-même, sinon l'enregistrement échoue en
# silence (fichier vide ou absent, sans aucun message).
FOURCC_CANDIDATES_MP4 = ["mp4v", "avc1", "X264"]
FOURCC_CANDIDATES_AVI = ["XVID", "MJPG"]


# ---------------------------------------------------------------------
# Configuration persistée (adaptée à l'OS)
# ---------------------------------------------------------------------

def _config_dir() -> Path:
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home()))
        return base / "CameraGui"
    return Path.home() / ".local" / "share" / "camera-gui"


CONFIG_DIR = _config_dir()
CONFIG_PATH = CONFIG_DIR / "config.json"


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_config(server_url: str) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"server_url": server_url}, indent=2), encoding="utf-8")


def resolve_server_url(cli_value: str | None) -> str:
    if cli_value:
        return cli_value.rstrip("/")
    config_value = load_config().get("server_url")
    if config_value:
        return config_value.rstrip("/")
    return DEFAULT_SERVER_URL


# ---------------------------------------------------------------------
# Images générées par le code (aucun fichier à fournir)
# ---------------------------------------------------------------------

def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _custom_banner_path() -> Path | None:
    """Si vous déposez une image « banner.png » (ou .jpg) à côté de ce script
    — ou dans le dossier de configuration — elle remplace le dégradé du bandeau."""
    for folder in (Path(__file__).resolve().parent, CONFIG_DIR):
        for name in ("banner.png", "banner.jpg", "banner.jpeg"):
            candidate = folder / name
            if candidate.exists():
                return candidate
    return None


def make_banner(width: int, height: int) -> Image.Image:
    """Bandeau d'en-tête : photo personnalisée assombrie, ou dégradé bleu nuit -> indigo
    avec des halos lumineux décoratifs."""
    width = max(width, 200)
    custom = _custom_banner_path()
    if custom is not None:
        try:
            photo = Image.open(custom).convert("RGB")
            scale = max(width / photo.width, height / photo.height)
            photo = photo.resize((int(photo.width * scale) + 1, int(photo.height * scale) + 1), Image.LANCZOS)
            left, top = (photo.width - width) // 2, (photo.height - height) // 2
            photo = photo.crop((left, top, left + width, top + height))
            veil = Image.new("RGB", (width, height), _hex_to_rgb(COLOR_BG))
            return Image.blend(photo, veil, 0.62)   # voile sombre : le texte reste lisible
        except Exception:
            pass   # image illisible : on retombe sur le dégradé

    x = np.linspace(0.0, 1.0, width)[None, :, None]
    y = np.linspace(0.0, 1.0, height)[:, None, None]
    t = np.clip(x * 0.9 + y * 0.1, 0.0, 1.0)
    start = np.array([10, 16, 40], dtype=np.float32)
    end = np.array([58, 50, 168], dtype=np.float32)
    base = (start * (1 - t) + end * t).astype(np.uint8)
    image = Image.fromarray(base, "RGB").convert("RGBA")

    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(glow)
    for cx, cy, radius, color in (
        (width - 60, -20, 150, (34, 211, 238, 46)),
        (width - 230, height + 40, 130, (167, 139, 250, 52)),
        (width * 0.55, -70, 110, (79, 140, 255, 34)),
        (width - 420, height // 2, 60, (255, 255, 255, 14)),
    ):
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=color)
    glow = glow.filter(ImageFilter.GaussianBlur(28))   # halos doux, sans bords durs
    image = Image.alpha_composite(image, glow)

    line = ImageDraw.Draw(image)   # fine ligne lumineuse en bas du bandeau
    line.rectangle((0, height - 2, width, height), fill=(*_hex_to_rgb(COLOR_ACCENT), 120))
    return image.convert("RGB")


def make_logo(size: int = 56) -> Image.Image:
    """Logo : carré arrondi dégradé avec un objectif stylisé."""
    s = size * 4
    gradient = np.linspace(0.0, 1.0, s)[None, :, None] * 0.5 + np.linspace(0.0, 1.0, s)[:, None, None] * 0.5
    c1 = np.array(_hex_to_rgb(COLOR_ACCENT), dtype=np.float32)
    c2 = np.array(_hex_to_rgb(COLOR_SCREEN), dtype=np.float32)
    fill = Image.fromarray((c1 * (1 - gradient) + c2 * gradient).astype(np.uint8), "RGB").convert("RGBA")
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, s - 1, s - 1), radius=int(s * 0.26), fill=255)
    logo = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    logo.paste(fill, (0, 0), mask)
    d = ImageDraw.Draw(logo)
    d.ellipse((s * 0.22, s * 0.22, s * 0.78, s * 0.78), outline=(255, 255, 255, 255), width=int(s * 0.07))
    d.ellipse((s * 0.38, s * 0.38, s * 0.62, s * 0.62), fill=(255, 255, 255, 235))
    return logo.resize((size, size), Image.LANCZOS)


def make_placeholder(kind: str, color_hex: str, size: int = 150) -> Image.Image:
    """Pictogramme affiché dans un panneau vidéo tant qu'aucune image n'arrive."""
    s = size * 3
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    rgb = _hex_to_rgb(color_hex)
    color = (*rgb, 255)
    d.ellipse((s * 0.06, s * 0.06, s * 0.94, s * 0.94), fill=(*rgb, 26))
    d.ellipse((s * 0.14, s * 0.14, s * 0.86, s * 0.86), fill=(*rgb, 22))
    width = max(6, s // 30)
    if kind == "camera":
        d.rounded_rectangle((s * 0.24, s * 0.36, s * 0.76, s * 0.68), radius=s * 0.05, outline=color, width=width)
        d.rounded_rectangle((s * 0.38, s * 0.29, s * 0.52, s * 0.36), radius=s * 0.02, outline=color, width=width)
        d.ellipse((s * 0.40, s * 0.42, s * 0.60, s * 0.62), outline=color, width=width)
    else:
        d.rounded_rectangle((s * 0.22, s * 0.30, s * 0.78, s * 0.62), radius=s * 0.04, outline=color, width=width)
        d.line((s * 0.5, s * 0.62, s * 0.5, s * 0.72), fill=color, width=width)
        d.line((s * 0.38, s * 0.72, s * 0.62, s * 0.72), fill=color, width=width)
    return image.resize((size, size), Image.LANCZOS)


def _open_video_writer(path: str, width: int, height: int, fps: float):
    """Essaie plusieurs codecs jusqu'à en trouver un qui s'ouvre réellement.
    Retourne (writer, nom_du_codec, chemin_final) ou (None, None, path).

    Si aucun codec ne convient pour l'extension demandée, bascule sur l'autre
    famille (.mp4 -> .avi MJPG) plutôt que de produire un fichier inutilisable."""
    extension = Path(path).suffix.lower()
    attempts = [(path, FOURCC_CANDIDATES_AVI if extension == ".avi" else FOURCC_CANDIDATES_MP4)]
    if extension != ".avi":
        attempts.append((str(Path(path).with_suffix(".avi")), FOURCC_CANDIDATES_AVI))

    for target, candidates in attempts:
        for codec in candidates:
            writer = cv2.VideoWriter(target, cv2.VideoWriter_fourcc(*codec), fps, (width, height))
            if writer.isOpened():
                return writer, codec, target
            writer.release()
    return None, None, path


@dataclass
class RecordingReport:
    path: str
    codec: str | None
    frames_written: int
    frames_received: int
    duration: float
    max_std: float
    verified_frames: int
    verified_ok: bool


class VideoRecorder:
    """Écrit dans un fichier vidéo la DERNIÈRE image reçue, à cadence fixe.

    Pourquoi : le serveur ne garde que la dernière image de chaque flux et la
    GUI la récupère par polling. Écrire une image à chaque réponse HTTP donne
    une vidéo dont la vitesse dépend du réseau (accélérée ou saccadée), et un
    VideoWriter OpenCV abandonne EN SILENCE toute image dont la taille diffère
    de celle de la première (fichier .mp4 présent mais vide/illisible).
    Ici : taille figée à la première image (paire, limitée en largeur), chaque
    image est ramenée à cette taille, et l'écriture suit l'horloge réelle."""

    def __init__(self, path: str, fps: float, log=print, on_fatal=None):
        self.path = path
        self.fps = float(fps)
        self._log = log
        self._on_fatal = on_fatal
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._latest: np.ndarray | None = None
        self._last_push = 0.0
        self._size: tuple[int, int] | None = None   # (largeur, hauteur) figée
        self._writer: cv2.VideoWriter | None = None
        self._codec: str | None = None
        self._started_at: float | None = None
        self.frames_received = 0
        self.frames_written = 0
        self.max_std = 0.0
        self.failed = False

    # -- API appelée depuis le thread réseau -------------------------------
    def push(self, image: np.ndarray) -> None:
        std = float(image[::8, ::8].std())  # échantillonné : très peu coûteux
        with self._lock:
            self._latest = image
            self._last_push = time.monotonic()
            self.frames_received += 1
            self.max_std = max(self.max_std, std)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> RecordingReport:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        with self._lock:
            if self._writer is not None:
                self._writer.release()
                self._writer = None
        duration = (time.monotonic() - self._started_at) if self._started_at else 0.0
        verified_frames, verified_ok = self._verify()
        return RecordingReport(
            self.path, self._codec, self.frames_written, self.frames_received,
            duration, self.max_std, verified_frames, verified_ok,
        )

    # -- interne ------------------------------------------------------------
    def _fit_size(self, image: np.ndarray) -> tuple[int, int]:
        height, width = image.shape[:2]
        if width > MAX_RECORD_WIDTH:
            height = int(height * MAX_RECORD_WIDTH / width)
            width = MAX_RECORD_WIDTH
        return max(2, width - width % 2), max(2, height - height % 2)  # dimensions paires

    def _normalize(self, image: np.ndarray) -> np.ndarray:
        width, height = self._size
        if image.shape[1] != width or image.shape[0] != height:
            image = cv2.resize(image, (width, height), interpolation=cv2.INTER_AREA)
        return image

    def _run(self) -> None:
        interval = 1.0 / self.fps
        next_tick = time.monotonic()
        last_stall_warning = 0.0
        while not self._stop_event.wait(max(0.0, next_tick - time.monotonic())):
            now = time.monotonic()
            with self._lock:
                frame = self._latest
                last_push = self._last_push
            if frame is not None:
                if self._writer is None and not self._open_writer(frame):
                    return
                if self._started_at is None:
                    self._started_at = now
                try:
                    self._writer.write(self._normalize(frame))
                    self.frames_written += 1
                except cv2.error as exc:
                    self._log(f"Erreur d'écriture vidéo : {exc}")
                if now - last_push > STALL_WARN_SECONDS and now - last_stall_warning > STALL_WARN_SECONDS:
                    last_stall_warning = now
                    self._log(
                        f"⚠ Aucune nouvelle image depuis {now - last_push:.0f} s : la dernière image "
                        "est répétée dans la vidéo (flux arrêté ou client déconnecté ?)."
                    )
            next_tick += interval
            if now - next_tick > 1.0:   # après un blocage, on ne rattrape pas en rafale
                next_tick = now + interval

    def _open_writer(self, first_frame: np.ndarray) -> bool:
        self._size = self._fit_size(first_frame)
        writer, codec, final_path = _open_video_writer(self.path, *self._size, self.fps)
        if writer is None:
            self.failed = True
            self._log(
                f"❌ Impossible d'ouvrir {self.path} pour l'écriture vidéo (aucun codec disponible). "
                "Installez les codecs FFmpeg/GStreamer ou choisissez une extension .avi."
            )
            if self._on_fatal:
                self._on_fatal()
            return False
        if final_path != self.path:
            self._log(f"⚠ Codec MP4 indisponible : enregistrement redirigé vers {final_path}")
            self.path = final_path
        self._writer, self._codec = writer, codec
        self._log(f"Codec vidéo utilisé : {codec} — {self._size[0]}x{self._size[1]} à {self.fps:g} im/s")
        return True

    def _verify(self) -> tuple[int, bool]:
        """Relit le fichier produit : c'est la seule façon de savoir s'il est exploitable."""
        if self.frames_written == 0 or not Path(self.path).exists():
            return 0, False
        capture = cv2.VideoCapture(self.path)
        try:
            if not capture.isOpened():
                return 0, False
            count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
            ok, _ = capture.read()
            return count, bool(ok and count > 0)
        finally:
            capture.release()


class StreamPanel:
    """Regroupe tout ce qui concerne UN flux (caméra ou écran) : ses widgets
    et son état d'enregistrement. Les deux flux sont totalement indépendants
    l'un de l'autre — démarrer, arrêter ou enregistrer l'un n'affecte jamais
    l'autre, et les deux s'affichent en permanence côte à côte."""

    def __init__(self, key: str, title: str):
        self.key = key              # "camera" ou "screen"
        self.title = title          # libellé affiché, ex. "📷 Caméra"
        self.on = False             # ce flux tourne-t-il sur l'appareil sélectionné ?
        self.recording = False
        self.recorder: VideoRecorder | None = None
        self.last_jpeg: bytes | None = None
        # Affichage : taille disponible (mise à jour par le thread Tk) et
        # « boîte aux lettres » à une seule image, pour ne jamais accumuler de retard.
        self.view_size: tuple[int, int] = (420, 380)
        self.pending_image = None
        self.display_scheduled = False
        self.display_lock = threading.Lock()
        # Widgets (créés dans _build_stream_panel) :
        self.video_label: tk.Label | None = None
        self.title_var: tk.StringVar | None = None
        self.status_var: tk.StringVar | None = None
        self.btn_start: ttk.Button | None = None
        self.btn_stop: ttk.Button | None = None
        self.btn_record: ttk.Button | None = None
        self.rec_badge: tk.Label | None = None   # badge "● REC" (couleur, pas une émoji : plus fiable)
        self.status_label: tk.Label | None = None
        self.placeholder = None                  # ImageTk.PhotoImage du pictogramme d'attente


class CameraServerApp:
    def __init__(self, root: tk.Tk, server_url: str = DEFAULT_SERVER_URL):
        self.root = root
        self.server_url = server_url.rstrip("/")
        self.root.title("Contrôle caméra / écran — Dashboard")
        self.root.geometry("1300x980")
        self.root.minsize(1040, 820)
        self.root.configure(bg=COLOR_BG)
        self._banner_width = 0

        self.camera_on = False
        self.screen_on = False
        self.devices: dict[str, dict] = {}
        self.selected_device_id: str | None = None
        self.log_queue: "queue.Queue" = queue.Queue()
        self._throttle_times: dict[str, float] = {}

        # Localisation : état du navigateur ouvert chez le client sélectionné.
        self.location_open = False
        self.last_location: dict | None = None
        self._location_marker = None

        self.panels: dict[str, StreamPanel] = {
            "camera": StreamPanel("camera", "📷  Caméra"),
            "screen": StreamPanel("screen", "🖥  Écran"),
        }

        self._build_ui()
        self._poll_logs()
        self._log(f"Serveur configuré : {self.server_url}")

        threading.Thread(target=self._poll_devices_loop, daemon=True).start()
        threading.Thread(target=self._poll_status_loop, daemon=True).start()
        threading.Thread(target=self._poll_location_loop, daemon=True).start()
        # Un thread de polling PAR flux : un flux lent à répondre ne doit
        # jamais retarder l'affichage (ni l'enregistrement) de l'autre.
        for key in self.panels:
            threading.Thread(target=self._poll_frame_loop, args=(key,), daemon=True).start()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Construction de l'interface
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self._apply_theme()

        # ---- Bandeau d'en-tête (image générée, ou banner.png si présent) ----
        self.banner = tk.Canvas(self.root, height=BANNER_HEIGHT, bg=COLOR_BG, highlightthickness=0)
        self.banner.pack(fill="x")
        self._banner_item = self.banner.create_image(0, 0, anchor="nw")
        self._logo_photo = ImageTk.PhotoImage(make_logo(56))
        self.banner.create_image(26, BANNER_HEIGHT // 2, image=self._logo_photo, anchor="w")
        self.banner.create_text(
            100, BANNER_HEIGHT // 2 - 13, anchor="w", text="Contrôle caméra / écran",
            fill="white", font=(FONT, 19, "bold"),
        )
        self.banner.create_text(
            101, BANNER_HEIGHT // 2 + 15, anchor="w",
            text="Tableau de bord à distance  ·  multi-appareils  ·  temps réel",
            fill="#b8c2e8", font=(FONT, 10),
        )
        try:
            self.root.iconphoto(True, self._logo_photo)
        except tk.TclError:
            pass

        self.status_var = tk.StringVar(value="● Connexion au serveur...")
        status_pill = tk.Frame(
            self.banner, bg=COLOR_BG, highlightthickness=1, highlightbackground=COLOR_BORDER
        )
        self.status_label = tk.Label(
            status_pill, textvariable=self.status_var, bg=COLOR_BG, fg=COLOR_KO,
            font=(FONT, 10, "bold"), padx=16, pady=7,
        )
        self.status_label.pack()
        self._status_window = self.banner.create_window(0, BANNER_HEIGHT // 2, window=status_pill, anchor="e")
        self.banner.bind("<Configure>", self._on_banner_resize)

        # ---- Sélecteur d'appareil ----
        device_bar = tk.Frame(self.root, bg=COLOR_BG)
        device_bar.pack(fill="x", padx=18, pady=(16, 0))
        # Bouton packé en premier (côté droit) : il garde toujours sa place, même si la fenêtre rétrécit
        self.btn_download = ttk.Button(
            device_bar,
            text="⬇  Télécharger le client (.exe)",
            command=self.download_client_installer,
            style="Primary.TButton",
            cursor="hand2",
        )
        self.btn_download.pack(side="right", padx=(14, 0))
        tk.Label(
            device_bar, text="APPAREIL", bg=COLOR_BG, fg=COLOR_MUTED, font=(FONT, 9, "bold")
        ).pack(side="left", padx=(0, 12))
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(
            device_bar, textvariable=self.device_var, state="readonly", width=45,
            style="Dark.TCombobox", font=(FONT, 10),
        )
        self.device_combo.pack(side="left", fill="x", expand=True, ipady=3)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)

        # ---- Les deux flux, affichés en permanence côte à côte ----
        streams_frame = tk.Frame(self.root, bg=COLOR_BG)
        streams_frame.pack(fill="both", expand=True, padx=18, pady=(14, 0))
        streams_frame.columnconfigure(0, weight=1, uniform="panel")
        streams_frame.columnconfigure(1, weight=1, uniform="panel")
        streams_frame.rowconfigure(0, weight=1)

        self._build_stream_panel(streams_frame, self.panels["camera"], column=0)
        self._build_stream_panel(streams_frame, self.panels["screen"], column=1)

        # ---- Localisation précise (carte + ouverture/fermeture du navigateur) ----
        self._build_location_panel(self.root)

        # ---- Barre du bas : serveur + téléchargement du client ----
        bottom_controls = tk.Frame(self.root, bg=COLOR_BG)
        bottom_controls.pack(fill="x", padx=18, pady=(12, 8))
        tk.Label(
            bottom_controls, text=f"Serveur  ·  {self.server_url}", bg=COLOR_BG, fg=COLOR_MUTED,
            font=(FONT, 9),
        ).pack(side="left")

        # ---- Journal ----
        log_card = tk.Frame(
            self.root, bg=COLOR_CARD, highlightthickness=1, highlightbackground=COLOR_BORDER
        )
        log_card.pack(fill="both", expand=False, padx=18, pady=(0, 18))
        tk.Label(
            log_card, text="JOURNAL DES ÉVÉNEMENTS", bg=COLOR_CARD, fg=COLOR_MUTED,
            font=(FONT, 9, "bold"), anchor="w",
        ).pack(fill="x", padx=14, pady=(10, 4))
        log_body = tk.Frame(log_card, bg=COLOR_CARD)
        log_body.pack(fill="both", expand=True, padx=(14, 6), pady=(0, 10))
        self.log_text = tk.Text(
            log_body, height=7, state="disabled", bg=COLOR_CARD, fg="#c5cce3",
            font=(FONT_MONO, 9), relief="flat", borderwidth=0, highlightthickness=0,
            insertbackground=COLOR_TEXT, selectbackground=COLOR_ACCENT, wrap="word", spacing1=2,
        )
        log_scroll = ttk.Scrollbar(log_body, orient="vertical", command=self.log_text.yview, style="Dark.Vertical.TScrollbar")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.tag_configure("time", foreground=COLOR_MUTED)
        self.log_text.tag_configure("err", foreground=COLOR_KO_HI)
        self.log_text.tag_configure("warn", foreground=COLOR_WARN)
        self.log_text.tag_configure("ok", foreground=COLOR_OK_HI)

    def _apply_theme(self) -> None:
        """Déclare tous les styles ttk (thème « clam » repeint aux couleurs de l'application)."""
        style = ttk.Style()
        style.theme_use("clam")

        def button(name: str, bg: str, hover: str, fg: str = "white") -> None:
            style.configure(
                name, background=bg, foreground=fg, bordercolor=bg, lightcolor=bg, darkcolor=bg,
                focuscolor=bg, font=(FONT, 9, "bold"), padding=(16, 8), relief="flat", borderwidth=0,
            )
            style.map(
                name,
                background=[("disabled", COLOR_DISABLED_BG), ("pressed", hover), ("active", hover)],
                foreground=[("disabled", COLOR_DISABLED_FG)],
                bordercolor=[("disabled", COLOR_DISABLED_BG), ("active", hover)],
                lightcolor=[("disabled", COLOR_DISABLED_BG), ("active", hover)],
                darkcolor=[("disabled", COLOR_DISABLED_BG), ("active", hover)],
            )

        button("Start.TButton", COLOR_OK, COLOR_OK_HI)
        button("Stop.TButton", COLOR_KO, COLOR_KO_HI)
        button("Record.TButton", COLOR_CARD_HI, COLOR_BORDER, fg=COLOR_TEXT)
        button("RecordOn.TButton", COLOR_REC, COLOR_KO_HI)
        button("Primary.TButton", COLOR_ACCENT, COLOR_ACCENT_HI)

        style.configure(
            "Dark.TCombobox", fieldbackground=COLOR_CARD, background=COLOR_CARD_HI, foreground=COLOR_TEXT,
            arrowcolor=COLOR_CAMERA, bordercolor=COLOR_BORDER, lightcolor=COLOR_CARD, darkcolor=COLOR_CARD,
            selectbackground=COLOR_CARD, selectforeground=COLOR_TEXT, padding=6,
        )
        style.map(
            "Dark.TCombobox",
            fieldbackground=[("readonly", COLOR_CARD)],
            foreground=[("readonly", COLOR_TEXT)],
            bordercolor=[("focus", COLOR_ACCENT)],
            arrowcolor=[("active", COLOR_ACCENT_HI)],
        )
        style.configure(
            "Dark.Vertical.TScrollbar", background=COLOR_CARD_HI, troughcolor=COLOR_CARD,
            bordercolor=COLOR_CARD, lightcolor=COLOR_CARD_HI, darkcolor=COLOR_CARD_HI,
            arrowcolor=COLOR_MUTED, relief="flat",
        )
        style.map("Dark.Vertical.TScrollbar", background=[("active", COLOR_BORDER)])

        # Liste déroulante du Combobox (widget Tk classique, hors ttk)
        self.root.option_add("*TCombobox*Listbox.background", COLOR_CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", COLOR_TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", COLOR_ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "white")
        self.root.option_add("*TCombobox*Listbox.font", (FONT, 10))

    def _on_banner_resize(self, event) -> None:
        if abs(event.width - self._banner_width) < 4:
            return
        self._banner_width = event.width
        self._banner_photo = ImageTk.PhotoImage(make_banner(event.width, BANNER_HEIGHT))
        self.banner.itemconfigure(self._banner_item, image=self._banner_photo)
        self.banner.coords(self._status_window, event.width - 26, BANNER_HEIGHT // 2)

    def _build_stream_panel(self, parent: tk.Frame, panel: StreamPanel, column: int) -> None:
        """Construit un panneau complet (vidéo + ses propres boutons) pour un flux."""
        accent = STREAM_ACCENTS.get(panel.key, COLOR_ACCENT)
        outer = tk.Frame(parent, bg=COLOR_CARD, highlightthickness=1, highlightbackground=COLOR_BORDER)
        outer.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 8 if column == 0 else 0))
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        # Liseré de couleur en haut de la carte : identifie le flux d'un coup d'œil
        tk.Frame(outer, bg=accent, height=3).grid(row=0, column=0, sticky="ew")

        title_bar = tk.Frame(outer, bg=COLOR_CARD)
        title_bar.grid(row=1, column=0, sticky="ew", padx=14, pady=(12, 10))
        panel.title_var = tk.StringVar(value=panel.title)
        tk.Label(
            title_bar, textvariable=panel.title_var, bg=COLOR_CARD, fg=COLOR_TEXT, font=(FONT, 12, "bold")
        ).pack(side="left")
        panel.rec_badge = tk.Label(
            title_bar, text="● REC", bg=COLOR_REC, fg="white", font=(FONT, 9, "bold"), padx=8, pady=1,
        )
        # Pas de .pack() ici : le badge n'apparaît que pendant l'enregistrement (voir _start/_stop_recording)
        panel.status_var = tk.StringVar(value="● Arrêté")
        panel.status_label = tk.Label(
            title_bar, textvariable=panel.status_var, bg=COLOR_CARD, fg=COLOR_KO, font=(FONT, 9, "bold"),
        )
        panel.status_label.pack(side="right")

        panel.placeholder = ImageTk.PhotoImage(make_placeholder(panel.key, accent, 150))
        panel.video_label = tk.Label(
            outer, bg=COLOR_VIDEO_BG, fg=COLOR_MUTED, text="Aucun flux vidéo",
            image=panel.placeholder, compound="top", font=(FONT, 11), pady=10,
        )
        panel.video_label.grid(row=2, column=0, sticky="nsew", padx=14)
        panel.video_label.bind(
            "<Configure>",
            lambda event, p=panel: setattr(p, "view_size", (max(event.width, 1), max(event.height, 1))),
        )

        controls = tk.Frame(outer, bg=COLOR_CARD)
        controls.grid(row=3, column=0, sticky="ew", padx=14, pady=14)
        panel.btn_start = ttk.Button(
            controls, text="▶  Démarrer", style="Start.TButton", cursor="hand2",
            command=lambda k=panel.key: self._send_command(k, "START"),
        )
        panel.btn_start.pack(side="left", padx=(0, 8))
        panel.btn_stop = ttk.Button(
            controls, text="■  Arrêter", style="Stop.TButton", cursor="hand2",
            command=lambda k=panel.key: self._send_command(k, "STOP"),
        )
        panel.btn_stop.pack(side="left", padx=(0, 8))
        panel.btn_record = ttk.Button(
            controls, text="⏺  Enregistrer", style="Record.TButton", cursor="hand2",
            command=lambda k=panel.key: self.toggle_recording(k),
        )
        panel.btn_record.pack(side="left")
        panel.btn_start["state"] = "disabled"
        panel.btn_stop["state"] = "disabled"

    def _show_placeholder(self, panel: StreamPanel) -> None:
        panel.video_label.configure(image=panel.placeholder, text="Aucun flux vidéo")
        panel.video_label.image = panel.placeholder

    def _build_location_panel(self, parent: tk.Frame) -> None:
        """Panneau « Localisation » : commandes pour ouvrir/fermer le
        navigateur chez le client sélectionné, coordonnées reçues, et une
        carte (tkintermapview, basée sur OpenStreetMap) centrée sur la
        dernière position précise transmise par CE navigateur."""
        outer = tk.Frame(parent, bg=COLOR_CARD, highlightthickness=1, highlightbackground=COLOR_BORDER)
        outer.pack(fill="x", padx=18, pady=(14, 0))

        tk.Frame(outer, bg=COLOR_WARN, height=3).pack(fill="x")

        header = tk.Frame(outer, bg=COLOR_CARD)
        header.pack(fill="x", padx=14, pady=(12, 10))
        tk.Label(
            header, text="📍  Localisation", bg=COLOR_CARD, fg=COLOR_TEXT, font=(FONT, 12, "bold")
        ).pack(side="left")
        self.location_status_var = tk.StringVar(value="● Fermée")
        self.location_status_label = tk.Label(
            header, textvariable=self.location_status_var, bg=COLOR_CARD, fg=COLOR_KO, font=(FONT, 9, "bold"),
        )
        self.location_status_label.pack(side="right")

        body = tk.Frame(outer, bg=COLOR_CARD)
        body.pack(fill="x", padx=14, pady=(0, 14))
        body.columnconfigure(1, weight=1)

        # ---- Colonne de gauche : contrôles + dernières coordonnées ----
        left = tk.Frame(body, bg=COLOR_CARD)
        left.grid(row=0, column=0, sticky="n", padx=(0, 14))

        self.btn_location_open = ttk.Button(
            left, text="🌐  Ouvrir navigateur", style="Start.TButton", cursor="hand2",
            command=lambda: self._send_location_command("OPEN"),
        )
        self.btn_location_open.pack(fill="x", pady=(0, 6))
        self.btn_location_close = ttk.Button(
            left, text="✖  Fermer navigateur", style="Stop.TButton", cursor="hand2",
            command=lambda: self._send_location_command("CLOSE"),
        )
        self.btn_location_close.pack(fill="x")
        self.btn_location_open["state"] = "disabled"
        self.btn_location_close["state"] = "disabled"

        self.location_coords_var = tk.StringVar(value="Aucune position reçue pour le moment.")
        tk.Label(
            left, textvariable=self.location_coords_var, bg=COLOR_CARD, fg=COLOR_MUTED,
            font=(FONT_MONO, 9), justify="left", wraplength=230, anchor="w",
        ).pack(fill="x", pady=(14, 0))

        # ---- Colonne de droite : la carte ----
        if _HAS_MAP:
            self.location_map = TkinterMapView(body, width=460, height=260, corner_radius=6)
            self.location_map.grid(row=0, column=1, sticky="nsew")
            self.location_map.set_position(DEFAULT_MAP_LAT, DEFAULT_MAP_LON)
            self.location_map.set_zoom(5)
        else:
            self.location_map = None
            tk.Label(
                body,
                text=(
                    "Carte indisponible : installez le paquet « tkintermapview »\n"
                    "(pip install tkintermapview) puis relancez le server-gui.\n"
                    "Les coordonnées restent affichées en texte à gauche."
                ),
                bg=COLOR_CARD, fg=COLOR_MUTED, font=(FONT, 9), justify="left", anchor="nw",
            ).grid(row=0, column=1, sticky="nw")

    def _send_location_command(self, command: str) -> None:
        """Envoie OPEN ou CLOSE : la GUI ne parle jamais directement au
        client, toujours via l'API du serveur (comme pour START/STOP)."""
        if self.selected_device_id is None:
            self._log("Aucun appareil sélectionné")
            return
        try:
            requests.post(
                f"{self.server_url}/api/location/command",
                json={"device_id": self.selected_device_id, "command": command},
                timeout=REQUEST_TIMEOUT,
            )
            self._log(f"Commande de localisation envoyée : {command}")
        except requests.RequestException as exc:
            self._log(f"Échec de l'envoi de la commande de localisation : {exc}")

    # ------------------------------------------------------------------
    # Boucles réseau (threads d'arrière-plan)
    # ------------------------------------------------------------------
    def _poll_devices_loop(self) -> None:
        while True:
            try:
                response = requests.get(f"{self.server_url}/api/devices", timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                devices = {d["device_id"]: d for d in response.json().get("devices", [])}
                self._sync_device_list(devices)
            except requests.RequestException:
                pass
            time.sleep(POLL_DEVICES_INTERVAL)

    def _poll_status_loop(self) -> None:
        was_connected = False
        while True:
            device_id = self.selected_device_id
            if device_id is None:
                time.sleep(POLL_STATUS_INTERVAL)
                continue
            try:
                response = requests.get(
                    f"{self.server_url}/api/status", params={"device_id": device_id}, timeout=REQUEST_TIMEOUT
                )
                response.raise_for_status()
                data = response.json()
                connected = data["client_connected"]
                self.camera_on = data["camera_on"]
                self.screen_on = data["screen_on"]
                self.panels["camera"].on = self.camera_on
                self.panels["screen"].on = self.screen_on

                if connected != was_connected:
                    self._update_status(connected)
                    was_connected = connected

                self._sync_buttons(connected)

            except requests.RequestException:
                if was_connected:
                    self._update_status(False)
                    was_connected = False

            time.sleep(POLL_STATUS_INTERVAL)

    def _poll_location_loop(self) -> None:
        """Interroge /api/location/latest pour l'appareil sélectionné :
        statut du navigateur (ouvert/fermé) + dernière position connue."""
        while True:
            device_id = self.selected_device_id
            if device_id is None:
                time.sleep(POLL_LOCATION_INTERVAL)
                continue
            try:
                response = requests.get(
                    f"{self.server_url}/api/location/latest",
                    params={"device_id": device_id},
                    timeout=REQUEST_TIMEOUT,
                )
                if response.status_code == 200:
                    self._update_location_display(response.json())
            except requests.RequestException:
                pass
            time.sleep(POLL_LOCATION_INTERVAL)

    def _poll_frame_loop(self, stream_key: str) -> None:
        """Boucle dédiée à UN SEUL flux (caméra ou écran) : les deux tournent
        en parallèle, donc un flux lent à répondre n'affecte jamais l'autre.

        Long-polling : on indique au serveur le numéro de la dernière image
        reçue (« after ») ; il ne répond que lorsqu'une image PLUS RÉCENTE
        existe, et immédiatement à son arrivée. Résultat : pas d'attente
        artificielle, et plus aucune image téléchargée deux fois."""
        panel = self.panels[stream_key]
        session = requests.Session()   # connexion persistante (keep-alive)
        last_device = None
        last_seq = None
        while True:
            device_id = self.selected_device_id
            if device_id is None:
                time.sleep(POLL_FRAME_INTERVAL)
                continue
            if device_id != last_device:
                last_device = device_id
                last_seq = None

            started = time.monotonic()
            long_poll_ok = False
            try:
                params = {"device_id": device_id, "stream": stream_key, "wait": FRAME_LONG_POLL_SECONDS}
                if last_seq is not None:
                    params["after"] = last_seq
                response = session.get(
                    f"{self.server_url}/api/frame/latest",
                    params=params,
                    timeout=REQUEST_TIMEOUT + FRAME_LONG_POLL_SECONDS,
                )
                if response.status_code == 200 and response.content:
                    seq_header = response.headers.get("X-Frame-Seq")
                    if seq_header is not None:
                        last_seq = int(seq_header)
                        long_poll_ok = True   # serveur à jour : pas de pause, on redemande tout de suite
                    self._handle_frame(panel, response.content)
                elif response.status_code == 204:
                    if panel.recording:
                        self._log_throttled(
                            f"waiting_frames_{stream_key}",
                            f"⏳ En attente d'images du flux « {panel.title} » pour l'enregistrement — "
                            "vérifiez qu'il est bien démarré sur l'appareil sélectionné (bouton Démarrer).",
                        )
                else:
                    self._log_throttled(
                        f"bad_status_{stream_key}",
                        f"Réponse inattendue du serveur pour « {panel.title} » ({response.status_code})",
                    )
            except requests.RequestException as exc:
                self._log_throttled(f"unreachable_{stream_key}", f"Flux « {panel.title} » injoignable : {exc}")
            except Exception as exc:
                # Ne JAMAIS laisser une erreur inattendue (décodage, écriture
                # vidéo...) arrêter cette boucle en silence.
                self._log_throttled(f"unexpected_{stream_key}", f"Erreur inattendue sur « {panel.title} » : {exc}")

            if not long_poll_ok:
                # Erreur, 204 immédiat ou ancien serveur sans long-polling : on évite de tourner à vide.
                elapsed = time.monotonic() - started
                if elapsed < POLL_FRAME_INTERVAL:
                    time.sleep(POLL_FRAME_INTERVAL - elapsed)

    def _log_throttled(self, key: str, message: str, interval: float = LOG_THROTTLE_SECONDS) -> None:
        """Comme _log, mais n'écrit le MÊME message (identifié par key) qu'une
        fois toutes les `interval` secondes, pour éviter de noyer le journal
        quand un problème persiste pendant plusieurs secondes d'affilée."""
        now = time.time()
        last = self._throttle_times.get(key, 0.0)
        if now - last >= interval:
            self._throttle_times[key] = now
            self._log(message)

    # ------------------------------------------------------------------
    # Sélection de l'appareil
    # ------------------------------------------------------------------
    def _sync_device_list(self, devices: dict[str, dict]) -> None:
        def apply():
            self.devices = devices
            labels = [
                f"{d['name']} — {'connecté' if d['connected'] else 'hors ligne'} ({device_id[:8]})"
                for device_id, d in devices.items()
            ]
            self.device_combo["values"] = labels

            if self.selected_device_id is None and devices:
                self._select_device(next(iter(devices)))
            elif self.selected_device_id not in devices and devices:
                self._select_device(next(iter(devices)))
            elif not devices:
                self.selected_device_id = None
                self.device_var.set("")
                self.status_var.set("● Aucun appareil connu")
                self.status_label.configure(foreground=COLOR_KO)

        self.root.after(0, apply)

    def _select_device(self, device_id: str) -> None:
        if device_id == self.selected_device_id:
            return
        # On change d'appareil : les enregistrements en cours appartiennent à
        # l'ancien appareil, donc on les arrête proprement plutôt que de
        # continuer à y écrire les images d'un autre appareil.
        for key, panel in self.panels.items():
            if panel.recording:
                self._stop_recording(key)
                self._log(f"Enregistrement « {panel.title} » arrêté (changement d'appareil)")

        self.selected_device_id = device_id
        device = self.devices.get(device_id, {})
        label = f"{device.get('name', '?')} — ({device_id[:8]})"
        self.device_var.set(label)
        for panel in self.panels.values():
            self._show_placeholder(panel)

        # La position affichée appartient à l'ANCIEN appareil : on l'efface
        # en attendant le prochain polling (/api/location/latest) du nouveau.
        self.last_location = None
        self.location_coords_var.set("Chargement de la position...")
        self.location_status_var.set("● Statut inconnu")
        self.location_status_label.configure(foreground=COLOR_MUTED)

        self._log(f"Appareil sélectionné : {label}")

    def _on_device_selected(self, _event) -> None:
        index = self.device_combo.current()
        device_ids = list(self.devices.keys())
        if 0 <= index < len(device_ids):
            self._select_device(device_ids[index])

    # ------------------------------------------------------------------
    # Commandes envoyées au serveur (pour l'appareil sélectionné)
    # ------------------------------------------------------------------
    def _send_command(self, stream: str, command: str) -> None:
        if self.selected_device_id is None:
            self._log("Aucun appareil sélectionné")
            return
        try:
            requests.post(
                f"{self.server_url}/api/command",
                json={"device_id": self.selected_device_id, "stream": stream, "command": command},
                timeout=REQUEST_TIMEOUT,
            )
            self._log(f"Commande envoyée : {stream} → {command}")
        except requests.RequestException as exc:
            self._log(f"Échec de l'envoi de la commande : {exc}")

    # ------------------------------------------------------------------
    # Téléchargement de l'installateur Windows du client
    # ------------------------------------------------------------------
    def download_client_installer(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Enregistrer l'installateur du client",
            defaultextension=".exe",
            initialfile="CameraClient-Setup.exe",
            filetypes=[("Exécutable Windows", "*.exe"), ("Tous les fichiers", "*.*")],
        )
        if not path:
            return
        self._log(f"Téléchargement du client depuis {self.server_url}/download/windows ...")
        threading.Thread(target=self._do_download_client, args=(path,), daemon=True).start()

    def _do_download_client(self, path: str) -> None:
        try:
            response = requests.get(f"{self.server_url}/download/windows", timeout=DOWNLOAD_TIMEOUT)
            if response.status_code == 404:
                self._log("L'installateur Windows du client n'est pas encore publié sur le serveur")
                return
            response.raise_for_status()
            with open(path, "wb") as f:
                f.write(response.content)
            self._log(f"Client téléchargé : {path}")
        except requests.RequestException as exc:
            self._log(f"Échec du téléchargement du client : {exc}")
        except OSError as exc:
            self._log(f"Impossible d'écrire le fichier : {exc}")

    # ------------------------------------------------------------------
    # Enregistrement vidéo — indépendant pour chaque flux
    # ------------------------------------------------------------------
    def toggle_recording(self, stream_key: str) -> None:
        panel = self.panels[stream_key]
        if panel.recording:
            self._stop_recording(stream_key)
        else:
            self._start_recording(stream_key)

    def _start_recording(self, stream_key: str) -> None:
        panel = self.panels[stream_key]
        if self.selected_device_id is None:
            self._log("Aucun appareil sélectionné : impossible d'enregistrer")
            return

        if not panel.on:
            self._log(
                f"⚠ Le flux « {panel.title} » n'est pas démarré sur cet appareil : "
                "cliquez d'abord sur son bouton « Démarrer », sinon l'enregistrement restera vide "
                "tant qu'aucune image n'arrive."
            )

        default_name = f"{stream_key}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
        path = filedialog.asksaveasfilename(
            title=f"Enregistrer la vidéo — {panel.title}",
            defaultextension=".mp4",
            initialfile=default_name,
            filetypes=[("Vidéo MP4", "*.mp4"), ("Vidéo AVI", "*.avi"), ("Tous les fichiers", "*.*")],
        )
        if not path:
            return
        fps = RECORD_FPS_SCREEN if stream_key == "screen" else RECORD_FPS_CAMERA
        panel.last_jpeg = None
        panel.recorder = VideoRecorder(
            path, fps, log=self._log, on_fatal=lambda k=stream_key: self.root.after(0, self._stop_recording, k)
        )
        panel.recorder.start()
        panel.recording = True
        self._throttle_times.pop(f"waiting_frames_{stream_key}", None)  # on veut revoir l'avertissement si ça persiste
        panel.btn_record.configure(text="⏹  Arrêter l'enreg.", style="RecordOn.TButton")
        panel.rec_badge.pack(side="left", padx=(10, 0))
        self._log(f"Enregistrement démarré ({panel.title}) : {path}")

    def _stop_recording(self, stream_key: str) -> None:
        panel = self.panels[stream_key]
        recorder = panel.recorder
        panel.recording = False
        panel.recorder = None
        panel.btn_record.configure(text="⏺  Enregistrer", style="Record.TButton")
        panel.rec_badge.pack_forget()
        if recorder is None:
            return

        report = recorder.stop()
        if recorder.failed:
            return  # le message d'erreur a déjà été journalisé
        if report.frames_written == 0:
            self._log(
                f"⚠ Aucune image n'a été écrite dans {report.path} — le flux « {panel.title} » n'a jamais "
                "envoyé d'image pendant l'enregistrement (voir les messages « En attente d'images... » : "
                "le flux n'était probablement pas démarré sur l'appareil, ou celui-ci s'est déconnecté)."
            )
            return
        if not report.verified_ok:
            self._log(
                f"❌ Le fichier {report.path} a été créé mais n'est pas lisible "
                f"({report.frames_written} images écrites). Réessayez en .avi."
            )
            return
        self._log(
            f"Enregistrement terminé ({panel.title}) : {report.path} — {report.duration:.0f} s, "
            f"{report.frames_written} images écrites ({report.frames_received} images distinctes reçues)"
        )
        if report.max_std < BLACK_FRAME_STD:
            self._log(
                f"⚠ Toutes les images reçues sur « {panel.title} » sont noires/uniformes : la vidéo est "
                "valide mais NOIRE. Voir le journal du client (~/.local/share/camera-client/client.log)."
            )

    def _write_recording_frame(self, panel: StreamPanel, jpeg_bytes: bytes) -> None:
        recorder = panel.recorder
        if recorder is None:
            return
        if jpeg_bytes == panel.last_jpeg:
            return   # même image que le tour précédent : le recorder la répète déjà tout seul
        panel.last_jpeg = jpeg_bytes
        array = np.frombuffer(jpeg_bytes, dtype=np.uint8)
        image = cv2.imdecode(array, cv2.IMREAD_COLOR)
        if image is None:
            self._log_throttled(f"decode_failed_{panel.key}", f"Image reçue invalide sur « {panel.title} » : ignorée")
            return
        recorder.push(image)

    # ------------------------------------------------------------------
    # Mise à jour de l'interface (toujours via root.after, thread principal)
    # ------------------------------------------------------------------
    def _handle_frame(self, panel: StreamPanel, jpeg_bytes: bytes) -> None:
        # L'affichage d'abord : le décodage pour l'enregistrement ne doit pas le retarder.
        self._display_frame(panel, jpeg_bytes)
        if panel.recording:
            self._write_recording_frame(panel, jpeg_bytes)

    def _update_status(self, connected: bool) -> None:
        def apply():
            if connected:
                self.status_var.set("● Client connecté")
                self.status_label.configure(foreground=COLOR_OK)
                self._log("Client connecté")
            else:
                self.status_var.set("● Client déconnecté")
                self.status_label.configure(foreground=COLOR_KO)
                self._log("Client déconnecté")
        self.root.after(0, apply)

    def _sync_buttons(self, connected: bool) -> None:
        def apply():
            for key, panel in self.panels.items():
                active = getattr(self, f"{key}_on")
                panel.btn_start["state"] = "normal" if connected and not active else "disabled"
                panel.btn_stop["state"] = "normal" if connected and active else "disabled"
                panel.status_var.set("● Actif" if connected and active else "● Arrêté")
                panel.status_label.configure(foreground=COLOR_OK if connected and active else COLOR_KO)
            self.btn_location_open["state"] = "normal" if connected and not self.location_open else "disabled"
            self.btn_location_close["state"] = "normal" if connected and self.location_open else "disabled"
        self.root.after(0, apply)

    def _update_location_display(self, data: dict) -> None:
        """Met à jour le statut, les coordonnées en texte et le marqueur sur
        la carte à partir de la réponse de /api/location/latest."""
        def apply():
            self.location_open = bool(data.get("browser_open"))
            self.location_status_var.set("● Ouverte" if self.location_open else "● Fermée")
            self.location_status_label.configure(
                foreground=COLOR_OK if self.location_open else COLOR_KO
            )

            latest = data.get("latest")
            if latest:
                self.last_location = latest
                lat, lon = latest["lat"], latest["lon"]
                accuracy = latest.get("accuracy")
                ts = latest.get("ts")
                when = datetime.fromtimestamp(ts / 1000).strftime("%H:%M:%S") if ts else "?"
                acc_text = f"± {accuracy:.0f} m" if isinstance(accuracy, (int, float)) else "précision inconnue"
                self.location_coords_var.set(
                    f"{lat:.6f}, {lon:.6f}\n{acc_text}  ·  reçue à {when}"
                )
                if self.location_map is not None:
                    if self._location_marker is None:
                        self._location_marker = self.location_map.set_marker(lat, lon, text="Appareil")
                        self.location_map.set_zoom(16)
                    else:
                        self._location_marker.set_position(lat, lon)
                    self.location_map.set_position(lat, lon)
        self.root.after(0, apply)

    def _display_frame(self, panel: StreamPanel, jpeg_bytes: bytes) -> None:
        """Décode et redimensionne DANS LE THREAD DE RÉCEPTION (le thread Tk ne
        fait plus que l'affichage). Une seule image en attente par panneau :
        si l'interface est en retard, l'image périmée est simplement remplacée
        au lieu de s'empiler dans la file de Tk (cause de latence croissante)."""
        max_w, max_h = panel.view_size
        image = Image.open(io.BytesIO(jpeg_bytes))
        image.thumbnail((max_w, max_h), Image.BILINEAR)   # utilise aussi le décodage JPEG réduit
        image.load()

        with panel.display_lock:
            panel.pending_image = image
            already_scheduled = panel.display_scheduled
            panel.display_scheduled = True
        if not already_scheduled:
            self.root.after(0, self._apply_pending_image, panel)

    def _apply_pending_image(self, panel: StreamPanel) -> None:
        with panel.display_lock:
            image = panel.pending_image
            panel.pending_image = None
            panel.display_scheduled = False
        if image is None:
            return
        photo = ImageTk.PhotoImage(image)
        panel.video_label.configure(image=photo, text="")
        panel.video_label.image = photo  # garder une référence

    def _poll_logs(self) -> None:
        while not self.log_queue.empty():
            message = self.log_queue.get_nowait()
            stamp, body = message[:10], message[11:]   # "[HH:MM:SS]" puis le texte
            lowered = body.lower()
            if "❌" in body or "échec" in lowered or "erreur" in lowered or "injoignable" in lowered or "impossible" in lowered:
                tag = "err"
            elif "⚠" in body or "déconnecté" in lowered or "arrêté" in lowered or "⏳" in body:
                tag = "warn"
            elif "connecté" in lowered or "terminé" in lowered or "téléchargé" in lowered or "démarré" in lowered:
                tag = "ok"
            else:
                tag = ""
            self.log_text["state"] = "normal"
            self.log_text.insert("end", stamp + "  ", "time")
            self.log_text.insert("end", body + "\n", tag)
            self.log_text.see("end")
            self.log_text["state"] = "disabled"
        self.root.after(200, self._poll_logs)

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{timestamp}] {message}")

    def _on_close(self) -> None:
        for key in self.panels:
            if self.panels[key].recording:
                self._stop_recording(key)
        self.root.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interface de contrôle caméra / écran")
    parser.add_argument("--server", help="URL du serveur (ex. https://mon-serveur.com)")
    parser.add_argument("--save", action="store_true", help="mémorise --server pour les prochains lancements")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server_url = resolve_server_url(args.server)
    if args.save:
        if not args.server:
            print("--save nécessite --server")
            return
        save_config(server_url)

    root = tk.Tk()
    CameraServerApp(root, server_url)
    root.mainloop()


if __name__ == "__main__":
    main()