#!/usr/bin/env python3
"""Генерирует снимки WEB UI для README.

Запуск из корня проекта (нужен playwright из requirements-dev.txt):

    python docs/make_webui_screenshot.py

Пишет docs/interface_webui.png (главный экран) и
docs/interface_webui_write.png (прогресс записи). Рация не нужна:
кодплаг собирается синтетически (как в make_screenshot.py), а задача
записи имитируется прямо в менеджере радио.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path
from time import monotonic

DOCS = Path(__file__).resolve().parent
ROOT = DOCS.parent

for p in (DOCS, ROOT / "src", ROOT / "vendor", ROOT / "webui"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

HOST, PORT = "127.0.0.1", 8799
URL = f"http://{HOST}:{PORT}/"

# Если системные библиотеки Chromium не установлены (нет root), скрипт
# подхватывает локальную сборку ~/.local/webui-shot-libs.
_LOCAL_LIBS = Path.home() / ".local" / "webui-shot-libs"
_LOCAL_CONF = Path.home() / ".local" / "webui-shot-etc" / "fonts-wrap.conf"

if _LOCAL_LIBS.is_dir():
    os.environ["LD_LIBRARY_PATH"] = (
        str(_LOCAL_LIBS) + os.pathsep + os.environ.get("LD_LIBRARY_PATH", "")
    )

if _LOCAL_CONF.is_file():
    os.environ["FONTCONFIG_FILE"] = str(_LOCAL_CONF)


def start_server() -> None:
    import django
    from django.core.management import execute_from_command_line

    django.setup()

    threading.Thread(
        target=lambda: execute_from_command_line(
            ["manage.py", "runserver", f"{HOST}:{PORT}",
             "--noreload", "--nothreading"]
        ),
        daemon=True,
    ).start()

    for _ in range(50):
        try:
            with socket.create_connection((HOST, PORT), timeout=0.2):
                return
        except Exception:
            time.sleep(0.2)

    raise RuntimeError("сервер не поднялся")


def load_state() -> None:
    from editor.state import state
    from make_screenshot import build_codeplug

    from gd77_zones import build_channel_zone_map

    cp = build_codeplug()

    state.port = "COM14"
    state.data = bytes(cp)
    state.saved_bytes = state.data
    state.zone_map = build_channel_zone_map(cp)


def fake_write_job(factor: float) -> None:
    from editor.radio import radio

    total = 1_000_000

    radio._job = {
        "id": f"screenshot-{monotonic():.3f}",
        "kind": "write",
        "status": "running",
        "message": "Запись кодплага в рацию…",
        "hint": "Не отключайте кабель и не закрывайте страницу.",
        "started": monotonic() - 18,
        "done": int(total * factor),
        "total": total,
        "rate": 26_000.0,
        "last_done": 0,
        "last_time": None,
        "cancel": threading.Event(),
        "cancellable": False,
        "snapshot": None,
        "result": None,
        "result_error": False,
    }


def load_sat_state() -> None:
    from editor.state import state

    iss1 = "1 25544U 98067A   26234.51780937  .00016717  00000-0  10270-3 0  9002"
    iss2 = "2 25544  51.6416 208.9163 0004088 300.1200  60.0000 15.50440000 90020"
    so1 = "1 27607U 02058A   26233.45678900  .00002345  00000-0  12345-4 0  9999"
    so2 = "2 27607 101.4567 123.4567 0009876  87.6543 270.0000  6.45000000  9999"

    state.sat_tle = {"ISS": (iss1, iss2), "SO-50": (so1, so2)}


def main() -> None:
    from playwright.sync_api import sync_playwright

    start_server()
    load_state()
    load_sat_state()

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1360, "height": 800},
            device_scale_factor=2,
        )

        page.goto(URL, wait_until="load")
        page.wait_for_timeout(800)
        page.screenshot(path=str(DOCS / "interface_webui.png"))
        print("записан: docs/interface_webui.png")

        fake_write_job(0.58)
        page.goto(URL, wait_until="load")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(DOCS / "interface_webui_write.png"))
        print("записан: docs/interface_webui_write.png")

        from editor.radio import radio

        radio._job = None

        page.goto(f"{URL}satellites/", wait_until="load")
        page.wait_for_timeout(800)
        page.screenshot(path=str(DOCS / "interface_webui_satellites.png"))
        print("записан: docs/interface_webui_satellites.png")

        page.goto(f"{URL}satellites/new/", wait_until="load")
        page.wait_for_timeout(400)
        page.screenshot(path=str(DOCS / "interface_webui_satellite_form.png"))
        print("записан: docs/interface_webui_satellite_form.png")

        page.goto(f"{URL}satellites/1/edit/", wait_until="load")
        page.wait_for_timeout(6000)  # автозагрузка транспондеров с satgs.ru
        page.screenshot(
            path=str(DOCS / "interface_webui_satellite_tx.png"),
            full_page=True,
        )
        print("записан: docs/interface_webui_satellite_tx.png")

        browser.close()


if __name__ == "__main__":
    main()
