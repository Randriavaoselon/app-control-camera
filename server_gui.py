"""
Interface graphique de contrôle — consomme l'API HTTP du serveur.

Cette GUI ne parle jamais directement aux clients caméra : elle passe
toujours par l'API du serveur (SERVER_URL). Comme un serveur peut gérer
plusieurs clients, elle affiche la liste des appareils connus et pilote
celui qui est sélectionné.
"""

import io
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk
from datetime import datetime

import requests
from PIL import Image, ImageTk

SERVER_URL = "http://127.0.0.1:5050"   # à remplacer par l'URL publique du serveur
POLL_DEVICES_INTERVAL = 2.0
POLL_STATUS_INTERVAL = 1.0
POLL_FRAME_INTERVAL = 0.1
REQUEST_TIMEOUT = 5

COLOR_OK = "#28a745"
COLOR_KO = "#d9534f"


class CameraServerApp:
    def __init__(self, root: tk.Tk, server_url: str = SERVER_URL):
        self.root = root
        self.server_url = server_url.rstrip("/")
        self.root.title("Contrôle caméra — Dashboard")
        self.root.geometry("840x660")
        self.root.minsize(700, 560)

        self.camera_on = False
        self.devices: dict[str, dict] = {}          # device_id -> infos
        self.selected_device_id: str | None = None
        self.log_queue: "queue.Queue" = queue.Queue()

        self._build_ui()
        self._poll_logs()

        threading.Thread(target=self._poll_devices_loop, daemon=True).start()
        threading.Thread(target=self._poll_status_loop, daemon=True).start()
        threading.Thread(target=self._poll_frame_loop, daemon=True).start()

    # ------------------------------------------------------------------
    # Construction de l'interface
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Title.TLabel", font=("Segoe UI", 14, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"))
        style.configure("Start.TButton", font=("Segoe UI", 10, "bold"), padding=8)
        style.configure("Stop.TButton", font=("Segoe UI", 10, "bold"), padding=8)

        header = ttk.Frame(self.root, padding=12)
        header.pack(fill="x")
        ttk.Label(header, text="Contrôle caméra à distance", style="Title.TLabel").pack(side="left")

        self.status_var = tk.StringVar(value="Connexion au serveur...")
        self.status_label = ttk.Label(
            header, textvariable=self.status_var, style="Status.TLabel", foreground=COLOR_KO
        )
        self.status_label.pack(side="right")

        device_bar = ttk.Frame(self.root, padding=(12, 0))
        device_bar.pack(fill="x")
        ttk.Label(device_bar, text="Appareil :").pack(side="left", padx=(0, 6))
        self.device_var = tk.StringVar()
        self.device_combo = ttk.Combobox(
            device_bar, textvariable=self.device_var, state="readonly", width=50
        )
        self.device_combo.pack(side="left", fill="x", expand=True)
        self.device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)

        video_frame = ttk.Frame(self.root, padding=(12, 0))
        video_frame.pack(fill="both", expand=True)
        self.video_label = tk.Label(
            video_frame, bg="black", fg="white", text="Aucun flux vidéo", font=("Segoe UI", 12)
        )
        self.video_label.pack(fill="both", expand=True)

        controls = ttk.Frame(self.root, padding=12)
        controls.pack(fill="x")

        self.btn_start = ttk.Button(
            controls, text="▶  Allumer la caméra", command=self.send_start, style="Start.TButton"
        )
        self.btn_start.pack(side="left", padx=(0, 8))
        self.btn_start["state"] = "disabled"

        self.btn_stop = ttk.Button(
            controls, text="■  Éteindre la caméra", command=self.send_stop, style="Stop.TButton"
        )
        self.btn_stop.pack(side="left")
        self.btn_stop["state"] = "disabled"

        log_frame = ttk.LabelFrame(self.root, text="Journal des événements", padding=8)
        log_frame.pack(fill="both", expand=False, padx=12, pady=(0, 12))
        self.log_text = tk.Text(
            log_frame, height=8, state="disabled", bg="#111118", fg="#d0d0d0", font=("Consolas", 9)
        )
        self.log_text.pack(fill="both", expand=True)

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

                if connected != was_connected:
                    self._update_status(connected)
                    was_connected = connected

                self._sync_buttons(connected)

            except requests.RequestException:
                if was_connected:
                    self._update_status(False)
                    was_connected = False

            time.sleep(POLL_STATUS_INTERVAL)

    def _poll_frame_loop(self) -> None:
        while True:
            device_id = self.selected_device_id
            if device_id is None:
                time.sleep(POLL_FRAME_INTERVAL)
                continue
            try:
                response = requests.get(
                    f"{self.server_url}/api/frame/latest",
                    params={"device_id": device_id},
                    timeout=REQUEST_TIMEOUT,
                )
                if response.status_code == 200 and response.content:
                    self._display_frame(response.content)
            except requests.RequestException:
                pass
            time.sleep(POLL_FRAME_INTERVAL)

    # ------------------------------------------------------------------
    # Sélection de l'appareil (un serveur peut en avoir plusieurs)
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
                first_id = next(iter(devices))
                self._select_device(first_id)
            elif self.selected_device_id not in devices and devices:
                first_id = next(iter(devices))
                self._select_device(first_id)
            elif not devices:
                self.selected_device_id = None
                self.device_var.set("")
                self.status_var.set("Aucun appareil connu")
                self.status_label.configure(foreground=COLOR_KO)

        self.root.after(0, apply)

    def _select_device(self, device_id: str) -> None:
        self.selected_device_id = device_id
        device = self.devices.get(device_id, {})
        label = f"{device.get('name', '?')} — ({device_id[:8]})"
        self.device_var.set(label)
        self._log(f"Appareil sélectionné : {label}")

    def _on_device_selected(self, _event) -> None:
        index = self.device_combo.current()
        device_ids = list(self.devices.keys())
        if 0 <= index < len(device_ids):
            self._select_device(device_ids[index])

    # ------------------------------------------------------------------
    # Commandes envoyées au serveur (pour l'appareil sélectionné)
    # ------------------------------------------------------------------
    def send_start(self) -> None:
        self._send_command("START_CAMERA")

    def send_stop(self) -> None:
        self._send_command("STOP_CAMERA")
        self.root.after(0, lambda: self.video_label.configure(image="", text="Caméra éteinte"))

    def _send_command(self, command: str) -> None:
        if self.selected_device_id is None:
            self._log("Aucun appareil sélectionné")
            return
        try:
            requests.post(
                f"{self.server_url}/api/command",
                json={"device_id": self.selected_device_id, "command": command},
                timeout=REQUEST_TIMEOUT,
            )
            self._log(f"Commande envoyée : {command}")
        except requests.RequestException as exc:
            self._log(f"Échec de l'envoi de la commande : {exc}")

    # ------------------------------------------------------------------
    # Mise à jour de l'interface (toujours via root.after, thread principal)
    # ------------------------------------------------------------------
    def _update_status(self, connected: bool) -> None:
        def apply():
            if connected:
                self.status_var.set("Client connecté")
                self.status_label.configure(foreground=COLOR_OK)
                self._log("Client caméra connecté")
            else:
                self.status_var.set("Client déconnecté")
                self.status_label.configure(foreground=COLOR_KO)
                self._log("Client caméra déconnecté")
        self.root.after(0, apply)

    def _sync_buttons(self, connected: bool) -> None:
        def apply():
            self.btn_start["state"] = "normal" if connected and not self.camera_on else "disabled"
            self.btn_stop["state"] = "normal" if connected and self.camera_on else "disabled"
        self.root.after(0, apply)

    def _display_frame(self, jpeg_bytes: bytes) -> None:
        def apply():
            image = Image.open(io.BytesIO(jpeg_bytes))
            max_w = self.video_label.winfo_width() or 780
            max_h = self.video_label.winfo_height() or 420
            image.thumbnail((max_w, max_h))
            photo = ImageTk.PhotoImage(image)
            self.video_label.configure(image=photo, text="")
            self.video_label.image = photo  # garder une référence
        self.root.after(0, apply)

    def _poll_logs(self) -> None:
        while not self.log_queue.empty():
            message = self.log_queue.get_nowait()
            self.log_text["state"] = "normal"
            self.log_text.insert("end", message + "\n")
            self.log_text.see("end")
            self.log_text["state"] = "disabled"
        self.root.after(200, self._poll_logs)

    def _log(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.log_queue.put(f"[{timestamp}] {message}")


def main() -> None:
    root = tk.Tk()
    CameraServerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()