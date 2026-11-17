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
"""

import ctypes
import ctypes.wintypes
import platform
import subprocess
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

# --- Prototypes Win32 (essentiel : sans eux, ctypes suppose c_int partout,
#     ce qui provoque OverflowError sur les pointeurs 64 bits) ---
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# SetWindowLongPtrW : (HWND, int, LONG_PTR) -> LONG_PTR
# LONG_PTR = pointeur de la taille d'un pointeur (c_void_p convient).
_user32.SetWindowLongPtrW.argtypes = [
    ctypes.wintypes.HWND,
    ctypes.c_int,
    ctypes.c_void_p,
]
_user32.SetWindowLongPtrW.restype = ctypes.c_void_p

# CallWindowProcW : (WNDPROC, HWND, UINT, WPARAM, LPARAM) -> LRESULT
# WNDPROC est un pointeur de fonction ; LRESULT = LONG_PTR.
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_void_p,  # LRESULT (LONG_PTR)
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
)
_user32.CallWindowProcW.argtypes = [
    ctypes.c_void_p,  # WNDPROC
    ctypes.wintypes.HWND,
    ctypes.wintypes.UINT,
    ctypes.wintypes.WPARAM,
    ctypes.wintypes.LPARAM,
]
_user32.CallWindowProcW.restype = ctypes.c_void_p


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

    - L'utilisateur du client tape dans la console : le hook capte, bloque
      VK_RETURN, envoie au serveur, puis injecte "echo Toi: <msg>".
    - La GUI injecte "echo Auteur: <msg>" à la demande (via show_incoming).
    - Le [X] est neutralisé (sous-classe WndProc) : seule la GUI peut fermer.
    - Timeout de sécurité : si aucun message serveur depuis 60 s, le hook
      se désactive et le [X] redevient fonctionnel (évite le blocage en cas
      de perte réseau).
    """

    SAFETY_TIMEOUT_SECONDS = 60.0

    def __init__(self, title: str, on_user_input):
        self.title = title
        self._on_user_input = on_user_input
        self.process: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._line_buffer = ""

        self._hook_thread: threading.Thread | None = None
        self._hook_stop = threading.Event()
        self._hook_handle = None
        self._hook_proc = None  # garde une référence pour éviter le GC

        self._wndproc_installed = False
        self._new_wndproc = None      # objet WNDPROC (callable)
        self._old_wndproc_ptr = None  # pointeur LONG_PTR retourné par SetWindowLongPtrW

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
            time.sleep(0.7)  # laisse la console s'ouvrir

        self._install_close_guard()
        self._start_hook()
        self.touch()  # initialise le timeout de sécurité
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
        """Signale qu'un message serveur vient d'arriver. Repousse le timeout."""
        self._last_server_message = time.monotonic()

    def is_hook_active(self) -> bool:
        """Le hook est actif tant qu'on reçoit des messages du serveur.
        Après SAFETY_TIMEOUT_SECONDS sans message, il se désactive et le [X]
        redevient fonctionnel."""
        if self._hook_thread is None or not self._hook_thread.is_alive():
            return False
        return (time.monotonic() - self._last_server_message) < self.SAFETY_TIMEOUT_SECONDS

    # ------------------------------------------------------------------
    # Injection par la GUI
    # ------------------------------------------------------------------
    def show_incoming(self, message: str) -> None:
        """Affiche un message venu de la GUI dans la console.
        Format demandé : 'Auteur: <message>'."""
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
        self._send_text(f"echo Auteur: {safe}\r")

    # ------------------------------------------------------------------
    # Win32 : focus, SendInput, WndProc
    # ------------------------------------------------------------------
    def _find_hwnd(self):
        user32 = ctypes.windll.user32
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
        def enum_proc(hwnd, _):
            n = user32.GetWindowTextLengthW(hwnd)
            if n == 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if buf.value.startswith(self.title) and user32.IsWindowVisible(hwnd):
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(enum_proc, 0)
        return found[0] if found else None

    def _send_text(self, text: str, force: bool = False) -> None:
        """Injecte `text` via SendInput. `force=True` court-circuite la
        vérification du timeout (utilisé pour `exit`)."""
        if not force and not self.is_hook_active():
            return
        hwnd = self._find_hwnd()
        if not hwnd:
            return
        user32 = ctypes.windll.user32
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)
        user32.SetForegroundWindow(hwnd)
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
    def _install_close_guard(self) -> None:
        if self._wndproc_installed:
            return
        hwnd = self._find_hwnd()
        if not hwnd:
            return

        def new_wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_CLOSE and not self.is_hook_active():
                # Timeout de sécurité dépassé : on laisse la fermeture passer.
                return _user32.CallWindowProcW(
                    self._old_wndproc_ptr, hwnd, msg, wparam, lparam
                )
            if msg == WM_CLOSE:
                return 0  # on ignore
            return _user32.CallWindowProcW(
                self._old_wndproc_ptr, hwnd, msg, wparam, lparam
            )

        # Instancie le callback C. On garde une référence forte pour éviter le GC.
        self._new_wndproc = WNDPROC(new_wndproc)

        # Convertit l'objet WNDPROC en c_void_p pour SetWindowLongPtrW.
        # ctypes.cast(WNDPROC, c_void_p).value donne un int Python, qui
        # peut dépasser c_int sur 64 bits. On passe donc directement un
        # c_void_p (créé via ctypes.cast) : SetWindowLongPtrW.argtypes
        # déclare le 3e argument comme c_void_p, ce qui accepte la valeur.
        new_ptr = ctypes.cast(self._new_wndproc, ctypes.c_void_p)

        # Remplace le WndProc de la console.
        self._old_wndproc_ptr = _user32.SetWindowLongPtrW(
            hwnd, GWLP_WNDPROC, new_ptr
        )
        self._wndproc_installed = True

    def _restore_close_guard(self) -> None:
        if not self._wndproc_installed:
            return
        hwnd = self._find_hwnd()
        if hwnd and self._old_wndproc_ptr:
            _user32.SetWindowLongPtrW(hwnd, GWLP_WNDPROC, self._old_wndproc_ptr)
        self._wndproc_installed = False
        self._new_wndproc = None
        self._old_wndproc_ptr = None

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
        kernel32 = ctypes.windll.kernel32

        HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_void_p,  # LRESULT (LONG_PTR)
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
                            if line.strip():
                                self._on_user_input(line)
                            # Solution B : on bloque la propagation vers cmd.exe
                            return 1
                        if vk == VK_BACK:
                            self._line_buffer = self._line_buffer[:-1]
                            return user32.CallNextHookEx(self._hook_handle, n_code, w_param, l_param)
                        ch = self._vk_to_char(vk)
                        if ch:
                            self._line_buffer += ch
                except Exception:
                    pass
            return user32.CallNextHookEx(self._hook_handle, n_code, w_param, l_param)

        self._hook_proc = HOOKPROC(hook_callback)
        # SetWindowsHookExW.argtypes : sans cela, ctypes suppose c_int et
        # peut produire des OverflowError sur les pointeurs 64 bits.
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
            WH_KEYBOARD_LL, self._hook_proc, kernel32.GetModuleHandleW(None), 0
        )
        if not self._hook_handle:
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