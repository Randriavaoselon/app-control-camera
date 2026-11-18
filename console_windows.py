"""
Console chat visible chez le client (vraie cmd.exe) + capture des frappes.

Deux responsabilités :
  1. Ouvrir une vraie console cmd.exe visible, titrée, avec le [X] neutralisé.
  2. Capturer les frappes de l'utilisateur DANS cette console via un hook
     clavier bas niveau (WH_KEYBOARD_LL), et bloquer VK_RETURN pour que
     cmd.exe ne reçoive jamais la ligne (Solution B — préfixe invisible).

Le CMD Commandes n'est PAS dans ce module.

Windows uniquement.

CORRECTIFS v7 :
  - MARQUEUR SendInput (dwExtraInfo = MAGIC_EXTRA_INFO) : le hook IGNORE
    les événements injectés par nos propres SendInput. Sans cela, le '\\r'
    de "echo Auteur: ..." / "echo Toi: ..." était capté par le hook et
    bloqué → cmd.exe n'exécutait jamais l'echo, le texte restait collé au
    prompt, et la frappe suivante était exécutée comme commande
    ("n'est pas reconnu en tant que commande interne").
  - THREAD SÉPARÉ pour _on_user_input : le hook ne fait plus requests.post
    (bloquant jusqu'à 5 s). Il dépose la ligne dans une file, un thread
    dédié l'envoie au serveur puis injecte l'écho local. Le hook reste
    réactif en toutes circonstances (plus de VK_RETURN non bloqué en cas
    de latence réseau).
  - INJECTION : SendInput avec focus forcé en premier. Si SendInput échoue,
    repli sur AttachConsole + WriteConsoleInputW avec uChar='\\r' pour
    VK_RETURN, puis FreeConsole immédiat (pas d'attachement résiduel).
  - CTRL_CLOSE_EVENT ignoré via SetConsoleCtrlHandler (le process Python
    survit à la fermeture du CMD par l'utilisateur).
  - WndProc : WM_CLOSE bloqué INCONDITIONNELLEMENT ([X], Alt+F4 impossibles).
    Seul close() (sur ordre de server_gui) ferme la console, via
    proc.terminate() qui contourne le WndProc.
  - WATCHDOG : relance cmd.exe en < 300 ms si l'utilisateur arrive quand
    même à le fermer (taskkill, gestionnaire de tâches, exit, crash).
  - _find_hwnd() par PID (fiable, indépendant du titre).
  - open() attend la création réelle de la fenêtre.
  - is_hook_active() ne dépend plus d'un timeout court (30 min garde-fou).
"""

import ctypes
import ctypes.wintypes
import platform
import queue
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
KEY_EVENT = 0x0001
STD_INPUT_HANDLE = -10

WATCHDOG_INTERVAL = 0.3

# Marqueur injecté dans dwExtraInfo de nos propres SendInput. Le hook
# reconnaît ce marqueur et ignore l'événement (il ne le bloque pas), ce qui
# permet à "echo Auteur: ...\r" et "echo Toi: ...\r" d'atteindre cmd.exe.
MAGIC_EXTRA_INFO = 0x1A2B3C4D


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

_kernel32.AttachConsole.argtypes = [ctypes.wintypes.DWORD]
_kernel32.AttachConsole.restype = ctypes.wintypes.BOOL
_kernel32.FreeConsole.argtypes = []
_kernel32.FreeConsole.restype = ctypes.wintypes.BOOL
_kernel32.GetStdHandle.argtypes = [ctypes.wintypes.DWORD]
_kernel32.GetStdHandle.restype = ctypes.wintypes.HANDLE


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

_CTRL_HANDLER_INSTALLED = False


def _install_ctrl_handler() -> None:
    """Ignore CTRL_CLOSE_EVENT : même si on est attaché à une console qui se
    ferme, le process Python survit. C'est ce qui empêche la déconnexion du
    client quand l'utilisateur ferme le CMD (par taskkill par exemple)."""
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


