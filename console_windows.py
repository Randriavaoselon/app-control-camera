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

CORRECTIFS v3 :
  - VK_RETURN porte wVirtualScanCode = 0x1C et uChar.UnicodeChar = '\\r'.
    Sans cela, cmd.exe ignore le "Enter" et le texte reste collé au prompt
    (le client ne peut pas répondre sur une nouvelle ligne).
  - L'injection utilise d'abord SendInput (focus clavier forcé via
    SetForegroundWindow + SetFocus + AttachThreadInput), avec repli sur
    AttachConsole + WriteConsoleInputW si SendInput échoue.
  - AttachConsole/FreeConsole ne sont plus appelés à chaque injection :
    on garde l'attache tant que la console vit (_attached_pid). Cela évite
    les échecs ERROR_ACCESS_DENIED après fermeture/réouverture.
  - _find_hwnd() identifie la console par PID, pas par titre.
  - open() attend la création réelle de la fenêtre (retry jusqu'à 3 s).
  - is_hook_active() ne dépend plus d'un timeout court : tant que la
    console est ouverte et le thread du hook vivant, il reste actif.
    Le [X] reste neutralisé ; un timeout très large (30 min) sert de
    garde-fou en cas de perte réseau prolongée.
  - Le hook appelle touch() à chaque frappe : l'activité utilisateur
    maintient le hook actif.
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
KEY_EVENT = 0x0001
STD_INPUT_HANDLE = -10
SCANCODE_RETURN = 0x1C   # scan code standard de la touche Entrée


# --- Prototypes Win32 ---
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_user32.SetWindowLongPtrW.argtypes = [
    ctypes.wintypes.HWND,
    ctypes.c_int,
    ctypes.c_void_p,
]
_user32.SetWindowLongPtrW.restype = ctypes.c_void_p

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

_user32.GetWindowThreadProcessId.argtypes = [
    ctypes.wintypes.HWND, ctypes.POINTER(ctypes.wintypes.DWORD),
]
_user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD

_kernel32.AttachConsole.argtypes = [ctypes.wintypes.DWORD]
_kernel32.AttachConsole.restype = ctypes.wintypes.BOOL
_kernel32.FreeConsole.argtypes = []
_kernel32.FreeConsole.restype = ctypes.wintypes.BOOL

_kernel32.GetModuleHandleW.argtypes = [ctypes.wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = ctypes.wintypes.HMODULE

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


class ChatConsole:
    """Vraie console cmd.exe visible chez le client, dédiée au chat."""

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

        # PID de la console à laquelle ce process Python est actuellement
        # attaché (via AttachConsole). None = pas attaché.
        self._attached_pid: int | None = None

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

        # Attend que la fenêtre de console soit réellement créée.
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

        self._install_close_guard(hwnd)
        self._start_hook()
        self.touch()
        return True

    def close(self) -> None:
        self._detach_console()
        self._stop_hook()
        self._restore_close_guard()
        with self._lock:
            proc = self.process
            self.process = None
        if proc is None or proc.poll() is not None:
            return
        # Demande de fermeture propre (sans injection : on kill directement
        # si la console est déjà fermée par l'utilisateur).
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
        """Actif tant que la console est ouverte et le thread du hook vivant.
        Le timeout (30 min) n'est qu'un garde-fou contre un blocage prolongé
        sans aucun message serveur ni frappe utilisateur."""
        if not self.is_open():
            return False
        if self._hook_thread is None or not self._hook_thread.is_alive():
            return False
        return (time.monotonic() - self._last_server_message) < self.SAFETY_TIMEOUT_SECONDS

    # ------------------------------------------------------------------
    # Injection par la GUI
    # ------------------------------------------------------------------
    def show_incoming(self, message: str) -> None:
        """Affiche un message venu de la GUI dans la console, PUIS place le
        curseur sur une nouvelle ligne prête à recevoir la frappe.

        Format : 'Auteur: <message>' — le '\\r' final déclenche l'exécution
        de 'echo' et réaffiche un prompt vierge."""
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
    # Recherche HWND par PID
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
    # Injection : SendInput (focus forcé) + repli AttachConsole
    # ------------------------------------------------------------------
    def _send_text(self, text: str, force: bool = False) -> None:
        """Injecte `text` dans la console.

        Stratégie :
          1. SendInput avec focus clavier forcé (SetForegroundWindow +
             AttachThreadInput + SetFocus). Méthode fiable quand HWND est
             correct, ne nécessite pas FreeConsole.
          2. Si SendInput échoue, repli sur AttachConsole + WriteConsoleInputW
             (attache persistante, pas de FreeConsole à chaque appel).
        """
        proc = self.process
        if proc is None or proc.poll() is not None:
            return

        if self._send_text_via_sendinput(text):
            return
        self._send_text_via_attach(text)

    def _send_text_via_sendinput(self, text: str) -> bool:
        """SendInput avec focus clavier forcé. Retourne True si tout est OK."""
        hwnd = self._find_hwnd()
        if not hwnd:
            return False
        user32 = ctypes.windll.user32
        try:
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            # Force le focus clavier de façon robuste.
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
        time.sleep(0.08)  # laisse le focus s'établir

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

    def _send_text_via_attach(self, text: str) -> None:
        """Repli : AttachConsole + WriteConsoleInputW.

        L'attache est PERSISTANTE (pas de FreeConsole après l'écriture) :
        cela évite les échecs ERROR_ACCESS_DENIED quand on rouvre la
        console et qu'on rappelle _send_text. L'attache est libérée dans
        close() via _detach_console().
        """
        proc = self.process
        if proc is None or proc.poll() is not None:
            return

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

        if self._attached_pid != proc.pid:
            # Détache d'une éventuelle console précédente, puis attache.
            try:
                _kernel32.FreeConsole()
            except Exception:
                pass
            if not _kernel32.AttachConsole(proc.pid):
                # Impossible de s'attacher : dernier recours SendInput.
                self._send_text_via_sendinput(text)
                return
            self._attached_pid = proc.pid

        try:
            handle = _kernel32.GetStdHandle(STD_INPUT_HANDLE)
            if not handle or handle == ctypes.wintypes.HANDLE(-1).value:
                self._send_text_via_sendinput(text)
                return
            arr = (INPUT_RECORD * len(records))(*records)
            written = ctypes.wintypes.DWORD(0)
            _kernel32.WriteConsoleInputW(handle, arr, len(records), ctypes.byref(written))
        except Exception:
            pass

    def _detach_console(self) -> None:
        if self._attached_pid is not None:
            try:
                _kernel32.FreeConsole()
            except Exception:
                pass
            self._attached_pid = None

    @staticmethod
    def _make_key_event(vk: int, ch: int, down: bool) -> "INPUT_RECORD":
        """Construit un INPUT_RECORD KEY_EVENT.

        Pour VK_RETURN : wVirtualScanCode = 0x1C ET uChar = '\\r' — SANS
        cela, cmd.exe ignore le "Enter" et le texte reste sur la même
        ligne que le prompt (le client ne peut pas répondre sur une
        nouvelle ligne).
        """
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
        # Poste un message à nous-mêmes pour débloquer GetMessageW.
        try:
            ctypes.windll.user32.PostThreadMessageW(
                ctypes.windll.kernel32.GetCurrentThreadId(), 0x0012, 0, 0  # WM_QUIT
            )
        except Exception:
            pass
        if self._hook_thread is not None:
            self._hook_thread.join(timeout=2)
            self._hook_thread = None

    def _hook_loop(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

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

        self._hook_handle = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, self._hook_proc, None, 0
        )
        if not self._hook_handle:
            print(
                "[ChatConsole] SetWindowsHookExW a échoué : le hook clavier est inactif.",
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