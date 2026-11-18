"""
Console chat visible chez le client (vraie cmd.exe) + capture des frappes.

Deux responsabilités :
  1. Ouvrir une vraie console cmd.exe visible, titrée, avec le [X] neutralisé.
  2. Capturer les frappes de l'utilisateur DANS cette console via un hook
     clavier bas niveau (WH_KEYBOARD_LL), et bloquer VK_RETURN pour que
     cmd.exe ne reçoive jamais la ligne (Solution B — préfixe invisible).

Le CMD Commandes n'est PAS dans ce module.

Windows uniquement.

CORRECTIFS v5 :
  - Le [X] / Alt+F4 sont bloqués INCONDITIONNELLEMENT par le WndProc
    (WM_CLOSE ignoré sauf si _allow_close est vrai). Seul close() appelé
    par le client (sur ordre de server_gui) peut fermer la console.
  - Un WATCHDOG relance automatiquement cmd.exe si l'utilisateur arrive
    quand même à le fermer (taskkill, gestionnaire de tâches, `exit`,
    crash...). Tant que _wanted_open est vrai et que _closed_by_gui est
    faux, le CMD est recréé en moins de 300 ms.
  - L'injection utilise UNIQUEMENT SendInput avec focus forcé (aucun
    AttachConsole, qui tuait le process Python à la fermeture du CMD).
  - SetConsoleCtrlHandler ignore CTRL_CLOSE_EVENT (ceinture de sécurité).
  - VK_RETURN porte wVirtualScanCode=0x1C et uChar='\\r'.
  - _find_hwnd() par PID (fiable, indépendant du titre).
  - open() attend la création réelle de la fenêtre.
  - is_hook_active() ne dépend plus d'un timeout court.
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
SCANCODE_RETURN = 0x1C
CTRL_CLOSE_EVENT = 2

# Intervalle du watchdog (secondes) : latence max avant relance du CMD.
WATCHDOG_INTERVAL = 0.3


# --- Prototypes Win32 ---
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_user32.SetWindowLongPtrW.argtypes = [ctypes.wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
_user32.SetWindowLongPtrW.restype = ctypes.c_void_p

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_void_p, ctypes.wintypes.HWND, ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
)
_user32.CallWindowProcW.argtypes = [
    ctypes.c_void_p, ctypes.wintypes.HWND, ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
]
_user32.CallWindowProcW.restype = ctypes.c_void_p

_user32.GetWindowThreadProcessId.argtypes = [
    ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD),
]
_user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD

_kernel32.GetModuleHandleW.argtypes = [ctypes.wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = ctypes.wintypes.HMODULE

PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(ctypes.wintypes.BOOL, ctypes.wintypes.DWORD)
_kernel32.SetConsoleCtrlHandler.argtypes = [PHANDLER_ROUTINE, ctypes.wintypes.BOOL]
_kernel32.SetConsoleCtrlHandler.restype = ctypes.wintypes.BOOL

_CTRL_HANDLER_INSTALLED = False


def _install_ctrl_handler() -> None:
    global _CTRL_HANDLER_INSTALLED
    if _CTRL_HANDLER_INSTALLED:
        return

    @PHANDLER_ROUTINE
    def _handler(ctrl_type):
        if ctrl_type == CTRL_CLOSE_EVENT:
            return True
        return False

    try:
        _kernel32.SetConsoleCtrlHandler(_handler, True)
        _CTRL_HANDLER_INSTALLED = True
    except Exception:
        pass
    _install_ctrl_handler._ref = _handler


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


class ChatConsole:
    """Vraie console cmd.exe visible chez le client, dédiée au chat.

    Seul le server_gui peut fermer la console (via close()). Toute fermeture
    utilisateur ([X], Alt+F4, taskkill, exit, crash) est soit bloquée, soit
    aussitôt compensée par la relance automatique (watchdog).
    """

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

        # Watchdog
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        self._wanted_open = False        # la GUI veut-elle la console ouverte ?
        self._closed_by_gui = False      # close() (GUI) a-t-il fermé la console ?

        _install_ctrl_handler()

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------
    def open(self) -> bool:
        if platform.system() != "Windows":
            return False
        with self._lock:
            self._wanted_open = True
            self._closed_by_gui = False
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

        hwnd = None
        for _ in range(30):  # jusqu'à 3 s
            time.sleep(0.1)
            hwnd = self._find_hwnd()
            if hwnd:
                break
        if not hwnd:
            print(
                "[ChatConsole] Fenêtre de console introuvable après 3 s.",
                file=sys.stderr,
            )
        else:
            time.sleep(0.15)  # laisse la console prête à recevoir le focus

        self._install_close_guard(hwnd)
        self._start_hook()
        self._start_watchdog()
        self.touch()
        return True

    def close(self) -> None:
        """Fermeture demandée par la GUI. Seul chemin autorisé."""
        with self._lock:
            self._wanted_open = False
            self._closed_by_gui = True
            proc = self.process
        self._stop_watchdog()
        self._stop_hook()
        self._restore_close_guard()
        with self._lock:
            self.process = None
        if proc is None or proc.poll() is not None:
            return
        # Fermeture directe : contourne le WndProc (qui bloque WM_CLOSE).
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def is_open(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def touch(self) -> None:
        self._last_server_message = time.monotonic()

    def is_hook_active(self) -> bool:
        if not self.is_open():
            return False
        if self._hook_thread is None or not self._hook_thread.is_alive():
            return False
        return (time.monotonic() - self._last_server_message) < self.SAFETY_TIMEOUT_SECONDS

    # ------------------------------------------------------------------
    # Watchdog : relance le CMD si l'utilisateur arrive à le fermer
    # ------------------------------------------------------------------
    def _start_watchdog(self) -> None:
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True
        )
        self._watchdog_thread.start()

    def _stop_watchdog(self) -> None:
        self._watchdog_stop.set()
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=1)
            self._watchdog_thread = None

    def _watchdog_loop(self) -> None:
        """Tant que _wanted_open est vrai et que la GUI n'a pas demandé la
        fermeture, recrée le CMD dès qu'il meurt (taskkill, exit, crash,
        gestionnaire de tâches, etc.)."""
        while not self._watchdog_stop.is_set():
            try:
                if self._wanted_open and not self._closed_by_gui and not self.is_open():
                    # L'utilisateur a réussi à fermer la console : on la relance.
                    # open() est idempotent et protégé par _lock.
                    self.open()
            except Exception:
                pass
            self._watchdog_stop.wait(WATCHDOG_INTERVAL)

    # ------------------------------------------------------------------
    def show_incoming(self, message: str) -> None:
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
    # Injection : SendInput UNIQUEMENT
    # ------------------------------------------------------------------
    def _send_text(self, text: str, force: bool = False) -> bool:
        proc = self.process
        if proc is None or proc.poll() is not None:
            return False

        hwnd = self._find_hwnd()
        if not hwnd:
            return False

        user32 = ctypes.windll.user32
        try:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)
            fg = user32.GetForegroundWindow()
            fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
            tgt_thread = user32.GetWindowThreadProcessId(hwnd, None)
            if fg_thread and tgt_thread and fg_thread != tgt_thread:
                user32.AttachThreadInput(fg_thread, tgt_thread, True)
                try:
                    user32.SetForegroundWindow(hwnd)
                    user32.SetFocus(hwnd)
                finally:
                    user32.AttachThreadInput(fg_thread, tgt_thread, False)
            else:
                user32.SetForegroundWindow(hwnd)
                user32.SetFocus(hwnd)
        except Exception:
            return False
        time.sleep(0.08)

        inputs = []
        for ch in text:
            if ch == "\n":
                continue
            if ch == "\r":
                inputs.append(INPUT(type=1, u=INPUT._U(ki=KEYBDINPUT(VK_RETURN, SCANCODE_RETURN, 0, 0, 0))))
                inputs.append(INPUT(type=1, u=INPUT._U(ki=KEYBDINPUT(VK_RETURN, SCANCODE_RETURN, KEYEVENTF_KEYUP, 0, 0))))
                continue
            code = ord(ch)
            inputs.append(INPUT(type=1, u=INPUT._U(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0))))
            inputs.append(INPUT(type=1, u=INPUT._U(ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0))))
        if not inputs:
            return True
        arr = (INPUT * len(inputs))(*inputs)
        sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
        return sent == len(inputs)

    # ------------------------------------------------------------------
    # WndProc : bloque WM_CLOSE inconditionnellement
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
            # Blocage INCONDITIONNEL de WM_CLOSE : ni [X], ni Alt+F4,
            # ni aucune autre tentative utilisateur ne peut fermer la
            # console. Seul close() (appelé par le client sur ordre de
            # server_gui) y arrive — et il le fait par proc.terminate(),
            # qui ne passe PAS par ce WndProc.
            if msg == WM_CLOSE:
                return 0
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
    # Hook clavier
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
            ctypes.c_void_p, ctypes.c_int,
            ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
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
                            self.touch()
                            if line.strip():
                                self._on_user_input(line)
                            return 1
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
            ctypes.c_int, HOOKPROC,
            ctypes.wintypes.HINSTANCE, ctypes.wintypes.DWORD,
        ]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p, ctypes.c_int,
            ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
        ]
        user32.CallNextHookEx.restype = ctypes.c_void_p
        user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        user32.UnhookWindowsHookEx.restype = ctypes.wintypes.BOOL

        self._hook_handle = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._hook_proc, None, 0
        )
        if not self._hook_handle:
            print(
                "[ChatConsole] SetWindowsHookExW a échoué.",
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