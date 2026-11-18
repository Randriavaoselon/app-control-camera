"""
Console chat visible chez le client (vraie cmd.exe) + capture des frappes.

Deux responsabilités :
  1. Ouvrir une vraie console cmd.exe visible, titrée, avec le [X] neutralisé.
  2. Capturer les frappes de l'utilisateur DANS cette console via un hook
     clavier bas niveau (WH_KEYBOARD_LL), et bloquer VK_RETURN pour que
     cmd.exe ne reçoive jamais la ligne (Solution B — préfixe invisible).

Le CMD Commandes n'est PAS dans ce module : il n'a aucune fenêtre côté
client, il est géré dans client.py via un cmd.exe persistant sans fenêtre.

Windows uniquement. Sous Linux, l'import échoue et client.py désactive
automatiquement la fonctionnalité chat.

CORRECTIFS v2 :
  - _find_hwnd() identifie la console par PID du process cmd.exe, et non
    par titre. GetWindowTextW ne renvoie rien pour les fenêtres de classe
    ConsoleWindowClass ; la recherche par PID est fiable.
  - open() attend la création réelle de la fenêtre (retry jusqu'à 3 s).
  - L'injection GUI utilise AttachConsole(pid) + WriteConsoleInputW, qui
    écrit DIRECTEMENT dans la file d'entrée de la console, sans dépendre
    du focus clavier (SetForegroundWindow + SendInput échouaient souvent).
  - show_incoming() ne dépend plus de is_hook_active() ni du hook : tant
    que la console est ouverte, le message GUI s'affiche.
  - is_hook_active() tolère les longues périodes d'inactivité (timeout
    porté à 30 min) : l'utilisateur peut taper sans que VK_RETURN ne soit
    soudain plus bloqué. Le [X] reste neutralisé tant que la console est
    vivante, et redevient fonctionnel si le hook meurt vraiment.
  - Le hook appelle touch() à chaque frappe : l'activité utilisateur
    repousse aussi le timeout.
  - Tous les prototypes Win32 sont définis AVANT tout appel.
"""

import ctypes
import ctypes.wintypes
import platform
import subprocess
import sys
import threading
import time


# --- Constantes Win32 ---
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_SYSKEYDOWN = 0x0104
WM_CLOSE = 0x0010
VK_RETURN = 0x0D
VK_BACK = 0x08
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
GWLP_WNDPROC = -4

# WriteConsoleInput : codes d'événement de console
KEY_EVENT = 0x0001

# --- Prototypes Win32 (essentiel : sans eux, ctypes suppose c_int partout,
#     ce qui provoque OverflowError sur les pointeurs 64 bits) ---
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# SetWindowLongPtrW : (HWND, int, LONG_PTR) -> LONG_PTR
_user32.SetWindowLongPtrW.argtypes = [
    ctypes.wintypes.HWND,
    ctypes.c_int,
    ctypes.c_void_p,
]
_user32.SetWindowLongPtrW.restype = ctypes.c_void_p

# CallWindowProcW : (WNDPROC, HWND, UINT, WPARAM, LPARAM) -> LRESULT
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_void_p,
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
)
_user32.CallWindowProcW.argtypes = [
    ctypes.c_void_p,
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
]
_user32.CallWindowProcW.restype = ctypes.c_void_p

# GetWindowThreadProcessId : (HWND, LPDWORD) -> DWORD
_user32.GetWindowThreadProcessId.argtypes = [
    ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD),
]
_user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD

# AttachConsole / FreeConsole
_kernel32.AttachConsole.argtypes = [ctypes.wintypes.DWORD]
_kernel32.AttachConsole.restype = ctypes.wintypes.BOOL
_kernel32.FreeConsole.argtypes = []
_kernel32.FreeConsole.restype = ctypes.wintypes.BOOL