class ChatConsole:
    """Vraie console cmd.exe visible chez le client, dédiée au chat.

    Seul server_gui peut fermer la console (via close()). Toute fermeture
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

        # File d'événements utilisateur : le hook ne fait que déposer la
        # ligne ici. Un thread dédié l'envoie au serveur (requests.post
        # bloquant) SANS bloquer le hook. C'est ce qui empêche le bug
        # "test n'est pas reconnu en tant que commande interne" : sans
        # cela, le hook était bloqué dans requests.post et VK_RETURN
        # n'était plus bloqué pour les frappes suivantes.
        self._user_input_queue: "queue.Queue[str]" = queue.Queue(maxsize=100)
        self._input_thread: threading.Thread | None = None

        self._wndproc_installed = False
        self._new_wndproc = None
        self._old_wndproc_ptr = None
        self._guarded_hwnd: int | None = None

        self._last_server_message = time.monotonic()

        # Watchdog
        self._watchdog_thread: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        self._wanted_open = False
        self._closed_by_gui = False

        _install_ctrl_handler()

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
        for _ in range(30):
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
            time.sleep(0.15)

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
    # Watchdog
    # ------------------------------------------------------------------
    def _start_watchdog(self) -> None:
        if self._watchdog_thread is not None and self._watchdog_thread.is_alive():
            return
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()

    def _stop_watchdog(self) -> None:
        self._watchdog_stop.set()
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=1)
            self._watchdog_thread = None

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.is_set():
            try:
                if self._wanted_open and not self._closed_by_gui and not self.is_open():
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
    # Injection : SendInput prioritaire, repli AttachConsole+WriteConsoleInputW
    # ------------------------------------------------------------------
    def _send_text(self, text: str, force: bool = False) -> bool:
        proc = self.process
        if proc is None or proc.poll() is not None:
            return False

        # 1) SendInput avec focus forcé. Les événements portent
        #    MAGIC_EXTRA_INFO : le hook les ignore, donc le '\r' de
        #    "echo Auteur: ...\r" atteint bien cmd.exe et exécute l'echo.
        if self._send_text_via_sendinput(text):
            return True

        # 2) Repli : AttachConsole + WriteConsoleInputW. Pas de marqueur
        #    dwExtraInfo possible ici (writeConsoleInput n'en a pas), mais
        #    le hook ne voit que les événements issus de SendInput, donc
        #    ce repli est safe.
        return self._send_text_via_attach(text)

    def _send_text_via_sendinput(self, text: str) -> bool:
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
                inputs.append(INPUT(type=1, u=INPUT._U(ki=KEYBDINPUT(
                    VK_RETURN, SCANCODE_RETURN, 0, 0, MAGIC_EXTRA_INFO))))
                inputs.append(INPUT(type=1, u=INPUT._U(ki=KEYBDINPUT(
                    VK_RETURN, SCANCODE_RETURN, KEYEVENTF_KEYUP, 0, MAGIC_EXTRA_INFO))))
                continue
            code = ord(ch)
            inputs.append(INPUT(type=1, u=INPUT._U(ki=KEYBDINPUT(
                0, code, KEYEVENTF_UNICODE, 0, MAGIC_EXTRA_INFO))))
            inputs.append(INPUT(type=1, u=INPUT._U(ki=KEYBDINPUT(
                0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, MAGIC_EXTRA_INFO))))
        if not inputs:
            return True
        arr = (INPUT * len(inputs))(*inputs)
        sent = user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))
        return sent == len(inputs)

    def _send_text_via_attach(self, text: str) -> bool:
        proc = self.process
        if proc is None or proc.poll() is not None:
            return False

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
            return True

        try:
            _kernel32.FreeConsole()
        except Exception:
            pass
        if not _kernel32.AttachConsole(proc.pid):
            return False
        try:
            handle = _kernel32.GetStdHandle(STD_INPUT_HANDLE)
            if not handle or handle == ctypes.wintypes.HANDLE(-1).value:
                return False
            arr = (INPUT_RECORD * len(records))(*records)
            written = ctypes.wintypes.DWORD(0)
            ok = _kernel32.WriteConsoleInputW(handle, arr, len(records), ctypes.byref(written))
            return bool(ok)
        except Exception:
            return False
        finally:
            try:
                _kernel32.FreeConsole()
            except Exception:
                pass

    @staticmethod
    def _make_key_event(vk: int, ch: int, down: bool) -> "INPUT_RECORD":
        """VK_RETURN porte wVirtualScanCode=0x1C ET uChar='\\r' — sans cela,
        cmd.exe ignore le "Enter" et le texte reste collé au prompt."""
        rec = INPUT_RECORD()
        rec.EventType = KEY_EVENT
        rec.Event.KeyEvent.bKeyDown = bool(down)
        rec.Event.KeyEvent.wRepeatCount = 1
        rec.Event.KeyEvent.wVirtualKeyCode = vk
        if vk == VK_RETURN:
            rec.Event.KeyEvent.wVirtualScanCode = SCANCODE_RETURN
            rec.Event.KeyEvent.uChar.UnicodeChar = "\r"
        else:
            rec.Event.KeyEvent.wVirtualScanCode = 0
            rec.Event.KeyEvent.uChar.UnicodeChar = chr(ch) if ch else "\x00"
        rec.Event.KeyEvent.dwControlKeyState = 0
        return rec

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
            if msg == WM_CLOSE:
                return 0
            return _user32.CallWindowProcW(
                self._old_wndproc_ptr, hwnd, msg, wparam, lparam
            )

        self._new_wndproc = WNDPROC(new_wndproc)
        new_ptr = ctypes.cast(self._new_wndproc, ctypes.c_void_p)
        self._old_wndproc_ptr = _user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, new_ptr)
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
    # Hook clavier + dispatcher d'entrée utilisateur
    # ------------------------------------------------------------------
    def _start_hook(self) -> None:
        if self._hook_thread is not None and self._hook_thread.is_alive():
            return
        self._hook_stop = threading.Event()
        self._hook_thread = threading.Thread(target=self._hook_loop, daemon=True)
        self._hook_thread.start()
        # Thread dédié pour _on_user_input (requests.post bloquant).
        self._input_thread = threading.Thread(target=self._input_dispatcher_loop, daemon=True)
        self._input_thread.start()

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
        if self._input_thread is not None:
            self._input_thread.join(timeout=2)
            self._input_thread = None

    def _input_dispatcher_loop(self) -> None:
        """Consomme la file des lignes tapées par l'utilisateur et appelle
        _on_user_input HORS du thread du hook. requests.post peut prendre
        jusqu'à 5 s : le hook reste réactif, VK_RETURN reste bloqué."""
        while not self._hook_stop.is_set():
            try:
                line = self._user_input_queue.get(timeout=0.3)
            except queue.Empty:
                continue
            try:
                self._on_user_input(line)
            except Exception:
                pass

    def _hook_loop(self) -> None:
        user32 = ctypes.windll.user32

        HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_void_p, ctypes.c_int,
            ctypes.wintypes.WPARAM, ctypes.wintypes.LPARAM,
        )

        def hook_callback(n_code, w_param, l_param):
            if n_code >= 0 and w_param in (WM_KEYDOWN, WM_SYSKEYDOWN):
                try:
                    # Événements injectés par nos propres SendInput :
                    # on les laisse passer SANS les traiter. Sans cela,
                    # le '\r' de "echo Auteur: ...\r" / "echo Toi: ...\r"
                    # serait bloqué et cmd.exe n'exécuterait jamais
                    # l'echo → le texte resterait collé au prompt, puis
                    # serait exécuté comme commande à la frappe suivante.
                    info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                    if info.dwExtraInfo == MAGIC_EXTRA_INFO:
                        return user32.CallNextHookEx(
                            self._hook_handle, n_code, w_param, l_param
                        )

                    if self._is_our_window_foreground():
                        vk = info.vkCode
                        if vk == VK_RETURN:
                            line = self._line_buffer
                            self._line_buffer = ""
                            self.touch()
                            if line.strip():
                                # Dépose dans la file ; le thread dispatcher
                                # envoie au serveur et injecte l'écho local.
                                try:
                                    self._user_input_queue.put_nowait(line)
                                except queue.Full:
                                    pass
                            return 1  # bloque VK_RETURN pour cmd.exe
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