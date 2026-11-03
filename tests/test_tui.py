import asyncio
import sys
import threading
import time
from pathlib import Path
from time import monotonic

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))

from opengd77.codeplug import Channel, Codeplug
from textual.widgets import Label, Static

import gd77_patch  # noqa: F401 применяет патчи BlockView/ZonesView
import md9600_tui
from cp_utils import set_channel_enable_bit
from gd77_zones import create_zone_raw, get_zones, zone_id, zone_name


def _make_cp() -> Codeplug:
    cp = Codeplug(bytes(Codeplug.SIZE))

    for idx, name in ((0, "CALL"), (1, "RPT")):
        ch = Channel()
        ch.data[:] = b"\x00" * len(ch.data)
        ch.name = name
        ch.rx_freq = 145_500_000
        ch.tx_freq = 145_000_000
        ch.mode = 0
        cp.channels[idx] = ch
        set_channel_enable_bit(cp, idx)

    return Codeplug(create_zone_raw(cp, "HOME", [0]))


def _app(monkeypatch):
    monkeypatch.setattr(md9600_tui, "detect_port", lambda *a, **k: None)
    app = md9600_tui.MD9600App(port=None)
    app.codeplug = _make_cp()
    app._saved_bytes = bytes(app.codeplug)
    app.zone_map = md9600_tui.build_channel_zone_map(app.codeplug)
    return app


class _FakeRadio:
    def __init__(self, port):
        self.port = port

    def close(self):
        pass


def test_read_radio_uses_safe_reader(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        md9600_tui, "detect_port", lambda *a, **k: "COM_TEST"
    )
    monkeypatch.setattr(
        md9600_tui, "OpenGD77Radio", lambda port: _FakeRadio(port)
    )

    called = {}

    def fake_read(radio, progress=None, **kwargs):
        called["radio"] = radio
        called["progress"] = progress

        if progress is not None:
            progress(64, 64)

        return _make_cp()

    monkeypatch.setattr(md9600_tui, "read_codeplug_safe", fake_read)
    app = md9600_tui.MD9600App(port=None)

    async def scenario():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app.action_read_radio()

            for _ in range(80):
                await pilot.pause(0.05)

                if not app._radio_busy:
                    break

            assert app._radio_busy is False
            assert app.codeplug is not None
            assert app.dirty is False
            assert not isinstance(app.screen, md9600_tui.RadioBusyScreen)
            assert called.get("radio") is not None
            assert any("Прочитано" in e for e in app._events)

    asyncio.run(scenario())


def test_read_radio_esc_cancels_job(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        md9600_tui, "detect_port", lambda *a, **k: "COM_TEST"
    )
    monkeypatch.setattr(
        md9600_tui, "OpenGD77Radio", lambda port: _FakeRadio(port)
    )

    started = threading.Event()

    def fake_read(radio, progress=None, cancel=None, **kwargs):
        assert cancel is not None, "чтение должно получать колбэк отмены"
        started.set()

        for _ in range(300):
            if cancel():
                raise md9600_tui.RadioCancelled(
                    "операция прервана пользователем"
                )
            time.sleep(0.01)

        raise AssertionError("отмена не дошла до чтения")

    monkeypatch.setattr(md9600_tui, "read_codeplug_safe", fake_read)
    app = md9600_tui.MD9600App(port=None)

    async def scenario():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app.action_read_radio()

            while not started.is_set():
                await pilot.pause(0.05)

            await pilot.pause()
            await pilot.press("escape")

            for _ in range(80):
                await pilot.pause(0.05)

                if not app._radio_busy:
                    break

            assert app._radio_busy is False
            assert not isinstance(app.screen, md9600_tui.RadioBusyScreen)
            assert app.codeplug is None
            assert any("отменена" in e for e in app._events)

    asyncio.run(scenario())


