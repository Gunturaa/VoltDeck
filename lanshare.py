#!/usr/bin/env python3
"""LANShare — Local AirDrop for Linux Mint ↔ iPhone via Safari & QR Code."""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

# Ensure package root is importable regardless of working directory
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def run_headless(port: int | None = None) -> int:
    from lanshare.config import AppConfig
    from lanshare.server import LANShareServer

    cfg = AppConfig.load()
    if port:
        cfg.port = port
    server = LANShareServer(cfg)
    server.start()
    print(f"LANShare server aktif di: {server.state.get_lan_url(include_pin=False)}")
    print(f"PIN Keamanan 6 Digit  : {server.state.security.pin}")
    print(f"Desktop URL (lokal)   : {server.state.get_desktop_url()}")
    print("Tekan Ctrl+C untuk berhenti.")

    stop = False

    def _sig(_signum, _frame):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)
    try:
        while not stop:
            time.sleep(0.3)
    finally:
        server.stop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="lanshare",
        description="LANShare — AirDrop lokal Linux Mint ↔ iPhone via Safari & QR Code",
    )
    parser.add_argument("--port", "-p", type=int, default=None, help="Port server HTTP (default: 8765)")
    parser.add_argument("--tray", "--standby", action="store_true", help="Jalankan siaga di system tray (hemat RAM)")
    parser.add_argument("--headless", action="store_true", help="Jalankan server tanpa jendela GTK3")
    parser.add_argument("--dev", action="store_true", help="Aktifkan WebKit2 Inspector untuk debugging")
    parser.add_argument("files", nargs="*", help="File lokal untuk langsung dibagikan ke iPhone")
    known, _unknown = parser.parse_known_args()

    if known.headless:
        return run_headless(known.port)

    from lanshare.desktop_app import run_desktop_app

    return run_desktop_app(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