# GetModuleHandleW : (LPCWSTR) -> HMODULE
_kernel32.GetModuleHandleW.argtypes = [ctypes.wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = ctypes.wintypes.HMODULE


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.wintypes.WORD),
        ("wScan", ctypes.wintypes.WORD),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", ctypes.wintypes.DWORD), ("u", _U)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.wintypes.DWORD),
        ("scanCode", ctypes.wintypes.DWORD),
        ("flags", ctypes.wintypes.DWORD),
        ("time", ctypes.wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]


# --- Structures pour WriteConsoleInputW ---
class CHAR_UNION(ctypes.Union):
    _fields_ = [("UnicodeChar", ctypes.wintypes.WCHAR), ("AsciiChar", ctypes.c_char)]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", ctypes.wintypes.BOOL),
        ("wRepeatCount", ctypes.wintypes.WORD),
        ("wVirtualKeyCode", ctypes.wintypes.WORD),
        ("wVirtualScanCode", ctypes.wintypes.WORD),
        ("uChar", CHAR_UNION),
        ("dwControlKeyState", ctypes.wintypes.DWORD),
    ]


class EVENT_UNION(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [
        ("EventType", ctypes.wintypes.WORD),
        ("Event", EVENT_UNION),
    ]


_kernel32.WriteConsoleInputW.argtypes = [
    ctypes.wintypes.HANDLE,
    ctypes.POINTER(INPUT_RECORD),
    ctypes.wintypes.DWORD,
    ctypes.POINTER(ctypes.wintypes.DWORD),
]
_kernel32.WriteConsoleInputW.restype = ctypes.wintypes.BOOL

_kernel32.GetStdHandle.argtypes = [ctypes.wintypes.DWORD]
_kernel32.GetStdHandle.restype = ctypes.wintypes.HANDLE

STD_INPUT_HANDLE = -10


class ChatConsole:
    """Vraie console cmd.exe visible chez le client, dédiée au chat.

    - L'utilisateur du client tape dans la console : le hook capte, bloque
      VK_RETURN, envoie au serveur, puis injecte "echo Toi: <msg>".
    - La GUI injecte "echo Auteur: <msg>" à la demande (via show_incoming).
    - Le [X] est neutralisé (sous-classe WndProc) : seule la GUI peut fermer.
    - Timeout de sécurité : si la console reste ouverte mais que le hook
      meurt (SetWindowsHookExW échoue), le [X] redevient fonctionnel.
    """

    # Timeout très large : l'utilisateur peut lire/attendre longtemps avant
    # de taper. Le hook doit rester actif tant que la console est vivante.
    SAFETY_TIMEOUT_SECONDS = 30 * 60.0

    def __init__(self, title: str, on_user_input):
        self.title = title
        self._on_user_input = on_user_input
        self.process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._line_buffer = ""

        self._hook_thread: threading.Thread | None = None
        self._hook_stop = threading.Event()
        self._hook_handle = None
        self._hook_proc = None

        self._wndproc_installed = False
        self._new_wndproc = None
        self._old_wndproc_ptr = None
        self._guarded_hwnd: int | None = None

        self._last_server_message = time.monotonic()

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------
    def open(self) -> bool:
        if platform.system() != "Windows":
            return False
        with self._lock:
            if self.process is not None and self.process.poll() is None:
                return True
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 1
            self.process = subprocess.Popen(
                ["cmd.exe", "/k", f"title {self.title} & echo === Chat distant === & echo."],
                creationflags=subprocess.CREATE_NEW_CONSOLE,
                startupinfo=startupinfo,
            )

        # Attend que la fenêtre de console soit réellement créée (retry).
        hwnd = None
        for _ in range(30):  # jusqu'à 3 s
            time.sleep(0.1)
            hwnd = self._find_hwnd()
            if hwnd:
                break
        if not hwnd:
            print(
                "[ChatConsole] Fenêtre de console introuvable après 3 s : "
                "le hook et l'injection GUI risquent de ne pas fonctionner.",
                file=sys.stderr,
            )

        self._install_close_guard(hwnd)
        self._start_hook()
        self.touch()
        return True

    def close(self) -> None:
        self._stop_hook()
        self._restore_close_guard()
        with self._lock:
            proc = self.process
            self.process = None
        if proc is None or proc.poll() is not None:
            return
        self._send_text("exit\r\n", force=True)
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.terminate()

    def is_open(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def touch(self) -> None:
        """Repousse le timeout de sécurité (message GUI reçu, frappe, etc.)."""
        self._last_server_message = time.monotonic()

    def is_hook_active(self) -> bool:
        """Le hook est actif tant que le thread vit et que la console est
        ouverte. Le timeout de sécurité (30 min) ne se déclenche que si le
        thread du hook est mort (SetWindowsHookExW a échoué) OU si la
        console est fermée — dans ces cas, le [X] doit redevenir
        fonctionnel pour ne pas piéger l'utilisateur."""
        if not self.is_open():
            return False
        if self._hook_thread is None or not self._hook_thread.is_alive():
            return False
        return (time.monotonic() - self._last_server_message) < self.SAFETY_TIMEOUT_SECONDS

    # ------------------------------------------------------------------
    # Injection par la GUI
    # ------------------------------------------------------------------
    def show_incoming(self, message: str) -> None:
        """Affiche un message venu de la GUI dans la console.

        Ne dépend PAS du hook clavier : tant que la console est ouverte, le
        message s'affiche (via AttachConsole + WriteConsoleInputW).
        """
        if not self.is_open():
            return
        self.touch()
        safe = (
            message.replace("^", "^^")
            .replace("&", "^&")
            .replace("|", "^|")
            .replace("<", "^<")
            .replace(">", "^>")
            .replace('"', "'")
        )
        self._send_text(f"echo Auteur: {safe}\r", force=True)

    # ------------------------------------------------------------------
    # Win32 : recherche du HWND par PID (fiable, indépendant du titre)
    # ------------------------------------------------------------------
    def _find_hwnd(self):
        proc = self.process
        if proc is None or proc.poll() is not None:
            return None
        target_pid = proc.pid
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def enum_proc(hwnd, _):
            pid = ctypes.wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == target_pid and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(enum_proc, 0)
        return found[0] if found else None

    # ------------------------------------------------------------------
    # Injection fiable via AttachConsole + WriteConsoleInputW
    # ------------------------------------------------------------------
    def _send_text(self, text: str, force: bool = False) -> None:
        """Injecte `text` dans la console via la file d'entrée de la console
        (AttachConsole + WriteConsoleInputW).

        Ne dépend ni du focus clavier ni de SendInput : écrit directement
        les événements clavier que cmd.exe va lire via ReadConsoleInput.
        `force` est conservé pour la compatibilité ; il n'est plus utilisé
        pour conditionner l'injection (is_open() suffit).
        """
        proc = self.process
        if proc is None or proc.poll() is not None:
            return

        # Prépare le tableau d'INPUT_RECORD : chaque caractère en down + up,
        # puis un VK_RETURN down + up.
        records = []
        for ch in text:
            if ch == "\n":
                continue
            if ch == "\r":
                records.append(self._make_key_event(VK_RETURN, 0, True))
                records.append(self._make_key_event(VK_RETURN, 0, False))
                continue
            code = ord(ch)
            records.append(self._make_key_event(0, code, True))
            records.append(self._make_key_event(0, code, False))

        if not records:
            return

        # AttachConsole permet d'obtenir le handle de la console cible.
        # IMPORTANT : FreeConsole d'abord, car le process Python peut déjà
        # être attaché à une autre console (celle qui a lancé le script).
        _kernel32.FreeConsole()
        if not _kernel32.AttachConsole(proc.pid):
            # Échec : on retombe sur SendInput (moins fiable, mais au cas où).
            self._send_text_via_sendinput(text)
            return

        try:
            handle = _kernel32.GetStdHandle(STD_INPUT_HANDLE)
            if not handle or handle == ctypes.wintypes.HANDLE(-1).value:
                self._send_text_via_sendinput(text)
                return
            arr = (INPUT_RECORD * len(records))(*records)
            written = ctypes.wintypes.DWORD(0)
            ok = _kernel32.WriteConsoleInputW(handle, arr, len(records), ctypes.byref(written))
            if not ok:
                self._send_text_via_sendinput(text)
        finally:
            _kernel32.FreeConsole()

    @staticmethod
    def _make_key_event(vk: int, ch: int, down: bool) -> "INPUT_RECORD":
        """Construit un INPUT_RECORD de type KEY_EVENT.

        - vk != 0 : touche spéciale (VK_RETURN, etc.), uChar = 0.
        - vk == 0 : caractère Unicode, wVirtualKeyCode = 0, uChar = ch.
        """
        rec = INPUT_RECORD()
        rec.EventType = KEY_EVENT
        rec.Event.KeyEvent.bKeyDown = bool(down)
        rec.Event.KeyEvent.wRepeatCount = 1
        rec.Event.KeyEvent.wVirtualKeyCode = vk
        rec.Event.KeyEvent.wVirtualScanCode = 0
        rec.Event.KeyEvent.uChar.UnicodeChar = chr(ch) if ch else "\x00"
        rec.Event.KeyEvent.dwControlKeyState = 0
        return rec

    def _send_text_via_sendinput(self, text: str) -> None:
        """Repli : SendInput classique (nécessite le focus clavier)."""
        hwnd = self._find_hwnd()
        if not hwnd:
            return
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)
        try:
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(0.05)
        inputs = []
        for ch in text:
            if ch == "\n":
                continue
            if ch == "\r":
                inputs.append(INPUT(type=1, u=INPUT._U(ki=KEYBDINPUT(0, VK_RETURN, 0, 0, 0))))
                inputs.append(INPUT(type=1, u=INPUT._U(ki=KEYBDINPUT(0, VK_RETURN, KEYEVENTF_KEYUP, 0, 0))))
                continue
            code = ord(ch)
            inputs.append(INPUT(type=1, u=INPUT._U(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0))))
            inputs.append(INPUT(type=1, u=INPUT._U(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0))))
        if inputs:
            arr = (INPUT * len(inputs))(*inputs)
            user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))

    # ------------------------------------------------------------------
    # Sous-classe WndProc : neutralise le [X]
    # ------------------------------------------------------------------
    def _install_close_guard(self, hwnd=None) -> None:
        if self._wndproc_installed:
            return
        if hwnd is None:
            hwnd = self._find_hwnd()
        if not hwnd:
            return
        self._guarded_hwnd = hwnd

        def new_wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_CLOSE and not self.is_hook_active():
                return _user32.CallWindowProcW(
                    self._old_wndproc_ptr, hwnd, msg, wparam, lparam
                )
            if msg == WM_CLOSE:
                return 0  # on ignore
            return _user32.CallWindowProcW(
                self._old_wndproc_ptr, hwnd, msg, wparam, lparam
            )

        self._new_wndproc = WNDPROC(new_wndproc)
        new_ptr = ctypes.cast(self._new_wndproc, ctypes.c_void_p)
        self._old_wndproc_ptr = _user32.SetWindowLongPtrW(
            hwnd, GWLP_WNDPROC, new_ptr
        )
        self._wndproc_installed = True

    def _restore_close_guard(self) -> None:
        if not self._wndproc_installed:
            return
        hwnd = self._guarded_hwnd or self._find_hwnd()
        if hwnd and self._old_wndproc_ptr:
            _user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, self._old_wndproc_ptr)
        self._wndproc_installed = False
        self._new_wndproc = None
        self._old_wndproc_ptr = None
        self._guarded_hwnd = None

    # ------------------------------------------------------------------
    # Hook clavier : capture les frappes, bloque VK_RETURN (Solution B)
    # ------------------------------------------------------------------
    def _start_hook(self) -> None:
        if self._hook_thread is not None and self._hook_thread.is_alive():
            return
        self._hook_stop = threading.Event()
        self._hook_thread = threading.Thread(target=self._hook_loop, daemon=True)
        self._hook_thread.start()

    def _stop_hook(self) -> None:
        self._hook_stop.set()
        if self._hook_handle:
            try:
                ctypes.windll.user32.UnhookWindowsHookEx(self._hook_handle)
            except Exception:
                pass
            self._hook_handle = None
        if self._hook_thread is not None:
            self._hook_thread.join(timeout=2)
            self._hook_thread = None

    def _hook_loop(self) -> None:
        user32 = ctypes.windll.user32

        HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.wintypes.WPARAM,
            ctypes.wintypes.LPARAM,
        )

        def hook_callback(n_code, w_param, l_param):
            if n_code >= 0 and w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                try:
                    if self._is_our_window_foreground():
                        info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                        vk = info.vkCode
                        if vk == VK_RETURN:
                            line = self._line_buffer
                            self._line_buffer = ""
                            self.touch()  # activité utilisateur : repousse le timeout
                            if line.strip():
                                self._on_user_input(line)
                            return 1  # bloque la propagation vers cmd.exe
                        if vk == VK_BACK:
                            self._line_buffer = self._line_buffer[:-1]
                            self.touch()
                            return user32.CallNextHookEx(
                                self._hook_handle, n_code, w_param, l_param
                            )
                        ch = self._vk_to_char(vk)
                        if ch:
                            self._line_buffer += ch
                            self.touch()
                except Exception:
                    pass
            return user32.CallNextHookEx(self._hook_handle, n_code, w_param, l_param)

        self._hook_proc = HOOKPROC(hook_callback)

        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            HOOKPROC,
            ctypes.wintypes.HINSTANCE,
            ctypes.wintypes.DWORD,
        ]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.wintypes.WPARAM,
            ctypes.wintypes.LPARAM,
        ]
        user32.CallNextHookEx.restype = ctypes.c_void_p
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        user32.UnhookWindowsHookEx.restype = ctypes.wintypes.BOOL

        # Pour WH_KEYBOARD_LL, hMod DOIT être NULL.
        self._hook_handle = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._hook_proc, None, 0
        )
        if not self._hook_handle:
            print(
                "[ChatConsole] SetWindowsHookExW a échoué : le hook clavier "
                "est inactif. Les messages de la GUI s'afficheront toutefois "
                "dans la console (AttachConsole + WriteConsoleInputW).",
                file=sys.stderr,
            )
            return

        msg = ctypes.wintypes.MSG()
        while not self._hook_stop.is_set():
            if user32.GetMessageW(ctypes.byref(msg), None, 0, 0) <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._hook_handle:
            try:
                user32.UnhookWindowsHookEx(self._hook_handle)
            except Exception:
                pass
            self._hook_handle = None

    def _is_our_window_foreground(self) -> bool:
        if not self.is_hook_active():
            return False
        fg = ctypes.windll.user32.GetForegroundWindow()
        if fg == 0:
            return False
        hwnd = self._find_hwnd()
        return hwnd is not None and hwnd == fg

    @staticmethod
    def _vk_to_char(vk: int):
        user32 = ctypes.windll.user32
        state = (ctypes.c_ubyte * 256)()
        if not user32.GetKeyboardState(state):
            return None
        layout = user32.GetKeyboardLayout(0)
        buf = ctypes.create_unicode_buffer(8)
        if user32.ToUnicodeEx(vk, 0, state, buf, 8, 0, layout) == 1:
            return buf[0]
        return None