def test_radio_not_found_lists_available_ports(monkeypatch):
    monkeypatch.setattr(md9600_tui, "detect_port", lambda *a, **k: None)
    monkeypatch.setattr(
        md9600_tui, "port_candidates", lambda: ["COM1", "COM14"]
    )
    app = md9600_tui.MD9600App(port=None)

    async def scenario():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app.action_read_radio()
            await pilot.pause()

            assert app._radio_busy is False
            assert any(
                "COM14" in e and "не найдена" in e for e in app._events
            )

    asyncio.run(scenario())


def test_r_key_reads_and_does_not_conflict_with_dialogs(monkeypatch):
    app = _app(monkeypatch)
    jobs = []

    async def scenario():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app._radio_job = lambda job: jobs.append(job)

            # На главном экране r — чтение из рации (read).
            await pilot.press("r")
            await pilot.pause()
            assert jobs == ["read"]

            # В диалоге зон r остаётся «переименовать» и не трогает рацию.
            jobs.clear()
            app.action_show_zones()
            await pilot.pause()
            assert isinstance(app.screen, md9600_tui.ZonesScreen)

            await pilot.press("r")
            await pilot.pause()
            assert jobs == []
            assert isinstance(app.screen, md9600_tui.TextPromptScreen)

            # В поле поиска r печатается, а не запускает чтение.
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, md9600_tui.ZonesScreen)

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, md9600_tui.ZonesScreen)

            search = app.query_one("#search")
            search.focus()
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()

            assert search.value == "r"
            assert jobs == []

    asyncio.run(scenario())


def test_read_and_write_hints_mention_safety(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(md9600_tui, "detect_port", lambda *a, **k: "COM_TEST")
    monkeypatch.setattr(
        md9600_tui, "OpenGD77Radio", lambda port: _FakeRadio(port)
    )

    started = threading.Event()
    screens = []
    original_push = md9600_tui.MD9600App.push_screen

    def spy_push(self, screen, *args, **kwargs):
        if isinstance(screen, md9600_tui.RadioBusyScreen):
            screens.append(screen)
        return original_push(self, screen, *args, **kwargs)

    monkeypatch.setattr(md9600_tui.MD9600App, "push_screen", spy_push)

    def fake_read(radio, progress=None, cancel=None, **kwargs):
        started.set()
        while not cancel():
            time.sleep(0.01)
        raise md9600_tui.RadioCancelled("отменено")

    monkeypatch.setattr(md9600_tui, "read_codeplug_safe", fake_read)
    app = md9600_tui.MD9600App(port=None)

    async def scenario():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app.action_read_radio()

            while not started.is_set():
                await pilot.pause(0.05)

            await pilot.pause()
            screen = screens[-1]

            # Текст не должен вылезать за рамку диалога.
            assert screen.show_progress is True
            assert screen.cancellable is True
            assert len(screen.hint) <= 66, screen.hint

            await pilot.press("escape")
            for _ in range(40):
                await pilot.pause(0.05)
                if not app._radio_busy:
                    break

    asyncio.run(scenario())


def test_radio_busy_dialog_layout_fits(monkeypatch):
    app = _app(monkeypatch)

    async def scenario():
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.pause()
            screen = md9600_tui.RadioBusyScreen(
                "Чтение кодплага из рации…",
                show_progress=True,
                cancellable=True,
                hint="Чтение ~15 с. Не отключайте рацию и кабель. Esc — стоп.",
            )
            app.push_screen(screen)
            await pilot.pause()

            # Прогресс меняет текст — рамка обязана остаться по размеру.
            screen.set_progress(1000, 125472)
            screen.set_progress(58_000, 125472)
            screen._render_hint()

            for _ in range(6):
                await pilot.pause(0.05)

            dialog = screen.query(".dialog > *")
            box = screen.query_one(".dialog").region

            for widget in dialog:
                assert widget.region in box, (widget, widget.region, box)

    asyncio.run(scenario())


def test_help_and_journal_dialogs_scroll(monkeypatch):
    from textual.containers import VerticalScroll

    app = _app(monkeypatch)

    async def scenario():
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.pause()
            app.action_help()
            await pilot.pause()

            scroll = app.screen.query_one("#help_scroll", VerticalScroll)
            assert app.screen.focused is scroll
            assert scroll.max_scroll_y > 0

            await pilot.press("pagedown")
            await pilot.pause()
            assert scroll.scroll_y > 0

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, md9600_tui.HelpScreen)

            lines = [f"строка {i}" for i in range(60)]
            app.push_screen(md9600_tui.JournalScreen(lines))
            await pilot.pause()

            journal = app.screen.query_one("#journal_scroll", VerticalScroll)
            assert app.screen.focused is journal
            assert journal.max_scroll_y > 0

            await pilot.press("end")
            await pilot.pause()
            assert round(journal.scroll_y) == journal.max_scroll_y

    asyncio.run(scenario())


