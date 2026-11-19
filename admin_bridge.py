"""
Pont élevé : lancé par client.py via ShellExecuteW(..., "runas", ...),
ce qui déclenche le popup UAC natif de Windows chez le client.

Une fois élevé, ce script :
  1. se connecte au socket localhost ouvert par client.py ;
  2. lance un cmd.exe élevé (hérite des droits admin) avec pipes,
     SANS FENÊTRE VISIBLE (CREATE_NO_WINDOW) ;
  3. fait le pont bidirectionnel :
       - stdin du cmd  <-  socket
       - stdout+stderr du cmd  ->  socket

Protocole sur le socket (texte UTF-8, lignes terminées par \n) :
  - "IN:<base64>\n"  : entrée à écrire dans stdin du cmd (base64 pour
                       éviter tout problème de caractères spéciaux)
  - "OUT:<base64>\n" : sortie du cmd (base64 pour transporter sans
                       risque les \n, \r, et octets non-UTF8)
  - "EXIT\n"         : fin de vie du cmd
  - "BYE\n"          : le client demande la fermeture

Un token aléatoire est passé en argument 1 : toute connexion qui ne le
présente pas est rejetée.

DEUX MODES D'INVOCATION :
  - Mode développement : python admin_bridge.py <token> <port>
  - Mode .exe PyInstaller : CameraClient.exe --admin-bridge <token> <port>
"""

import base64
import os
import socket
import subprocess
import sys
import threading


ADMIN_BRIDGE_FLAG = "--admin-bridge"
STREAM_CHUNK = 4096


def is_bridge_invocation(argv=None) -> bool:
    """True si ce process a été lancé pour jouer le rôle de pont élevé."""
    if argv is None:
        argv = sys.argv
    return len(argv) >= 2 and argv[1] == ADMIN_BRIDGE_FLAG


def _recv_lines(sock, stop_event):
    buf = b""
    sock.settimeout(0.5)
    while not stop_event.is_set():
        try:
            data = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            return
        if not data:
            return
        buf += data
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            yield line.decode("utf-8", errors="replace")


def run_bridge(token: str, port: int) -> int:
    """Boucle principale du pont élevé."""
    try:
        sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    except OSError:
        return 3
    sock.settimeout(None)

    try:
        sock.sendall((token + "\n").encode("utf-8"))
    except OSError:
        sock.close()
        return 4

    # CMD élevé SANS FENÊTRE.
    # CREATE_NO_WINDOW : aucune fenêtre visible côté client.
    # chcp 65001      : force UTF-8 côté cmd.exe (sortie cohérente).
    # /Q              : pas d'echo des commandes (sortie plus propre).
    creationflags = subprocess.CREATE_NO_WINDOW
    proc = subprocess.Popen(
        ["cmd.exe", "/Q", "/K", "chcp 65001 >NUL"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
        bufsize=0,
    )

    stop_event = threading.Event()
    # DEUX verrous distincts : un pour les envois socket (partagé entre
    # stdout et EXIT), un pour les écritures stdin du cmd.
    sock_write_lock = threading.Lock()
    stdin_write_lock = threading.Lock()

    def pump_stdout() -> None:
        try:
            stream = proc.stdout
            while not stop_event.is_set():
                chunk = stream.read(1)
                if not chunk:
                    break
                data = bytearray(chunk)
                try:
                    fd = stream.fileno()
                    while len(data) < STREAM_CHUNK:
                        more = os.read(fd, STREAM_CHUNK - len(data))
                        if not more:
                            break
                        data.extend(more)
                        if len(more) < 64:
                            break
                except OSError:
                    pass
                # Base64 : transporte sans risque les \n, \r, et octets
                # non-UTF8 (cmd.exe peut cracher du CP850 en cas d'erreur).
                payload = base64.b64encode(bytes(data)).decode("ascii")
                with sock_write_lock:
                    try:
                        sock.sendall(b"OUT:" + payload.encode("ascii") + b"\n")
                    except OSError:
                        break
        finally:
            stop_event.set()
            with sock_write_lock:
                try:
                    sock.sendall(b"EXIT\n")
                except OSError:
                    pass

    def pump_stdin() -> None:
        try:
            for line in _recv_lines(sock, stop_event):
                if line.startswith("IN:"):
                    try:
                        raw = base64.b64decode(line[3:].encode("ascii"))
                    except Exception:
                        continue
                    with stdin_write_lock:
                        try:
                            proc.stdin.write(raw)
                            proc.stdin.flush()
                        except OSError:
                            break
                elif line == "BYE":
                    break
        finally:
            stop_event.set()
            with stdin_write_lock:
                try:
                    proc.stdin.write(b"exit\r\n")
                    proc.stdin.flush()
                except OSError:
                    pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.terminate()

    t_out = threading.Thread(target=pump_stdout, daemon=True)
    t_in = threading.Thread(target=pump_stdin, daemon=True)
    t_out.start()
    t_in.start()
    t_out.join()
    t_in.join()

    try:
        sock.close()
    except OSError:
        pass
    return 0


def main(argv=None) -> int:
    """Mode script (développement) : python admin_bridge.py <token> <port>."""
    if argv is None:
        argv = sys.argv
    if len(argv) < 3:
        return 2
    token = argv[1]
    port = int(argv[2])
    return run_bridge(token, port)


if __name__ == "__main__":
    sys.exit(main())