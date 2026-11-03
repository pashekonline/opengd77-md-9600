#!/usr/bin/env python3
"""Запуск WEB UI редактора MD-9600.

Поднимает Django-приложение, которое работает с рацией через те же
модули, что и TUI (src/gd77_flash.py, src/cp_utils.py, src/gd77_zones.py).

    venv/bin/python webui/run.py [--port COM14] [--addr 127.0.0.1:8000]
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

WEBUI = Path(__file__).resolve().parent
ROOT = WEBUI.parent

for p in (ROOT / "src", ROOT / "vendor", WEBUI):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


def main() -> None:
    parser = argparse.ArgumentParser(description="MD-9600 Codeplug Web UI")
    parser.add_argument(
        "--port",
        default=None,
        help="serial port for the radio (default: auto-detect)",
    )
    parser.add_argument(
        "--addr",
        default="127.0.0.1:8000",
        help="host:port for the web server (default 127.0.0.1:8000)",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="listen on all interfaces (доступ с других хостов)",
    )
    args = parser.parse_args()

    if args.port:
        os.environ["MD9600_PORT"] = args.port

    addr = args.addr

    if args.public:
        _, sep, port = addr.rpartition(":")
        addr = f"0.0.0.0:{port if sep else addr}"
        os.environ["MD9600_PUBLIC"] = "1"

    from django.core.management import execute_from_command_line

    print(f"MD-9600 Web UI: http://{addr}/  (Ctrl+C — остановить)")

    execute_from_command_line(
        ["manage.py", "runserver", addr, "--noreload"]
    )


if __name__ == "__main__":
    main()