def test_radio_busy_screen_reports_progress_and_cancels(monkeypatch):
    app = _app(monkeypatch)

    async def scenario():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            calls = []
            screen = md9600_tui.RadioBusyScreen(
                "Чтение кодплага…",
                show_progress=True,
                cancellable=True,
                on_cancel=lambda: calls.append(True),
            )
            app.push_screen(screen)
            await pilot.pause()

            screen.set_progress(512, 1024)
            await pilot.pause()

            progress = str(screen.query_one("#busy_progress", Static).render())
            elapsed = str(screen.query_one("#busy_elapsed", Label).render())
            assert "50" in progress
            assert "0.5 / 1.0 КБ" in progress
            assert "Прошло" in elapsed

            screen._rate = 256.0
            screen._render_elapsed()
            assert "осталось" in str(
                screen.query_one("#busy_elapsed", Label).render()
            )

            # Долгое молчание рации объясняется в подсказке.
            screen._last_activity = (
                monotonic() - md9600_tui.STALL_SECONDS - 5
            )
            screen._tick()
            hint = str(screen.query_one("#busy_hint", Label).render())
            assert "Молчание" in hint
            assert screen._stall_shown is True

            screen.set_progress(700, 1024)
            hint = str(screen.query_one("#busy_hint", Label).render())
            assert "Молчание" not in hint
            assert screen._stall_shown is False

            # Всё содержимое должно оставаться внутри рамки диалога.
            dialog_region = screen.query_one(".dialog").region
            for selector in (
                "#busy_message",
                "#busy_progress",
                "#busy_elapsed",
                "#busy_hint",
            ):
                assert screen.query_one(selector).region in dialog_region

            screen.action_request_cancel()
            await pilot.pause()
            assert calls == [True]
            assert screen._cancel_requested is True
            assert screen.check_action("request_cancel", ()) is False

    asyncio.run(scenario())


def test_main_screen_search_help_and_zones(monkeypatch):
    app = _app(monkeypatch)

    async def scenario():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            assert app.query_one("#table").row_count == 2

            app.query_one("#search").value = "RPT"
            await pilot.pause()
            assert app.query_one("#table").row_count == 1

            app.action_clear_filters()
            await pilot.pause()
            assert app.query_one("#table").row_count == 2

            app.action_help()
            await pilot.pause()
            assert isinstance(app.screen, md9600_tui.HelpScreen)
            app.pop_screen()
            await pilot.pause()

            app.action_show_zones()
            await pilot.pause()
            assert isinstance(app.screen, md9600_tui.ZonesScreen)
            assert app.screen.query_one("#zone_table").row_count == 1

    asyncio.run(scenario())


