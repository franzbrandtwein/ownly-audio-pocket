#!/usr/bin/env python3
"""
Ownly Audio Pocket — Remote-Log-Server (UDP)

Die App schickt jede Log-Zeile als UDP-Datagramm.
UDP braucht keine Verbindung — Logs kommen auch wenn die App im Hintergrund ist.

Starten:
    python log_server.py [port]   (Standard: 9999)

Mitlesen:
    tail -f app_remote.log
"""
import socket
import sys
import os
import time

PORT     = int(sys.argv[1]) if len(sys.argv) > 1 else 9999
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app_remote.log')


def main():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(('0.0.0.0', PORT))

    header = f'=== Ownly Log-Server (UDP) :{PORT} — {time.strftime("%Y-%m-%d %H:%M:%S")} ==='
    print(header)
    print(f'Schreibe nach: {LOG_FILE}')
    print('Ctrl+C zum Beenden\n')
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(header + '\n')

    log_f = open(LOG_FILE, 'a', encoding='utf-8')
    try:
        while True:
            try:
                data, _addr = sock.recvfrom(65535)
                text = data.decode('utf-8', errors='replace')
                for line in text.split('\n'):
                    line = line.rstrip()
                    if line:
                        print(line, flush=True)
                        log_f.write(line + '\n')
                        log_f.flush()
            except KeyboardInterrupt:
                print('\nBeendet.')
                break
            except Exception as e:
                print(f'[!] Fehler: {e}')
    finally:
        log_f.close()
        sock.close()


if __name__ == '__main__':
    main()
