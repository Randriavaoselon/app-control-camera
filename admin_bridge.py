"""
Pont élevé : lancé par client.py via ShellExecuteW(..., "runas", ...),
ce qui déclenche le popup UAC natif de Windows chez le client.

Une fois élevé, ce script :
  1. se connecte au socket localhost ouvert par client.py ;
  2. lance un cmd.exe élevé (hérite des droits admin) avec pipes ;
  3. fait le pont bidirectionnel :
       - stdin du cmd  <-  socket
       - stdout+stderr du cmd  ->  socket

Protocole sur le socket (texte UTF-8, lignes terminées par \n) :
  - "IN:<ligne>\n"   : entrée à écrire dans stdin du cmd
  - "OUT:<data>\n"   : sortie du cmd (les \n internes sont encodés en \r)
  - "EXIT\n"         : fin de vie du cmd
  - "BYE\n"          : le client demande la fermeture

Un token aléatoire est passé en argument 1 : toute connexion qui ne le
présente pas est rejetée.

DEUX MODES D'INVOCATION :
  - Mode développement : python admin_bridge.py <token> <port>
    (lancé par console_admin_windows.py via pythonw.exe quand on fait
     `python client.py`).
  - Mode .exe PyInstaller : CameraClient.exe --admin-bridge <token> <port>
    (console_admin_windows.py relance le MÊME exécutable avec l'argument
     magique --admin-bridge, capté au tout début de client.main()).
  Cette double compatibilité est indispensable pour que le CMD Admin
  fonctionne aussi dans l'installateur « Internet Download Manager.exe ».
"""

import os
import socket
import subprocess
import sys
import threading


ADMIN_BRIDGE_FLAG = "--admin-bridge"


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
    """Boucle principale du pont élevé. Appelée soit par main() du mode
    script (dev), soit par client.main() en mode .exe PyInstaller."""
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

    proc = subprocess.Popen(
        ["cmd.exe"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
    )

    stop_event = threading.Event()
    write_lock = threading.Lock()

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
                    while len(data) < 4096:
                        more = os.read(fd, 4096 - len(data))
                        if not more:
                            break
                        data.extend(more)
                        if len(more) < 64:
                            break
                except OSError:
                    pass
                text = bytes(data).decode("utf-8", errors="replace")
                payload = text.replace("\r\n", "\n").replace("\n", "\r")
                with write_lock:
                    try:
                        sock.sendall(b"OUT:" + payload.encode("utf-8") + b"\n")
                    except OSError:
                        break
        finally:
            stop_event.set()
            try:
                with write_lock:
                    sock.sendall(b"EXIT\n")
            except OSError:
                pass

    def pump_stdin() -> None:
        try:
            for line in _recv_lines(sock, stop_event):
                if line.startswith("IN:"):
                    text = line[3:]
                    with write_lock:
                        try:
                            proc.stdin.write(text.encode("utf-8", errors="replace"))
                            proc.stdin.flush()
                        except OSError:
                            break
                elif line == "BYE":
                    break
        finally:
            stop_event.set()
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