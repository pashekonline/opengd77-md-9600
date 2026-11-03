#!/usr/bin/env python3
"""Генерирует снимки интерфейса для README.

Запуск из корня проекта:

    python docs/make_screenshot.py

Пишет docs/interface.svg (главный экран) и docs/interface_read.svg (окно
чтения с прогрессом). Рация не нужна: кодплаг собирается синтетически,
окно чтения отрисуется напрямую.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from time import monotonic

DOCS = Path(__file__).resolve().parent
ROOT = DOCS.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "vendor"))

from opengd77.codeplug import Channel, Codeplug  # noqa: E402

import gd77_patch  # noqa: F401,E402
import md9600_tui  # noqa: E402
from cp_utils import set_channel_enable_bit  # noqa: E402
from gd77_zones import create_zone_raw  # noqa: E402

SIZE = (118, 34)

CHANNELS = [
    ("CALL LS", 145.500, 145.500, "FM", "", ""),
    ("RPT 145,700", 145.700, 145.100, "FM", "88.5", "88.5"),
    ("DMR REPEATER", 146.400, 146.975, "DMR", "", ""),
    ("SAT VOX", 145.850, 145.850, "FM", "", "67.0"),
    ("APRS 144.800", 144.800, 144.800, "FM", "", ""),
    ("DX 433,500", 433.500, 433.500, "FM", "", ""),
    ("NW 144,000", 144.000, 144.000, "FM", "74.4", "74.4"),
    ("QSO 144,500", 144.500, 144.500, "FM", "", ""),
]


def build_codeplug() -> Codeplug:
    cp = Codeplug(bytes(Codeplug.SIZE))

    for idx, (name, rx, tx, mode, rx_tone, tx_tone) in enumerate(CHANNELS):
        ch = Channel()
        ch.data[:] = b"\x00" * len(ch.data)
        ch.name = name
        ch.rx_freq = int(rx * 1e6)
        ch.tx_freq = int(tx * 1e6)
        ch.mode = 1 if mode == "DMR" else 0
        if ch.mode == 1:
            ch.tx_color = 1
            ch.contact_num = 1
        cp.channels[idx] = ch
        set_channel_enable_bit(cp, idx)

    data = create_zone_raw(cp, "AMATEUR", [0, 1, 2])
    data = create_zone_raw(Codeplug(data), "SATELLITE", [3, 4])
    return Codeplug(data)


def app_with_codeplug() -> md9600_tui.MD9600App:
    app = md9600_tui.MD9600App(port="COM14")
    app.codeplug = build_codeplug()
    app._saved_bytes = bytes(app.codeplug)
    app.zone_map = md9600_tui.build_channel_zone_map(app.codeplug)
    return app


def save(svg: str, name: str) -> None:
    path = DOCS / name
    path.write_text(svg, encoding="utf-8")
    print(f"{path.name}: {path.stat().st_size // 1024} КБ")


async def main() -> None:
    app = app_with_codeplug()

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        app.refresh_table()
        await pilot.pause()
        save(app.export_screenshot(), "interface.svg")

    app = app_with_codeplug()

    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause()
        screen = md9600_tui.RadioBusyScreen(
            "Чтение кодплага из рации…",
            show_progress=True,
            cancellable=True,
            hint="Чтение ~15 с. Не отключайте рацию и кабель. Esc — стоп.",
        )
        app.push_screen(screen)
        await pilot.pause()
        screen.set_progress(0, 125472)

        # Прогресс в реальном чтении приходит на каждый чанк; для снимка
        # достаточно нескольких шагов, но каждый должен быть отрисован.
        for done in range(32, 58_600, 1024):
            screen.set_progress(done, 125472)

        # Реалистичные тайминги: ~8,2 КБ/с, пройдено 7 секунд.
        screen._rate = 8250.0
        screen._start = monotonic() - 7.0
        screen._render_elapsed()

        for _ in range(5):
            await pilot.pause(0.05)

        save(app.export_screenshot(), "interface_read.svg")


if __name__ == "__main__":
    asyncio.run(main())
