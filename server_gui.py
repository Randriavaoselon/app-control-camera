"""
Interface graphique de contrôle — consomme l'API HTTP du serveur.

Cette GUI ne parle jamais directement aux clients : elle passe toujours
par l'API du serveur. Les flux caméra et écran s'affichent EN PERMANENCE
côte à côte, chacun avec ses propres contrôles (démarrer/arrêter/
enregistrer) : ce sont deux panneaux totalement indépendants l'un de
l'autre, y compris pour l'enregistrement.

Contrôle souris : quand le flux écran est actif, le client remonte la
position de sa souris (affichée par un curseur rouge sur l'image) et un
clic sur l'image écran est converti en coordonnées écran réelles puis
envoyé au serveur, qui le fait exécuter par le client.

Enregistrement : un thread dédié par flux (VideoRecorder) écrit les images
à cadence FIXE dans le temps réel (la dernière image reçue est répétée
tant qu'aucune nouvelle n'arrive), toutes à la même taille, puis le
fichier est relu et vérifié à l'arrêt pour signaler un problème (vidéo
vide, écran noir...).

CMD Commandes : une fenêtre GUI par appareil (une par device_id), liée à
la console persistante du client. Fermer la fenêtre GUI NE ferme PAS la
console distante — elle continue de tourner côté client. Rouvrir la
fenêtre réaffiche le buffer courant.

Chat : une fenêtre GUI par appareil, miroir de la conversation avec la
console visible du client. Seul l'opérateur peut ouvrir/fermer la console
distante via les boutons dédiés.
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
    from tkintermapview import TkinterMapView
    _HAS_MAP = True
except ImportError:
    TkinterMapView = None
    _HAS_MAP = False

DEFAULT_SERVER_URL = "http://127.0.0.1:5050"
POLL_DEVICES_INTERVAL = 2.0
POLL_STATUS_INTERVAL = 1.0
POLL_LOCATION_INTERVAL = 2.0
POLL_MOUSE_INTERVAL = 0.1
DEFAULT_MAP_LAT = -18.8792
DEFAULT_MAP_LON = 47.5079
MAP_TILE_SERVER = "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}"
MAP_TILE_MAX_ZOOM = 16
POLL_FRAME_INTERVAL = 0.03
FRAME_LONG_POLL_SECONDS = 0.0
REQUEST_TIMEOUT = 5
DOWNLOAD_TIMEOUT = 30
RECORD_FPS_CAMERA = 10
RECORD_FPS_SCREEN = 5
MAX_RECORD_WIDTH = 1920
STALL_WARN_SECONDS = 10.0
BLACK_FRAME_STD = 2.0
LOG_THROTTLE_SECONDS = 5.0
MOUSE_CURSOR_COLOR = "#ff3b30"
MOUSE_CURSOR_RADIUS = 6

# Shell / chat
SHELL_OUTPUT_POLL_INTERVAL_MS = 300
SHELL_CHAT_POLL_INTERVAL_MS = 800

# --- Thème « Midnight » ---
COLOR_BG = "#0b1020"
COLOR_CARD = "#141b30"
COLOR_CARD_HI = "#1b2440"
COLOR_BORDER = "#263150"
COLOR_VIDEO_BG = "#070a14"
COLOR_TEXT = "#e8ecf8"
COLOR_MUTED = "#8b95b5"
COLOR_ACCENT = "#4f8cff"
COLOR_ACCENT_HI = "#6aa0ff"
COLOR_CAMERA = "#22d3ee"
COLOR_SCREEN = "#a78bfa"
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

FOURCC_CANDIDATES_MP4 = ["mp4v", "avc1", "X264"]
FOURCC_CANDIDATES_AVI = ["XVID", "MJPG"]


# ---------------------------------------------------------------------
# Configuration persistée
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
# Images générées par le code
# ---------------------------------------------------------------------

def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _custom_banner_path() -> Path | None:
    for folder in (Path(__file__).resolve().parent, CONFIG_DIR):
        for name in ("banner.png", "banner.jpg", "banner.jpeg"):
            candidate = folder / name
            if candidate.exists():
                return candidate
    return None


def make_banner(width: int, height: int) -> Image.Image:
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
            return Image.blend(photo, veil, 0.62)
        except Exception:
            pass

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
    glow = glow.filter(ImageFilter.GaussianBlur(28))
    image = Image.alpha_composite(image, glow)

    line = ImageDraw.Draw(image)
    line.rectangle((0, height - 2, width, height), fill=(*_hex_to_rgb(COLOR_ACCENT), 120))
    return image.convert("RGB")


def make_logo(size: int = 56) -> Image.Image:
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
    """Écrit dans un fichier vidéo la DERNIÈRE image reçue, à cadence fixe."""

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
        self._size: tuple[int, int] | None = None
        self._writer: cv2.VideoWriter | None = None
        self._codec: str | None = None
        self._started_at: float | None = None
        self.frames_received = 0
        self.frames_written = 0
        self.max_std = 0.0
        self.failed = False

    def push(self, image: np.ndarray) -> None:
        std = float(image[::8, ::8].std())
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

    def _fit_size(self, image: np.ndarray) -> tuple[int, int]:
        height, width = image.shape[:2]
        if width > MAX_RECORD_WIDTH:
            height = int(height * MAX_RECORD_WIDTH / width)
            width = MAX_RECORD_WIDTH
        return max(2, width - width % 2), max(2, height - height % 2)

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
            if now - next_tick > 1.0:
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
    """Regroupe tout ce qui concerne UN flux (caméra ou écran)."""

    def __init__(self, key: str, title: str):
        self.key = key
        self.title = title
        self.on = False
        self.recording = False
        self.recorder: VideoRecorder | None = None
        self.last_jpeg: bytes | None = None
        self.view_size: tuple[int, int] = (420, 380)
        self.pending_image = None
        self.display_scheduled = False
        self.display_lock = threading.Lock()
        self.video_label: tk.Label | None = None
        self.title_var: tk.StringVar | None = None
        self.status_var: tk.StringVar | None = None
        self.btn_start: ttk.Button | None = None
        self.btn_stop: ttk.Button | None = None
        self.btn_record: ttk.Button | None = None
        self.rec_badge: tk.Label | None = None
        self.status_label: tk.Label | None = None
        self.placeholder = None


class CameraServerApp:
    def __init__(self, root: tk.Tk, server_url: str = DEFAULT_SERVER_URL):
        self.root = root
        self.server_url = server_url.rstrip("/")
        self.root.title("Contrôle caméra / écran — Dashboard")
        screen_w, screen_h = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        win_w, win_h = min(1440, screen_w - 60), min(860, screen_h - 90)
        self.root.geometry(f"{win_w}x{win_h}+{max((screen_w - win_w) // 2, 0)}+{max((screen_h - win_h) // 3, 0)}")
        self.root.minsize(min(1040, win_w), min(660, win_h))
        self.root.configure(bg=COLOR_BG)

        self.camera_on = False
        self.screen_on = False
        self.devices: dict[str, dict] = {}
        self.selected_device_id: str | None = None
        self.log_queue: "queue.Queue" = queue.Queue()
        self._throttle_times: dict[str, float] = {}

        self.location_open = False
        self.last_location: dict | None = None
        self._location_marker = None

        self.mouse_position: dict | None = None

        self.panels: dict[str, StreamPanel] = {
            "camera": StreamPanel("camera", "📷  Caméra"),
            "screen": StreamPanel("screen", "🖥  Écran"),
        }

        # Fenêtres CMD/Chat, une par device_id
        self._cmd_windows: dict[str, tk.Toplevel] = {}
        self._chat_windows: dict[str, tk.Toplevel] = {}

        self._build_ui()
        self._poll_logs()
        self._log(f"Serveur configuré : {self.server_url}")

        threading.Thread(target=self._poll_devices_loop, daemon=True).start()
        threading.Thread(target=self._poll_status_loop, daemon=True).start()
        threading.Thread(target=self._poll_location_loop, daemon=True).start()
        threading.Thread(target=self._poll_mouse_loop, daemon=True).start()
        for key in self.panels:
            threading.Thread(target=self._poll_frame_loop, args=(key,), daemon=True).start()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------
    # Construction de l'interface
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        self._apply_theme()

        self._logo_photo = ImageTk.PhotoImage(make_logo(34))
        try:
            self.root.iconphoto(True, self._logo_photo)
        except tk.TclError:
            pass

        # ---- En-tête ----
        header = tk.Frame(self.root, bg=COLOR_CARD)
        header.pack(fill="x")
        tk.Frame(header, bg=COLOR_ACCENT, height=2).pack(fill="x")
        header_in = tk.Frame(header, bg=COLOR_CARD)
        header_in.pack(fill="x", padx=16, pady=9)
        tk.Label(header_in, image=self._logo_photo, bg=COLOR_CARD).pack(side="left")
        titles = tk.Frame(header_in, bg=COLOR_CARD)
        titles.pack(side="left", padx=(12, 0))
        tk.Label(
            titles, text="Contrôle caméra / écran", bg=COLOR_CARD, fg=COLOR_TEXT, font=(FONT, 13, "bold")
        ).pack(anchor="w")
        tk.Label(
            titles, text="Tableau de bord à distance  ·  multi-appareils  ·  temps réel",
            bg=COLOR_CARD, fg=COLOR_MUTED, font=(FONT, 8),
        ).pack(anchor="w")

        self.btn_download = ttk.Button(
            header_in, text="⬇  Télécharger le client (.exe)", command=self.download_client_installer,
            style="Primary.TButton", cursor="hand2",
        )
        self.btn_download.pack(side="right")
        self.status_var = tk.StringVar(value="● Connexion au serveur...")
        status_pill = tk.Frame(header_in, bg=COLOR_BG, highlightthickness=1, highlightbackground=COLOR_BORDER)
        status_pill.pack(side="right", padx=(0, 14))
        self.status_label = tk.Label(
            status_pill, textvariable=self.status_var, bg=COLOR_BG, fg=COLOR_KO,
            font=(FONT, 9, "bold"), padx=14, pady=5,
        )
        self.status_label.pack()
        tk.Frame(header, bg=COLOR_BORDER, height=1).pack(fill="x")

        # ---- Barre d'outils : sélecteur d'appareil ----
        toolbar = tk.Frame(self.root, bg=COLOR_BG)
        toolbar.pack(fill="x", padx=14, pady=(10, 8))
        tk.Label(
            toolbar, text=f"Serveur  ·  {self.server_url}", bg=COLOR_BG, fg=COLOR_MUTED, font=(FONT, 9),
        ).pack(side="right", padx=(14, 0))
        tk.Label(
            toolbar, text="APPAREIL", bg=COLOR_BG, fg=COLOR_MUTED, font=(FONT, 9, "bold")
        ).pack(side="left", padx=(0, 10))
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(
            toolbar, textvariable=self.device_var, state="readonly", width=45,
            style="Dark.TCombobox", font=(FONT, 10),
        )
        self.device_combo.pack(side="left", fill="x", expand=True, ipady=2)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)

        # ---- Barre shell/chat ----
        shell_toolbar = tk.Frame(self.root, bg=COLOR_BG)
        shell_toolbar.pack(fill="x", padx=14, pady=(0, 8))
        tk.Label(
            shell_toolbar, text="OUTILS DISTANTS", bg=COLOR_BG, fg=COLOR_MUTED, font=(FONT, 9, "bold")
        ).pack(side="left", padx=(0, 10))
        self.btn_open_cmd = ttk.Button(
            shell_toolbar, text="⌨  CMD distant", style="Primary.TButton", cursor="hand2",
            command=self._open_cmd_window_for_selected,
        )
        self.btn_open_cmd.pack(side="left", padx=(0, 8))
        self.btn_open_chat = ttk.Button(
            shell_toolbar, text="💬  Chat distant", style="Primary.TButton", cursor="hand2",
            command=self._open_chat_window_for_selected,
        )
        self.btn_open_chat.pack(side="left")

        # ---- Zone principale ----
        sash = dict(bg=COLOR_BG, sashwidth=8, sashrelief="flat", borderwidth=0, sashpad=0, opaqueresize=True)
        main = tk.PanedWindow(self.root, orient="horizontal", **sash)
        main.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        left = tk.PanedWindow(main, orient="vertical", **sash)
        main.add(left, minsize=620, stretch="always")

        streams_frame = tk.Frame(left, bg=COLOR_BG)
        streams_frame.columnconfigure(0, weight=1, uniform="panel")
        streams_frame.columnconfigure(1, weight=1, uniform="panel")
        streams_frame.rowconfigure(0, weight=1)
        self._build_stream_panel(streams_frame, self.panels["camera"], column=0)
        self._build_stream_panel(streams_frame, self.panels["screen"], column=1)
        left.add(streams_frame, minsize=280, stretch="always")

        log_card = self._build_log_panel(left)
        left.add(log_card, minsize=90, height=170, stretch="never")

        location_card = self._build_location_panel(main)
        main.add(location_card, minsize=300, width=420, stretch="never")

    def _build_log_panel(self, parent: tk.Misc) -> tk.Frame:
        log_card = tk.Frame(parent, bg=COLOR_CARD, highlightthickness=1, highlightbackground=COLOR_BORDER)
        header = tk.Frame(log_card, bg=COLOR_CARD)
        header.pack(fill="x", padx=14, pady=(8, 2))
        tk.Label(
            header, text="JOURNAL DES ÉVÉNEMENTS", bg=COLOR_CARD, fg=COLOR_MUTED, font=(FONT, 9, "bold"),
        ).pack(side="left")
        ttk.Button(
            header, text="Effacer", style="Ghost.TButton", cursor="hand2", command=self._clear_log,
        ).pack(side="right")
        log_body = tk.Frame(log_card, bg=COLOR_CARD)
        log_body.pack(fill="both", expand=True, padx=(14, 6), pady=(0, 8))
        self.log_text = tk.Text(
            log_body, height=3, state="disabled", bg=COLOR_CARD, fg="#c5cce3",
            font=(FONT_MONO, 9), relief="flat", borderwidth=0, highlightthickness=0,
            insertbackground=COLOR_TEXT, selectbackground=COLOR_ACCENT, wrap="word", spacing1=2,
        )
        log_scroll = ttk.Scrollbar(
            log_body, orient="vertical", command=self.log_text.yview, style="Dark.Vertical.TScrollbar"
        )
        self.log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_text.tag_configure("time", foreground=COLOR_MUTED)
        self.log_text.tag_configure("err", foreground=COLOR_KO_HI)
        self.log_text.tag_configure("warn", foreground=COLOR_WARN)
        self.log_text.tag_configure("ok", foreground=COLOR_OK_HI)
        return log_card

    def _clear_log(self) -> None:
        self.log_text["state"] = "normal"
        self.log_text.delete("1.0", "end")
        self.log_text["state"] = "disabled"

    def _apply_theme(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")

        def button(name: str, bg: str, hover: str, fg: str = "white") -> None:
            style.configure(
                name, background=bg, foreground=fg, bordercolor=bg, lightcolor=bg, darkcolor=bg,
                focuscolor=bg, font=(FONT, 9, "bold"), padding=(14, 7), relief="flat", borderwidth=0,
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
        button("Ghost.TButton", COLOR_CARD, COLOR_CARD_HI, fg=COLOR_MUTED)
        style.configure("Ghost.TButton", padding=(8, 2), font=(FONT, 8))

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

        self.root.option_add("*TCombobox*Listbox.background", COLOR_CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", COLOR_TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", COLOR_ACCENT)
        self.root.option_add("*TCombobox*Listbox.selectForeground", "white")
        self.root.option_add("*TCombobox*Listbox.font", (FONT, 10))

    def _build_stream_panel(self, parent: tk.Frame, panel: StreamPanel, column: int) -> None:
        accent = STREAM_ACCENTS.get(panel.key, COLOR_ACCENT)
        outer = tk.Frame(parent, bg=COLOR_CARD, highlightthickness=1, highlightbackground=COLOR_BORDER)
        outer.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 5, 5 if column == 0 else 0))
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        tk.Frame(outer, bg=accent, height=3).grid(row=0, column=0, sticky="ew")

        title_bar = tk.Frame(outer, bg=COLOR_CARD)
        title_bar.grid(row=1, column=0, sticky="ew", padx=12, pady=(9, 7))
        panel.title_var = tk.StringVar(value=panel.title)
        tk.Label(
            title_bar, textvariable=panel.title_var, bg=COLOR_CARD, fg=COLOR_TEXT, font=(FONT, 12, "bold")
        ).pack(side="left")
        panel.rec_badge = tk.Label(
            title_bar, text="● REC", bg=COLOR_REC, fg="white", font=(FONT, 9, "bold"), padx=8, pady=1,
        )
        panel.status_var = tk.StringVar(value="● Arrêté")
        panel.status_label = tk.Label(
            title_bar, textvariable=panel.status_var, bg=COLOR_CARD, fg=COLOR_KO, font=(FONT, 9, "bold"),
        )
        panel.status_label.pack(side="right")

        panel.placeholder = ImageTk.PhotoImage(make_placeholder(panel.key, accent, 96))
        holder = tk.Frame(outer, bg=COLOR_VIDEO_BG, width=240, height=140)
        holder.pack_propagate(False)
        holder.grid(row=2, column=0, sticky="nsew", padx=12)
        panel.video_label = tk.Label(
            holder, bg=COLOR_VIDEO_BG, fg=COLOR_MUTED, text="Aucun flux vidéo",
            image=panel.placeholder, compound="top", font=(FONT, 10), pady=6,
        )
        panel.video_label.pack(fill="both", expand=True)
        panel.video_label.bind(
            "<Configure>",
            lambda event, p=panel: setattr(p, "view_size", (max(event.width, 1), max(event.height, 1))),
        )
        if panel.key == "screen":
            panel.video_label.configure(cursor="crosshair")
            panel.video_label.bind("<Button-1>", lambda event, p=panel: self._on_screen_click(p, event))

        controls = tk.Frame(outer, bg=COLOR_CARD)
        controls.grid(row=3, column=0, sticky="ew", padx=12, pady=10)
        for col in range(3):
            controls.columnconfigure(col, weight=1, uniform="ctl")
        panel.btn_start = ttk.Button(
            controls, text="▶  Démarrer", style="Start.TButton", cursor="hand2",
            command=lambda k=panel.key: self._send_command(k, "START"),
        )
        panel.btn_start.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        panel.btn_stop = ttk.Button(
            controls, text="■  Arrêter", style="Stop.TButton", cursor="hand2",
            command=lambda k=panel.key: self._send_command(k, "STOP"),
        )
        panel.btn_stop.grid(row=0, column=1, sticky="ew", padx=(0, 6))
        panel.btn_record = ttk.Button(
            controls, text="⏺  Enregistrer", style="Record.TButton", cursor="hand2",
            command=lambda k=panel.key: self.toggle_recording(k),
        )
        panel.btn_record.grid(row=0, column=2, sticky="ew")
        panel.btn_start["state"] = "disabled"
        panel.btn_stop["state"] = "disabled"

    def _show_placeholder(self, panel: StreamPanel) -> None:
        panel.video_label.configure(image=panel.placeholder, text="Aucun flux vidéo")
        panel.video_label.image = panel.placeholder

    def _build_location_panel(self, parent: tk.Misc) -> tk.Frame:
        outer = tk.Frame(parent, bg=COLOR_CARD, highlightthickness=1, highlightbackground=COLOR_BORDER)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)

        tk.Frame(outer, bg=COLOR_WARN, height=3).grid(row=0, column=0, sticky="ew")

        header = tk.Frame(outer, bg=COLOR_CARD)
        header.grid(row=1, column=0, sticky="ew", padx=12, pady=(9, 7))
        tk.Label(
            header, text="📍  Localisation", bg=COLOR_CARD, fg=COLOR_TEXT, font=(FONT, 12, "bold")
        ).pack(side="left")
        self.location_status_var = tk.StringVar(value="● Fermée")
        self.location_status_label = tk.Label(
            header, textvariable=self.location_status_var, bg=COLOR_CARD, fg=COLOR_KO, font=(FONT, 9, "bold"),
        )
        self.location_status_label.pack(side="right")

        buttons = tk.Frame(outer, bg=COLOR_CARD)
        buttons.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))
        buttons.columnconfigure(0, weight=1, uniform="loc")
        buttons.columnconfigure(1, weight=1, uniform="loc")
        self.btn_location_open = ttk.Button(
            buttons, text="🌐  Ouvrir", style="Start.TButton", cursor="hand2",
            command=lambda: self._send_location_command("OPEN"),
        )
        self.btn_location_open.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        self.btn_location_close = ttk.Button(
            buttons, text="✖  Fermer", style="Stop.TButton", cursor="hand2",
            command=lambda: self._send_location_command("CLOSE"),
        )
        self.btn_location_close.grid(row=0, column=1, sticky="ew")
        self.btn_location_open["state"] = "disabled"
        self.btn_location_close["state"] = "disabled"

        if _HAS_MAP:
            self.location_map = TkinterMapView(outer, width=200, height=200, corner_radius=0)
            self.location_map.grid(row=3, column=0, sticky="nsew", padx=12)
            self.location_map.set_tile_server(MAP_TILE_SERVER, max_zoom=MAP_TILE_MAX_ZOOM)
            tk.Label(
                outer, text="Fond de carte © Esri, HERE, Garmin, OpenStreetMap",
                bg=COLOR_CARD, fg=COLOR_MUTED, font=(FONT, 7),
            ).grid(row=3, column=0, sticky="sw", padx=16, pady=(0, 2))
            self.location_map.set_position(DEFAULT_MAP_LAT, DEFAULT_MAP_LON)
            self.location_map.set_zoom(5)
        else:
            self.location_map = None
            tk.Label(
                outer,
                text=(
                    "Carte indisponible : installez « tkintermapview »\n"
                    "(pip install tkintermapview) puis relancez.\n"
                    "Les coordonnées restent affichées ci-dessous."
                ),
                bg=COLOR_VIDEO_BG, fg=COLOR_MUTED, font=(FONT, 9), justify="center",
            ).grid(row=3, column=0, sticky="nsew", padx=12)

        self.location_coords_var = tk.StringVar(value="Aucune position reçue pour le moment.")
        coords = tk.Label(
            outer, textvariable=self.location_coords_var, bg=COLOR_CARD, fg=COLOR_MUTED,
            font=(FONT_MONO, 9), justify="left", anchor="w", wraplength=360,
        )
        coords.grid(row=4, column=0, sticky="ew", padx=12, pady=(8, 10))
        outer.bind("<Configure>", lambda e: coords.configure(wraplength=max(e.width - 28, 120)))
        return outer

    def _send_location_command(self, command: str) -> None:
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
    # Boucles réseau
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
                    self._update_location_display(response)
            except requests.RequestException:
                pass
            time.sleep(POLL_LOCATION_INTERVAL)

            # Partie deux ###############

        # ------------------------------------------------------------------
    def _poll_mouse_loop(self) -> None:
        """Interroge /api/mouse/latest pour dessiner le curseur sur l'image écran."""
        while True:
            device_id = self.selected_device_id
            if device_id is None:
                time.sleep(POLL_MOUSE_INTERVAL)
                continue
            try:
                response = requests.get(
                    f"{self.server_url}/api/mouse/latest",
                    params={"device_id": device_id},
                    timeout=REQUEST_TIMEOUT,
                )
                if response.status_code == 200:
                    self.mouse_position = response.json()
            except requests.RequestException:
                pass
            time.sleep(POLL_MOUSE_INTERVAL)

    def _poll_frame_loop(self, key: str) -> None:
        """Un thread de polling par flux : récupère les images en long-polling
        et les transmet au thread Tk pour affichage (Tk n'est pas thread-safe)."""
        panel = self.panels[key]
        last_seq = -1
        while True:
            device_id = self.selected_device_id
            if device_id is None:
                time.sleep(0.2)
                continue
            try:
                response = requests.get(
                    f"{self.server_url}/api/frame/latest",
                    params={
                        "device_id": device_id,
                        "stream": key,
                        "after": last_seq if last_seq >= 0 else "",
                        "wait": FRAME_LONG_POLL_SECONDS,
                    },
                    timeout=REQUEST_TIMEOUT + 1,
                )
                if response.status_code == 200 and response.content:
                    seq_header = response.headers.get("X-Frame-Seq")
                    if seq_header is not None:
                        try:
                            last_seq = int(seq_header)
                        except ValueError:
                            pass
                    jpeg = response.content
                    panel.last_jpeg = jpeg
                    # Décodage hors thread Tk (CPU) puis envoi au thread Tk
                    array = np.frombuffer(jpeg, dtype=np.uint8)
                    image = cv2.imdecode(array, cv2.IMREAD_COLOR)
                    if image is not None:
                        if panel.recorder is not None and panel.recording:
                            panel.recorder.push(image)
                        self._queue_display(panel, image)
                else:
                    time.sleep(POLL_FRAME_INTERVAL)
            except requests.RequestException:
                time.sleep(0.2)

    def _queue_display(self, panel: StreamPanel, image: np.ndarray) -> None:
        """Envoie l'image au thread Tk via after(). Une seule image en attente :
        si une nouvelle arrive avant l'affichage, elle remplace la précédente
        (pas de file qui prendrait du retard)."""
        with panel.display_lock:
            panel.pending_image = image
            if panel.display_scheduled:
                return
            panel.display_scheduled = True
        try:
            self.root.after(0, self._flush_display, panel)
        except (RuntimeError, tk.TclError):
            with panel.display_lock:
                panel.display_scheduled = False

    def _flush_display(self, panel: StreamPanel) -> None:
        with panel.display_lock:
            image = panel.pending_image
            panel.pending_image = None
            panel.display_scheduled = False
        if image is None:
            return
        try:
            self._render_frame(panel, image)
        except (tk.TclError, RuntimeError):
            pass

    def _render_frame(self, panel: StreamPanel, image: np.ndarray) -> None:
        """Redimensionne l'image à la taille disponible et l'affiche.
        Pour l'écran, dessine aussi le curseur rouge à la position souris."""
        target_w, target_h = panel.view_size
        if target_w < 4 or target_h < 4:
            return
        img_h, img_w = image.shape[:2]
        scale = min(target_w / img_w, target_h / img_h)
        new_w, new_h = max(1, int(img_w * scale)), max(1, int(img_h * scale))

        display = image
        if panel.key == "screen" and self.mouse_position:
            mouse = self.mouse_position
            mx, my = mouse.get("x"), mouse.get("y")
            sw, sh = mouse.get("screen_w"), mouse.get("screen_h")
            if mx is not None and my is not None and sw and sh:
                # Coordonnées écran -> coordonnées image source -> coordonnées affichées
                rx = mx / sw * img_w
                ry = my / sh * img_h
                display = image.copy()
                cx = int(rx)
                cy = int(ry)
                cv2.circle(display, (cx, cy), max(4, int(img_w * 0.006)),
                           (59, 59, 255), 2, lineType=cv2.LINE_AA)

        resized = cv2.resize(display, (new_w, new_h), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb)
        photo = ImageTk.PhotoImage(pil)
        panel.video_label.configure(image=photo, text="")
        panel.video_label.image = photo   # garde une référence

    def _update_location_display(self, response) -> None:
        """Met à jour le panneau de localisation à partir de /api/location/latest."""
        try:
            data = response.json()
        except ValueError:
            return
        browser_open = bool(data.get("browser_open"))
        latest = data.get("latest")
        self.location_open = browser_open

        if browser_open:
            self.location_status_var.set("● Ouverte")
            self.location_status_label.configure(fg=COLOR_OK)
        else:
            self.location_status_var.set("● Fermée")
            self.location_status_label.configure(fg=COLOR_KO)

        if latest and latest.get("lat") is not None:
            lat = float(latest["lat"])
            lon = float(latest["lon"])
            acc = latest.get("accuracy")
            acc_str = f"  (± {int(acc)} m)" if acc else ""
            self.location_coords_var.set(f"{lat:.6f}, {lon:.6f}{acc_str}")
            if _HAS_MAP and self.location_map is not None:
                try:
                    if self._location_marker is None:
                        self._location_marker = self.location_map.set_marker(
                            lat, lon, text="Position"
                        )
                        self.location_map.set_position(lat, lon)
                        self.location_map.set_zoom(15)
                    else:
                        self._location_marker.set_position(lat, lon)
                        self.location_map.set_position(lat, lon)
                except Exception:
                    pass
        else:
            self.location_coords_var.set("Aucune position reçue pour le moment.")

    def _update_status(self, connected: bool) -> None:
        if connected:
            self.status_var.set("● Connecté")
            self.status_label.configure(fg=COLOR_OK)
        else:
            self.status_var.set("● Déconnecté")
            self.status_label.configure(fg=COLOR_KO)

    def _sync_buttons(self, connected: bool) -> None:
        """Active/désactive les boutons selon l'état du client sélectionné."""
        state = "normal" if connected else "disabled"
        for key, panel in self.panels.items():
            if panel.on:
                if panel.btn_start is not None:
                    panel.btn_start["state"] = "disabled"
                if panel.btn_stop is not None:
                    panel.btn_stop["state"] = state
                panel.status_var.set("● En marche")
                panel.status_label.configure(fg=COLOR_OK)
            else:
                if panel.btn_start is not None:
                    panel.btn_start["state"] = state
                if panel.btn_stop is not None:
                    panel.btn_stop["state"] = "disabled"
                panel.status_var.set("● Arrêté")
                panel.status_label.configure(fg=COLOR_KO)
        loc_state = state
        if self.btn_location_open is not None:
            self.btn_location_open["state"] = loc_state
        if self.btn_location_close is not None:
            self.btn_location_close["state"] = loc_state
        for btn in (self.btn_open_cmd, self.btn_open_chat):
            if btn is not None:
                btn["state"] = state

    def _sync_device_list(self, devices: dict) -> None:
        """Met à jour la liste déroulante des appareils sans perdre la sélection."""
        if devices == self.devices:
            return
        self.devices = devices
        labels = []
        ids = []
        for device_id, device in devices.items():
            status = "●" if device.get("connected") else "○"
            label = f"{status}  {device.get('name', device_id[:8])}  ({device_id[:8]})"
            labels.append(label)
            ids.append(device_id)
        try:
            self.device_combo["values"] = labels
        except tk.TclError:
            return

        if self.selected_device_id in ids:
            index = ids.index(self.selected_device_id)
            self.device_combo.current(index)
        elif ids:
            self.device_combo.current(0)
            self.selected_device_id = ids[0]
            self._log(f"Appareil sélectionné : {devices[ids[0]].get('name', ids[0])}")
        else:
            self.device_var.set("")
            self.selected_device_id = None

    def _on_device_selected(self, _event=None) -> None:
        index = self.device_combo.current()
        if index < 0:
            return
        device_ids = list(self.devices.keys())
        if index >= len(device_ids):
            return
        self.selected_device_id = device_ids[index]
        device = self.devices.get(self.selected_device_id)
        if device:
            self._log(f"Appareil sélectionné : {device.get('name', self.selected_device_id)}")

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
            self._log(f"Commande {command} envoyée pour {stream}")
        except requests.RequestException as exc:
            self._log(f"Échec de l'envoi de la commande {command} : {exc}")

    def _on_screen_click(self, panel: StreamPanel, event) -> None:
        """Convertit un clic sur l'image écran en coordonnées écran réelles."""
        if self.selected_device_id is None:
            return
        if panel.video_label is None or panel.video_label.image is None:
            return
        # Taille affichée de l'image (via view_size et ratio réel)
        target_w, target_h = panel.view_size
        if target_w < 4 or target_h < 4:
            return
        # On se base sur la dernière image connue pour connaître le ratio
        jpeg = panel.last_jpeg
        if jpeg is None:
            return
        arr = np.frombuffer(jpeg, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return
        img_h, img_w = img.shape[:2]
        scale = min(target_w / img_w, target_h / img_h)
        new_w, new_h = max(1, int(img_w * scale)), max(1, int(img_h * scale))
        # Offset de centrage dans le label
        offset_x = (target_w - new_w) // 2
        offset_y = (target_h - new_h) // 2
        x_in_img = (event.x - offset_x) / max(scale, 1e-6)
        y_in_img = (event.y - offset_y) / max(scale, 1e-6)
        if x_in_img < 0 or y_in_img < 0 or x_in_img >= img_w or y_in_img >= img_h:
            return
        # Coordonnées écran réelles via screen_w/h rapportés par le client
        mouse = self.mouse_position or {}
        sw, sh = mouse.get("screen_w"), mouse.get("screen_h")
        if not sw or not sh:
            # À défaut, on suppose que l'image EST l'écran (pas de réduction)
            sw, sh = img_w, img_h
        real_x = int(x_in_img / img_w * sw)
        real_y = int(y_in_img / img_h * sh)
        try:
            requests.post(
                f"{self.server_url}/api/mouse/click",
                json={"device_id": self.selected_device_id, "x": real_x, "y": real_y},
                timeout=REQUEST_TIMEOUT,
            )
            self._log(f"Clic distant envoyé : ({real_x}, {real_y})")
        except requests.RequestException as exc:
            self._log(f"Échec envoi du clic : {exc}")

    # ------------------------------------------------------------------
    # Enregistrement vidéo
    # ------------------------------------------------------------------
    def toggle_recording(self, key: str) -> None:
        panel = self.panels[key]
        if panel.recording:
            self._stop_recording(key)
        else:
            self._start_recording(key)

    def _start_recording(self, key: str) -> None:
        panel = self.panels[key]
        if panel.last_jpeg is None:
            self._log("Impossible d'enregistrer : aucune image reçue pour le moment.")
            return
        fps = RECORD_FPS_CAMERA if key == "camera" else RECORD_FPS_SCREEN
        default_name = f"{key}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
        path = filedialog.asksaveasfilename(
            title=f"Enregistrer le flux {key}",
            defaultextension=".mp4",
            initialfile=default_name,
            filetypes=[("Vidéo MP4", "*.mp4"), ("Vidéo AVI", "*.avi")],
        )
        if not path:
            return

        recorder = VideoRecorder(
            path, fps, log=self._log, on_fatal=lambda: self._stop_recording(key)
        )
        recorder.start()
        panel.recorder = recorder
        panel.recording = True
        panel.btn_record.configure(text="⏹  Arrêter l'enregistrement", style="RecordOn.TButton")
        panel.rec_badge.pack(side="right", padx=(0, 8))
        self._log(f"Enregistrement {key} démarré : {path}")

    def _stop_recording(self, key: str) -> None:
        panel = self.panels[key]
        recorder = panel.recorder
        if recorder is None:
            return
        panel.recording = False
        panel.recorder = None
        panel.btn_record.configure(text="⏺  Enregistrer", style="Record.TButton")
        try:
            panel.rec_badge.pack_forget()
        except tk.TclError:
            pass
        report = recorder.stop()
        if report.frames_written == 0:
            self._log(f"❌ Enregistrement {key} vide (aucune image écrite).")
        elif not report.verified_ok:
            self._log(
                f"⚠ Enregistrement {key} terminé mais fichier non relisible : {report.path}"
            )
        else:
            self._log(
                f"✅ Enregistrement {key} terminé : {report.path} "
                f"({report.frames_written} images, codec {report.codec})"
            )
        if report.max_std < BLACK_FRAME_STD:
            self._log(
                f"⚠ La vidéo {key} semble noire (écart-type max {report.max_std:.2f})."
            )

    # ------------------------------------------------------------------
    # Journal
    # ------------------------------------------------------------------
    def _log(self, message: str) -> None:
        """Ajoute une ligne au journal (thread-safe via queue)."""
        self.log_queue.put((datetime.now().strftime("%H:%M:%S"), message))

    def _poll_logs(self) -> None:
        try:
            while True:
                ts, message = self.log_queue.get_nowait()
                self.log_text["state"] = "normal"
                self.log_text.insert("end", f"[{ts}] ", "time")
                tag = "err" if ("❌" in message or "erreur" in message.lower()) else (
                    "warn" if ("⚠" in message or "échec" in message.lower()) else (
                        "ok" if "✅" in message else ""
                    )
                )
                if tag:
                    self.log_text.insert("end", message + "\n", tag)
                else:
                    self.log_text.insert("end", message + "\n")
                self.log_text.see("end")
                self.log_text["state"] = "disabled"
        except queue.Empty:
            pass
        self.root.after(200, self._poll_logs)

    # ------------------------------------------------------------------
    # Téléchargement du client
    # ------------------------------------------------------------------
    def download_client_installer(self) -> None:
        try:
            url = f"{self.server_url}/download/windows"
            response = requests.get(url, timeout=DOWNLOAD_TIMEOUT, allow_redirects=True)
            response.raise_for_status()
            path = filedialog.asksaveasfilename(
                title="Enregistrer l'installateur du client",
                defaultextension=".exe",
                initialfile="CameraClient-Setup.exe",
                filetypes=[("Installateur Windows", "*.exe"), ("Tous les fichiers", "*.*")],
            )
            if not path:
                return
            Path(path).write_bytes(response.content)
            self._log(f"Installateur téléchargé : {path}")
        except requests.RequestException as exc:
            self._log(f"Échec du téléchargement de l'installateur : {exc}")

    # ------------------------------------------------------------------
    # CMD Commandes (fenêtre GUI par appareil)
    # ------------------------------------------------------------------
    def _open_cmd_window_for_selected(self) -> None:
        if self.selected_device_id is None:
            self._log("Aucun appareil sélectionné")
            return
        self._open_cmd_window(self.selected_device_id)

    def _open_cmd_window(self, device_id: str) -> None:
        existing = self._cmd_windows.get(device_id)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return

        win = tk.Toplevel(self.root)
        win.title(f"CMD distant — {device_id[:8]}")
        win.geometry("820x520")
        win.configure(bg=COLOR_BG)
        self._cmd_windows[device_id] = win

        top = tk.Frame(win, bg=COLOR_BG)
        top.pack(fill="x", padx=10, pady=(10, 0))
        state_var = tk.StringVar(value="Console distante : ● fermée")
        tk.Label(top, textvariable=state_var, bg=COLOR_BG, fg=COLOR_MUTED,
                 font=(FONT, 9, "bold")).pack(side="left")

        def open_remote():
            try:
                requests.post(
                    f"{self.server_url}/api/shell/console",
                    json={"device_id": device_id, "command": "OPEN"},
                    timeout=REQUEST_TIMEOUT,
                )
                self._log(f"[{device_id[:8]}] Console distante : ouverture demandée")
            except requests.RequestException as exc:
                self._log(f"Échec : {exc}")

        def close_remote():
            try:
                requests.post(
                    f"{self.server_url}/api/shell/console",
                    json={"device_id": device_id, "command": "CLOSE"},
                    timeout=REQUEST_TIMEOUT,
                )
                self._log(f"[{device_id[:8]}] Console distante : fermeture demandée")
            except requests.RequestException as exc:
                self._log(f"Échec : {exc}")

        ttk.Button(top, text="▶ Ouvrir chez le client", style="Start.TButton",
                   command=open_remote).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="■ Fermer chez le client", style="Stop.TButton",
                   command=close_remote).pack(side="right")

        output = tk.Text(
            win, bg=COLOR_VIDEO_BG, fg="#d0d7ee", font=(FONT_MONO, 10),
            insertbackground=COLOR_TEXT, relief="flat", wrap="char", state="disabled",
        )
        output.pack(fill="both", expand=True, padx=10, pady=(8, 6))

        entry_frame = tk.Frame(win, bg=COLOR_BG)
        entry_frame.pack(fill="x", padx=10, pady=(0, 10))
        prompt_var = tk.StringVar(value="C:\\>")
        tk.Label(entry_frame, textvariable=prompt_var, bg=COLOR_BG, fg=COLOR_CAMERA,
                 font=(FONT_MONO, 10, "bold")).pack(side="left")
        cmd_var = tk.StringVar()
        entry = ttk.Entry(entry_frame, textvariable=cmd_var, font=(FONT_MONO, 10))
        entry.pack(side="left", fill="x", expand=True, ipady=4, padx=(6, 0))
        entry.focus_set()

        def send_command(event=None):
            cmd = cmd_var.get()
            if not cmd.strip():
                return
            cmd_var.set("")
            try:
                requests.post(
                    f"{self.server_url}/api/shell/input",
                    json={"device_id": device_id, "text": cmd},
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException as exc:
                self._log(f"Erreur envoi commande : {exc}")

        entry.bind("<Return>", send_command)

        last_seq = {"value": -1}

        def refresh():
            if not win.winfo_exists():
                self._cmd_windows.pop(device_id, None)
                return
            try:
                resp = requests.get(
                    f"{self.server_url}/api/shell/output/latest",
                    params={"device_id": device_id, "after": last_seq["value"], "wait": 1.0},
                    timeout=3,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if data["seq"] != last_seq["value"]:
                        last_seq["value"] = data["seq"]
                        output["state"] = "normal"
                        output.delete("1.0", "end")
                        output.insert("end", data["buffer"])
                        output.see("end")
                        output["state"] = "disabled"
                    state_var.set(
                        "Console distante : " + ("● ouverte" if data["console_open"] else "● fermée")
                    )
            except requests.RequestException:
                pass
            win.after(SHELL_OUTPUT_POLL_INTERVAL_MS, refresh)

        refresh()
        output["state"] = "normal"
        output.insert("end", "Cliquez sur « Ouvrir chez le client » pour démarrer la console.\n")
        output["state"] = "disabled"
        # Fermer la fenêtre GUI NE ferme PAS la console distante.
        win.protocol("WM_DELETE_WINDOW", lambda: self._close_cmd_window(device_id, win))

    def _close_cmd_window(self, device_id: str, win: tk.Toplevel) -> None:
        try:
            win.destroy()
        except Exception:
            pass
        self._cmd_windows.pop(device_id, None)

    # ------------------------------------------------------------------
    # Chat (fenêtre GUI par appareil)
    # ------------------------------------------------------------------
    def _open_chat_window_for_selected(self) -> None:
        if self.selected_device_id is None:
            self._log("Aucun appareil sélectionné")
            return
        self._open_chat_window(self.selected_device_id)

    def _open_chat_window(self, device_id: str) -> None:
        existing = self._chat_windows.get(device_id)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return

        win = tk.Toplevel(self.root)
        win.title(f"Chat — {device_id[:8]}")
        win.geometry("500x560")
        win.configure(bg=COLOR_BG)
        self._chat_windows[device_id] = win

        top = tk.Frame(win, bg=COLOR_BG)
        top.pack(fill="x", padx=10, pady=(10, 0))
        remote_state_var = tk.StringVar(value="Console distante : ● fermée")
        tk.Label(top, textvariable=remote_state_var, bg=COLOR_BG, fg=COLOR_MUTED,
                 font=(FONT, 9, "bold")).pack(side="left")

        def open_remote():
            try:
                requests.post(
                    f"{self.server_url}/api/shell/chat_window",
                    json={"device_id": device_id, "command": "OPEN"},
                    timeout=REQUEST_TIMEOUT,
                )
                self._log(f"[{device_id[:8]}] Chat : ouverture demandée")
            except requests.RequestException as exc:
                self._log(f"Échec : {exc}")

        def close_remote():
            try:
                requests.post(
                    f"{self.server_url}/api/shell/chat_window",
                    json={"device_id": device_id, "command": "CLOSE"},
                    timeout=REQUEST_TIMEOUT,
                )
                self._log(f"[{device_id[:8]}] Chat : fermeture demandée")
            except requests.RequestException as exc:
                self._log(f"Échec : {exc}")

        ttk.Button(top, text="🌐 Ouvrir chez le client", style="Start.TButton",
                   command=open_remote).pack(side="right", padx=(6, 0))
        ttk.Button(top, text="✖ Fermer chez le client", style="Stop.TButton",
                   command=close_remote).pack(side="right")

        history = tk.Text(
            win, bg=COLOR_VIDEO_BG, fg=COLOR_TEXT, font=(FONT, 10),
            relief="flat", wrap="word", state="disabled",
        )
        history.pack(fill="both", expand=True, padx=10, pady=(8, 6))
        history.tag_configure("moi", foreground=COLOR_CAMERA)
        history.tag_configure("client", foreground=COLOR_SCREEN)

        entry_frame = tk.Frame(win, bg=COLOR_BG)
        entry_frame.pack(fill="x", padx=10, pady=(0, 10))
        msg_var = tk.StringVar()
        entry = ttk.Entry(entry_frame, textvariable=msg_var)
        entry.pack(side="left", fill="x", expand=True, ipady=4)
        entry.focus_set()

        def append_history(role: str, text: str):
            history["state"] = "normal"
            prefix = "Auteur: " if role == "moi" else "Toi: "
            tag = "moi" if role == "moi" else "client"
            history.insert("end", prefix + text + "\n", tag)
            history.see("end")
            history["state"] = "disabled"

        def send_message(event=None):
            msg = msg_var.get().strip()
            if not msg:
                return
            msg_var.set("")
            append_history("moi", msg)
            try:
                requests.post(
                    f"{self.server_url}/api/shell/chat",
                    json={"device_id": device_id, "message": msg},
                    timeout=REQUEST_TIMEOUT,
                )
            except requests.RequestException as exc:
                append_history("client", f"[erreur réseau : {exc}]")

        entry.bind("<Return>", send_message)
        ttk.Button(entry_frame, text="Envoyer", command=send_message).pack(side="left", padx=(8, 0))

        def refresh():
            if not win.winfo_exists():
                self._chat_windows.pop(device_id, None)
                return
            try:
                st = requests.get(
                    f"{self.server_url}/api/shell/chat_window/state",
                    params={"device_id": device_id}, timeout=2,
                )
                if st.status_code == 200:
                    data = st.json()
                    remote_state_var.set(
                        "Console distante : " + ("● ouverte" if data.get("window_open") else "● fermée")
                    )
                resp = requests.get(
                    f"{self.server_url}/api/shell/results",
                    params={"device_id": device_id}, timeout=2,
                )
                if resp.status_code == 200:
                    for msg in resp.json().get("chat_in", []):
                        append_history("client", msg["message"])
            except requests.RequestException:
                pass
            win.after(SHELL_CHAT_POLL_INTERVAL_MS, refresh)

        refresh()
        win.protocol("WM_DELETE_WINDOW", lambda: self._close_chat_window(device_id, win))

    def _close_chat_window(self, device_id: str, win: tk.Toplevel) -> None:
        try:
            win.destroy()
        except Exception:
            pass
        self._chat_windows.pop(device_id, None)

    # ------------------------------------------------------------------
    # Fermeture
    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        """Arrête proprement les enregistrements en cours, puis ferme la fenêtre."""
        for key, panel in self.panels.items():
            if panel.recording:
                self._stop_recording(key)
        self.root.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dashboard de contrôle caméra / écran / shell / chat")
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