#!/usr/bin/env python3
"""
Ownly Audio Pocket — Remote-Log-Server

Die App verbindet sich per TCP und sendet jede Log-Zeile.
Hier laufen lassen, dann mit 'tail -f app_remote.log' mitlesen.

Starten:
    python log_server.py [port]   (Standard: 9999)
"""
import socket
import sys
import os
import threading
import time

PORT     = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app_remote.log')

_file_lock = threading.Lock()


def handle_client(conn: socket.socket, addr):
    print(f'[+] verbunden: {addr}')
    buf = b''
    try:
        with conn:
            while True:
                try:
                    data = conn.recv(4096)
                except Exception:
                    break
                if not data:
                    break
                buf += data
                while b'\n' in buf:
                    raw, buf = buf.split(b'\n', 1)
                    line = raw.decode('utf-8', errors='replace').rstrip()
                    print(line, flush=True)
                    with _file_lock:
                        with open(LOG_FILE, 'a', encoding='utf-8') as f:
                            f.write(line + '\n')
    except Exception as e:
        print(f'[!] Fehler: {e}')
    print(f'[-] getrennt: {addr}')


def main():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(('0.0.0.0', PORT))
    srv.listen(5)

    header = f'=== Ownly Log-Server :{PORT} — {time.strftime("%Y-%m-%d %H:%M:%S")} ==='
    print(header)
    print(f'Schreibe nach: {LOG_FILE}')
    print('Ctrl+C zum Beenden\n')
    with _file_lock:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(header + '\n')

    while True:
        try:
            conn, addr = srv.accept()
            threading.Thread(target=handle_client, args=(conn, addr), daemon=True).start()
        except KeyboardInterrupt:
            print('\nBeendet.')
            break
        except Exception as e:
            print(f'[!] accept Fehler: {e}')


if __name__ == '__main__':
    main()