def test_editor_save_and_undo(monkeypatch):
    app = _app(monkeypatch)

    async def scenario():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app.action_edit_channel()
            await pilot.pause()
            assert isinstance(app.screen, md9600_tui.EditorScreen)

            editor = app.screen
            editor.query_one("#ed_name").value = "CALL2"
            editor.query_one("#ed_rx").value = "145,600"
            editor.query_one("#ed_tx").value = "145,000"
            editor._save()
            await pilot.pause()
            await pilot.pause()

            assert app.codeplug.channels[0].name == "CALL2"
            assert app.dirty is True

            app.action_undo()
            await pilot.pause()
            assert app.codeplug.channels[0].name == "CALL"

    asyncio.run(scenario())


def test_dirty_quit_guard(monkeypatch):
    app = _app(monkeypatch)

    async def scenario():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app.dirty = True
            app.action_quit()
            await pilot.pause()
            assert isinstance(app.screen, md9600_tui.ConfirmScreen)

    asyncio.run(scenario())


def test_zone_actions_from_screen(monkeypatch):
    app = _app(monkeypatch)

    async def scenario():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app.action_show_zones()
            await pilot.pause()
            zones_screen = app.screen

            assert app.rename_zone(0, "HOME2")
            zones_screen.refresh_zones()
            await pilot.pause()
            assert zone_name(get_zones(app.codeplug)[0], 0) == "HOME2"

            assert app.create_zone("MOBILE")
            zones_screen.refresh_zones()
            await pilot.pause()
            assert len(get_zones(app.codeplug)) == 2

            assert app.delete_zone(0)
            zones_screen.refresh_zones()
            await pilot.pause()
            names = [
                zone_name(z, zone_id(z, i))
                for i, z in enumerate(get_zones(app.codeplug))
            ]
            assert names == ["MOBILE"]

    asyncio.run(scenario())


def test_zone_row_select_opens_detail_not_editor(monkeypatch):
    app = _app(monkeypatch)

    async def scenario():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()
            app.action_show_zones()
            await pilot.pause()

            table = app.screen.query_one("#zone_table")
            table.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()

            assert isinstance(app.screen, md9600_tui.ZoneDetailScreen)
            assert not any(
                isinstance(s, md9600_tui.EditorScreen)
                for s in app.screen_stack
            )

    asyncio.run(scenario())


def test_csv_parser_reports_line_errors(monkeypatch, tmp_path):
    app = _app(monkeypatch)
    path = tmp_path / "channels.csv"
    path.write_text(
        "index;name;rx_freq;tx_freq;mode;rx_tone;tx_tone;"
        "tx_color;contact_num;zones\n"
        "0;NEW;146.100;146.100;FM;;;;;0\n"
        "1;BAD;abc;146.100;FM;;;;;\n"
        "2;NOZONE;146.200;146.200;FM;;;;;99\n",
        encoding="utf-8-sig",
    )

    rows, errors = app._parse_csv(str(path))
    assert len(rows) == 2
    assert rows[0].name == "NEW"
    assert rows[0].zones == {0}
    assert rows[1].name == "NOZONE"
    assert rows[1].zones == set()
    assert rows[1].zone_error is not None
    assert "99" in rows[1].zone_error
    assert len(errors) == 1
    assert "Строка 3" in errors[0]


def test_csv_import_applies_channels_and_zones(monkeypatch):
    app = _app(monkeypatch)

    async def scenario():
        async with app.run_test(size=(100, 32)) as pilot:
            await pilot.pause()

            rows = [
                md9600_tui.ParsedRow(
                    line=2,
                    name="NEW",
                    rx=146_100_000,
                    tx=146_100_000,
                    mode=0,
                    rx_tone=None,
                    tx_tone=None,
                    tx_color=0,
                    contact_num=0,
                    zones={0},
                )
            ]

            app._apply_import(rows)
            await pilot.pause()

            idx = next(
                ch.index
                for ch in app.codeplug.channels
                if ch.name == "NEW"
            )

            assert app.dirty is True
            assert app.zone_map.get(idx) == [(0, "HOME")]

    asyncio.run(scenario())
