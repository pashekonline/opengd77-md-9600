#!/usr/bin/env python3
from __future__ import annotations

import sys

from _vendor_path import _VENDOR  # noqa: F401

if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

import argparse
import csv
import hashlib
import re
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import ClassVar

from opengd77.codeplug import Channel, Codeplug
from opengd77.comm import OpenGD77Radio
from rich.markup import escape
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    LoadingIndicator,
    Select,
    SelectionList,
    Static,
)
from textual.widgets.selection_list import Selection

from cp_utils import (
    COLOR_CODE_MAX,
    CSV_FIELDS,
    MODES,
    NAME_MAX_LEN,
    _channel_valid,
    _find_channel_template,
    _new_channel,
    clear_channel_enable_bit,
    freq_to_str,
    normalize_mode,
    parse_color_code,
    parse_contact_num,
    parse_frequency_mhz,
    parse_tone_hz,
    set_channel_enable_bit,
    set_tone_value,
    tone_to_str,
    used_indices,
    validate_channel_name,
)
from debug_log import _log
from gd77_flash import (
    RadioCancelled,
    configure_radio_port,
    detect_port,
    friendly_serial_error,
    port_candidates,
    read_codeplug_safe,
)
from gd77_patch import CODE_VERSION
from gd77_zones import (
    ZoneError,
    build_channel_zone_map,
    create_zone_raw,
    delete_zone_raw,
    format_channel_zones,
    get_zones,
    log_zone_diagnostics,
    rename_zone_raw,
    set_channel_zones_raw,
    validate_zone_name,
    zone_capacity,
    zone_id,
    zone_members_map,
    zone_name,
)

ALL_FILTER = "__all__"
APP_TITLE = f"MD-9600 Codeplug Editor {CODE_VERSION}"

# Сколько секунд молчания рации считаем подозрительным и показываем
# подсказку про питание/кабель.
STALL_SECONDS = 20
EVENT_LIMIT = 200
UNDO_LIMIT = 30

HELP_TEXT = """[b]MD-9600 Codeplug Editor[/b]

[b]Рация[/b]
  [b]r[/b]  прочитать кодплаг (read, с автоматическим backup)
  [b]w[/b]  записать в рацию — подтверждение словом WRITE
  [b]v[/b]  проверить запись: прочитать рацию и сравнить
  [b]Esc[/b]  прервать чтение или проверку (запись прерывать нельзя)
  Полное чтение занимает около 15 секунд (32 байта на запрос),
  в окне видны прогресс, прошедшее время и остаток (ETA)

[b]Каналы[/b]
  [b]n[/b]  новый канал
  [b]e[/b]  или [b]Enter[/b] — редактировать выбранный
  [b]d[/b]  удалить выбранный (с подтверждением)
  [b]c[/b]  дублировать выбранный
  [b]Ctrl+Z[/b]  отменить правку
  [b]Ctrl+Y[/b]  вернуть правку

[b]Зоны[/b]
  [b]z[/b]  список зон: состав, переименование, создание, удаление

[b]Файлы[/b]
  [b]x[/b]  экспорт каналов в CSV
  [b]i[/b]  импорт CSV с предпросмотром
  [b]b[/b]  резервная копия текущего буфера

[b]Навигация[/b]
  [b]/[/b]  поиск по имени/частоте/зоне
  [b]Esc[/b]  сбросить фильтры
  [b]l[/b]  журнал событий
  [b]?[/b] или [b]F1[/b] — эта справка
  [b]q[/b]  выход (с защитой несохранённых изменений)

[b]Прокрутка этого окна и журнала[/b]
  [b]↑[/b] [b]↓[/b]  на строку   [b]PgUp[/b] [b]PgDn[/b]  на страницу
  [b]Home[/b] [b]End[/b]  в начало и конец   колесо мыши

[b]Форматы[/b]
  Частоты — МГц, можно с запятой: 145,500
  Тоны — Гц: 88.5; пусто = тона нет
  Имя канала — до 16 ASCII-символов
  Нумерация каналов в таблице — как на рации, с 1
"""


SCROLL_HINT = (
    "Прокрутка: ↑ ↓ · PgUp PgDn · Home End · колесо мыши"
)


def focus_scroll(screen, scroll_id: str) -> None:
    """Даёт области прокрутки фокус, чтобы работали клавиши-стрелки."""
    try:
        screen.query_one(f"#{scroll_id}", VerticalScroll).focus()
    except Exception as e:
        _log(f"focus_scroll_error={e!r}")


class FieldError(ValueError):
    def __init__(self, selector: str, message: str):
        super().__init__(message)
        self.selector = selector


@dataclass
class ParsedRow:
    line: int
    name: str
    rx: int
    tx: int
    mode: int
    rx_tone: int | None
    tx_tone: int | None
    tx_color: int
    contact_num: int
    zones: set[int]
    zone_error: str | None = None


def _safe_close(radio) -> None:
    try:
        radio.close()
        return
    except Exception:
        pass

    for attr in ("serial", "ser", "s", "port"):
        try:
            obj = getattr(radio, attr, None)
            if obj is not None and hasattr(obj, "close"):
                obj.close()
                return
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Диалоги
# ---------------------------------------------------------------------------

class ConfirmScreen(ModalScreen[bool]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Отмена")
    ]

    def __init__(
        self,
        message: str,
        *,
        title: str = "Подтверждение",
        confirm_label: str = "Подтвердить",
        cancel_label: str = "Отмена",
        require_text: str | None = None,
        danger: bool = False,
    ):
        super().__init__()
        self.message = message
        self.title = title
        self.confirm_label = confirm_label
        self.cancel_label = cancel_label
        self.require_text = require_text
        self.danger = danger

    def compose(self) -> ComposeResult:
        children = [
            Label(self.title, classes="dialog-title"),
            Static(self.message, id="confirm_message", markup=False),
        ]

        if self.require_text:
            children.extend([
                Label(
                    f"Для подтверждения введите {self.require_text}:",
                    classes="dialog-hint",
                ),
                Input(placeholder=self.require_text, id="confirm_text"),
            ])

        children.extend([
            Static("", id="confirm_error", classes="dialog-error", markup=False),
            Horizontal(
                Button(self.cancel_label, id="confirm_cancel"),
                Button(
                    self.confirm_label,
                    id="confirm_ok",
                    variant="error" if self.danger else "primary",
                ),
                classes="dialog-buttons",
            ),
        ])

        yield Vertical(*children, classes="dialog")

    def on_mount(self) -> None:
        if self.require_text:
            self.query_one("#confirm_text", Input).focus()
        else:
            self.query_one("#confirm_ok", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm_ok":
            self._ok()
        elif event.button.id == "confirm_cancel":
            self.dismiss(False)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._ok()

    def _ok(self) -> None:
        if self.require_text:
            value = self.query_one("#confirm_text", Input).value.strip()

            if value != self.require_text:
                self.query_one("#confirm_error", Static).update(
                    f"Введите {self.require_text} заглавными буквами"
                )
                return

        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class TextPromptScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Отмена")
    ]

    def __init__(
        self,
        title: str,
        label: str,
        *,
        value: str = "",
        placeholder: str = "",
        max_length: int = 64,
        validator=None,
    ):
        super().__init__()
        self.title = title
        self.label = label
        self.value = value
        self.placeholder = placeholder
        self.max_length = max_length
        self.validator = validator

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(self.title, classes="dialog-title"),
            Label(self.label),
            Input(
                value=self.value,
                placeholder=self.placeholder,
                max_length=self.max_length,
                id="prompt_input",
            ),
            Static("", id="prompt_error", classes="dialog-error", markup=False),
            Horizontal(
                Button("Отмена", id="prompt_cancel"),
                Button("OK", id="prompt_ok", variant="primary"),
                classes="dialog-buttons",
            ),
            classes="dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#prompt_input", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "prompt_ok":
            self._ok()
        elif event.button.id == "prompt_cancel":
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._ok()

    def _ok(self) -> None:
        value = self.query_one("#prompt_input", Input).value.strip()

        if self.validator is not None:
            error = self.validator(value)

            if error:
                self.query_one("#prompt_error", Static).update(error)
                return

        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


def _fmt_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"

    return f"{minutes}:{secs:02d}"


class RadioBusyScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "request_cancel", "Отмена")
    ]

    DEFAULT_HINT: ClassVar[str] = (
        "Не отключайте кабель и не закрывайте приложение."
    )

    def __init__(
        self,
        message: str,
        *,
        show_progress: bool = False,
        cancellable: bool = False,
        on_cancel=None,
        hint: str | None = None,
    ):
        super().__init__()
        self.message = message
        self.show_progress = show_progress
        self.cancellable = cancellable
        self.on_cancel = on_cancel
        self.hint = hint or self.DEFAULT_HINT
        self._cancel_requested = False
        self._start = monotonic()
        self._last_activity = monotonic()
        self._timer = None
        self._done = 0
        self._total = 0
        self._last_done = 0
        self._last_time = None
        self._rate = None
        self._stall_shown = False

    def compose(self) -> ComposeResult:
        children = [
            Label(self.message, id="busy_message", classes="dialog-title"),
            LoadingIndicator(),
        ]

        if self.show_progress:
            children.append(Static("", id="busy_progress"))

        children.extend([
            Label("Прошло 0:00", id="busy_elapsed"),
            Label(self.hint, id="busy_hint"),
        ])

        yield Vertical(*children, classes="dialog")

    def on_mount(self) -> None:
        self._timer = self.set_interval(1.0, self._tick)

    def check_action(
        self,
        action: str,
        parameters: tuple[object, ...],
    ) -> bool | None:
        if action == "request_cancel":
            return self.cancellable and not self._cancel_requested

        return True

    def action_request_cancel(self) -> None:
        if not self.cancellable or self._cancel_requested:
            return

        self._cancel_requested = True

        try:
            self.query_one("#busy_hint", Label).update(
                "Отмена… ждём завершения текущего пакета. "
                "Не отключайте кабель."
            )
        except Exception:
            pass

        _log("radio_cancel_requested")

        if self.on_cancel is not None:
            try:
                self.on_cancel()
            except Exception as e:
                _log(f"radio_cancel_error={e!r}")

    def set_progress(self, done: int, total: int) -> None:
        now = monotonic()
        self._done = done
        self._total = total

        if self._last_time is not None and done > self._last_done:
            dt = now - self._last_time

            if dt > 0.05:
                instant = (done - self._last_done) / dt
                if self._rate is None:
                    self._rate = instant
                else:
                    # Сглаживаем скорость, чтобы ETA не прыгала.
                    self._rate = self._rate * 0.8 + instant * 0.2

        self._last_done = done
        self._last_time = now
        self._last_activity = now
        self._render_progress()

        # Если показывали предупреждение о молчании, оно больше не нужно.
        if self._stall_shown:
            self._render_hint()

    def _render_progress(self) -> None:
        if not self.show_progress:
            return

        try:
            widget = self.query_one("#busy_progress", Static)
        except Exception:
            return

        pct = int(self._done * 100 / self._total) if self._total else 0
        pct = max(0, min(100, pct))
        filled = int(pct / 5)
        bar = "█" * filled + "░" * (20 - filled)
        kb_done = self._done / 1024
        kb_total = max(0.0, self._total / 1024)
        widget.update(
            f"{bar}  {pct:3d}%\n"
            f"{kb_done:.1f} / {kb_total:.1f} КБ"
        )

    def _render_elapsed(self) -> None:
        elapsed = monotonic() - self._start
        text = f"Прошло {_fmt_duration(elapsed)}"

        if self._rate and self._total > self._done:
            remain = (self._total - self._done) / self._rate
            text += f" · осталось ~{_fmt_duration(remain)}"

        try:
            self.query_one("#busy_elapsed", Label).update(text)
        except Exception:
            pass

    def _render_hint(self) -> None:
        if self._cancel_requested:
            return

        try:
            widget = self.query_one("#busy_hint", Label)
        except Exception:
            return

        silence = monotonic() - self._last_activity

        if self.show_progress and silence >= STALL_SECONDS:
            if self.cancellable:
                widget.update(
                    f"Молчание {int(silence)} с: питание/кабель? "
                    "Esc — отменить."
                )
            else:
                widget.update(
                    f"Молчание {int(silence)} с: питание/кабель? "
                    "Запись прерывать нельзя."
                )
            self._stall_shown = True
        else:
            widget.update(self.hint)
            self._stall_shown = False

    def _tick(self) -> None:
        self._render_elapsed()
        self._render_hint()

    def on_unmount(self) -> None:
        if self._timer is not None:
            self._timer.stop()


class EditorScreen(ModalScreen[dict | None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Отмена"),
        Binding("ctrl+s", "save", "Сохранить"),
    ]

    FIELD_ORDER: ClassVar[list[str]] = [
        "ed_name",
        "ed_rx",
        "ed_tx",
        "ed_mode",
        "ed_rx_tone",
        "ed_tx_tone",
        "ed_cc",
        "ed_contact",
    ]

    def __init__(
        self,
        channel: Channel | None,
        index: int | None,
        zone_labels: list[tuple[int, str]],
        channel_zones: list[int],
    ):
        super().__init__()
        self.channel = channel
        self.index = index
        self.zone_labels = zone_labels
        self.channel_zones = set(channel_zones)

    def compose(self) -> ComposeResult:
        c = self.channel
        vals = {
            "name": "",
            "rx": "",
            "tx": "",
            "mode": "FM",
            "rx_tone": "",
            "tx_tone": "",
            "cc": "0",
            "contact": "0",
        }

        if c is not None:
            vals = {
                "name": c.name,
                "rx": freq_to_str(c.rx_freq),
                "tx": freq_to_str(c.tx_freq),
                "mode": MODES.get(c.mode, "FM"),
                "rx_tone": tone_to_str(c, "rx_tone"),
                "tx_tone": tone_to_str(c, "tx_tone"),
                "cc": str(c.tx_color or 0),
                "contact": str(c.contact_num or 0),
            }

        title = "Новый канал" if c is None else f"Канал #{self.index + 1}"

        fields = [
            (Label("Имя"), Input(
                value=vals["name"],
                max_length=NAME_MAX_LEN,
                placeholder="до 16 ASCII-символов",
                id="ed_name",
            )),
            (Label("RX, МГц"), Input(
                value=vals["rx"],
                placeholder="145.500",
                id="ed_rx",
            )),
            (Label("TX, МГц"), Input(
                value=vals["tx"],
                placeholder="145.500",
                id="ed_tx",
            )),
            (Label("Режим"), Select(
                [("FM", "FM"), ("DMR", "DMR")],
                value=vals["mode"],
                allow_blank=False,
                id="ed_mode",
            )),
            (Label("RX тон"), Input(
                value=vals["rx_tone"],
                placeholder="нет, например 88.5",
                id="ed_rx_tone",
            )),
            (Label("TX тон"), Input(
                value=vals["tx_tone"],
                placeholder="нет, например 88.5",
                id="ed_tx_tone",
            )),
            (Label("Color code"), Select(
                [(str(i), str(i)) for i in range(COLOR_CODE_MAX + 1)],
                value=vals["cc"],
                allow_blank=False,
                id="ed_cc",
            )),
            (Label("Контакт DMR"), Input(
                value=vals["contact"],
                placeholder="0",
                id="ed_contact",
            )),
        ]

        zone_widgets = []

        if self.zone_labels:
            zone_widgets.append(SelectionList(
                *[
                    Selection(
                        f"{zid}: {name}",
                        zid,
                        zid in self.channel_zones,
                    )
                    for zid, name in self.zone_labels
                ],
                id="ed_zones",
            ))
        else:
            zone_widgets.append(
                Static("Зоны не найдены", classes="dialog-hint")
            )

        yield Vertical(
            Label(title, classes="dialog-title"),
            VerticalScroll(
                Grid(*[w for pair in fields for w in pair], id="ed_fields"),
                Label("Зоны", classes="section-title"),
                *zone_widgets,
                Static("", id="ed_error", classes="dialog-error", markup=False),
                Static(
                    "Enter — следующее поле · Ctrl+S — сохранить · Esc — отмена",
                    id="ed_hint",
                    classes="dialog-hint",
                ),
                id="ed_scroll",
            ),
            Horizontal(
                Button("Отмена", id="ed_cancel"),
                Button("Сохранить", id="ed_save", variant="primary"),
                classes="dialog-buttons",
            ),
            classes="dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#ed_name", Input).focus()
        self._update_dmr_state()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "ed_mode":
            self._update_dmr_state()

    def _update_dmr_state(self) -> None:
        try:
            is_dmr = self.query_one("#ed_mode", Select).value == "DMR"
            self.query_one("#ed_cc", Select).disabled = not is_dmr
            self.query_one("#ed_contact", Input).disabled = not is_dmr
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._focus_next(event.input.id)

    def _focus_next(self, current_id: str | None) -> None:
        if current_id not in self.FIELD_ORDER:
            return

        i = self.FIELD_ORDER.index(current_id)

        if i + 1 < len(self.FIELD_ORDER):
            self.query_one(f"#{self.FIELD_ORDER[i + 1]}").focus()
            return

        try:
            self.query_one("#ed_zones", SelectionList).focus()
        except Exception:
            self.query_one("#ed_save", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "ed_save":
            self._save()
        elif event.button.id == "ed_cancel":
            self.dismiss(None)

    def action_save(self) -> None:
        self._save()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _field(self, selector: str, fn, *args):
        widget = self.query_one(selector)
        value = widget.value

        try:
            return fn(value, *args)
        except ValueError as e:
            raise FieldError(selector, str(e)) from e

    def _save(self) -> None:
        try:
            name = self._field("#ed_name", validate_channel_name)
            rx = self._field("#ed_rx", parse_frequency_mhz, "RX частота")
            tx = self._field("#ed_tx", parse_frequency_mhz, "TX частота")
            mode = self._field("#ed_mode", normalize_mode)
            rx_tone = self._field("#ed_rx_tone", parse_tone_hz, "RX тон")
            tx_tone = self._field("#ed_tx_tone", parse_tone_hz, "TX тон")
            tx_color = self._field("#ed_cc", parse_color_code)
            contact_num = self._field("#ed_contact", parse_contact_num)

            try:
                zones = set(self.query_one("#ed_zones", SelectionList).selected)
            except Exception:
                zones = set()

        except FieldError as e:
            self.query_one("#ed_error", Static).update(str(e))
            try:
                self.query_one(e.selector).focus()
            except Exception:
                pass
            return

        except Exception as e:
            self.query_one("#ed_error", Static).update(f"Ошибка: {e}")
            return

        self.dismiss({
            "index": self.index,
            "name": name,
            "rx": rx,
            "tx": tx,
            "mode": mode,
            "rx_tone": rx_tone,
            "tx_tone": tx_tone,
            "tx_color": tx_color,
            "contact_num": contact_num,
            "zones": zones,
        })


class CsvPathScreen(ModalScreen[str | None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Отмена")
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Импорт CSV", classes="dialog-title"),
            Label("Путь к файлу (разделитель «;»):"),
            Input(value="channels.csv", id="csv_path"),
            Static(
                "Файл будет разобран и показан в предпросмотре. "
                "Колонка index не используется — каналы добавляются "
                "в первые свободные слоты.",
                classes="dialog-hint",
                markup=False,
            ),
            Horizontal(
                Button("Отмена", id="csv_cancel"),
                Button("Далее", id="csv_ok", variant="primary"),
                classes="dialog-buttons",
            ),
            classes="dialog",
        )

    def on_mount(self) -> None:
        self.query_one("#csv_path", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "csv_ok":
            self._ok()
        elif event.button.id == "csv_cancel":
            self.dismiss(None)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self._ok()

    def _ok(self) -> None:
        path = self.query_one("#csv_path", Input).value.strip()

        if not path:
            path = "channels.csv"

        self.dismiss(path)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ImportPreviewScreen(ModalScreen[bool]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "cancel", "Отмена")
    ]

    def __init__(
        self,
        path: str,
        rows: list[ParsedRow],
        errors: list[str],
    ):
        super().__init__()
        self.path = path
        self.rows = rows
        self.errors = errors

    def compose(self) -> ComposeResult:
        zone_warnings = sum(1 for r in self.rows if r.zone_error)

        yield Vertical(
            Label("Предпросмотр импорта", classes="dialog-title"),
            Static(
                f"Файл: {self.path}\n"
                f"Будет добавлено каналов: {len(self.rows)}\n"
                f"Строк с ошибками: {len(self.errors)}\n"
                f"Предупреждений по зонам: {zone_warnings}\n\n"
                "Колонка index не используется: каналы добавляются "
                "в первые свободные слоты.",
                id="preview_summary",
                markup=False,
            ),
            DataTable(id="preview_table", cursor_type="row"),
            VerticalScroll(
                Static(
                    "\n".join(self._messages()),
                    id="preview_errors",
                    markup=False,
                ),
                id="preview_errors_scroll",
            ),
            Horizontal(
                Button("Отмена", id="preview_cancel"),
                Button(
                    "Импортировать",
                    id="preview_ok",
                    variant="primary",
                    disabled=not self.rows,
                ),
                classes="dialog-buttons",
            ),
            classes="dialog",
        )

    def _messages(self) -> list[str]:
        out = []

        for line in self.errors[:20]:
            out.append(f"! {line}")

        for r in self.rows:
            if r.zone_error:
                out.append(f"! Строка {r.line}: зоны — {r.zone_error}")

        if len(self.errors) > 20:
            out.append(f"… и ещё ошибок: {len(self.errors) - 20}")

        return out or ["Ошибок нет."]

    def on_mount(self) -> None:
        table = self.query_one("#preview_table", DataTable)
        table.add_columns("Строка", "Имя", "RX", "TX", "Режим", "Зоны")

        for r in self.rows[:20]:
            table.add_row(
                str(r.line),
                r.name,
                freq_to_str(r.rx),
                freq_to_str(r.tx),
                MODES.get(r.mode, "?"),
                ",".join(str(z) for z in sorted(r.zones)),
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "preview_ok":
            self.dismiss(True)
        elif event.button.id == "preview_cancel":
            self.dismiss(False)

    def action_cancel(self) -> None:
        self.dismiss(False)


class HelpScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Закрыть"),
        Binding("q", "close", "Закрыть"),
        Binding("f1", "close", "Закрыть"),
    ]

    SCROLL_ID: ClassVar[str] = "help_scroll"

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Помощь", classes="dialog-title"),
            VerticalScroll(
                Static(HELP_TEXT, id="help_text"),
                id=self.SCROLL_ID,
            ),
            Static(SCROLL_HINT, classes="dialog-hint"),
            Horizontal(
                Button("Закрыть", id="help_close"),
                classes="dialog-buttons",
            ),
            classes="dialog",
        )

    def on_mount(self) -> None:
        # Стрелки и PgUp/PgDn слушает сама область прокрутки, поэтому ей
        # нужен фокус: в диалоге больше нет перемещаемых виджетов.
        focus_scroll(self, self.SCROLL_ID)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "help_close":
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class JournalScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Закрыть"),
        Binding("q", "close", "Закрыть"),
    ]

    SCROLL_ID: ClassVar[str] = "journal_scroll"

    def __init__(self, lines: list[str]):
        super().__init__()
        self.lines = lines

    def compose(self) -> ComposeResult:
        text = "\n".join(self.lines) if self.lines else "Журнал пока пуст."

        yield Vertical(
            Label("Журнал событий", classes="dialog-title"),
            VerticalScroll(
                Static(text, id="journal_text", markup=False),
                id=self.SCROLL_ID,
            ),
            Static(SCROLL_HINT, classes="dialog-hint"),
            Horizontal(
                Button("Закрыть", id="journal_close"),
                classes="dialog-buttons",
            ),
            classes="dialog",
        )

    def on_mount(self) -> None:
        focus_scroll(self, self.SCROLL_ID)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "journal_close":
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


class ZonesScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Закрыть"),
        Binding("r", "rename", "Переименовать"),
        Binding("n", "new", "Новая"),
        Binding("d", "delete", "Удалить"),
    ]

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label("Зоны", classes="dialog-title"),
            Static(
                "Enter — состав · r — переименовать · n — новая · "
                "d — удалить · Esc — закрыть",
                classes="dialog-hint",
                markup=False,
            ),
            DataTable(id="zone_table", cursor_type="row"),
            Horizontal(
                Button("Новая", id="zone_new"),
                Button("Переименовать", id="zone_rename"),
                Button("Удалить", id="zone_delete", variant="error"),
                Button("Закрыть", id="zone_close"),
                classes="dialog-buttons",
            ),
            classes="dialog",
        )

    def on_mount(self) -> None:
        table = self.query_one("#zone_table", DataTable)
        table.add_columns("ID", "Имя", "Каналов", "Ёмкость")
        self.refresh_zones()

    def refresh_zones(self) -> None:
        table = self.query_one("#zone_table", DataTable)
        table.clear()

        app = self.app
        cp = getattr(app, "codeplug", None)

        if cp is None:
            return

        members = zone_members_map(cp)

        for i, z in enumerate(get_zones(cp)):
            zid = zone_id(z, i)
            table.add_row(
                str(zid),
                zone_name(z, zid),
                str(len(members.get(zid, []))),
                str(zone_capacity(z)),
                key=str(zid),
            )

    def _selected_zid(self) -> int | None:
        table = self.query_one("#zone_table", DataTable)

        if table.cursor_row is None or table.row_count == 0:
            return None

        try:
            return int(table.get_row_at(table.cursor_row)[0])
        except Exception:
            return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Не даём событию всплыть до MD9600App: иначе поверх состава зоны
        # откроется редактор канала из главной таблицы.
        event.stop()
        zid = self._selected_zid()

        if zid is not None:
            self.app.push_screen(ZoneDetailScreen(zid))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "zone_new":
            self.action_new()
        elif event.button.id == "zone_rename":
            self.action_rename()
        elif event.button.id == "zone_delete":
            self.action_delete()
        elif event.button.id == "zone_close":
            self.dismiss(None)

    def action_rename(self) -> None:
        zid = self._selected_zid()

        if zid is None:
            self.app.notify("Выберите зону", severity="warning")
            return

        cp = getattr(self.app, "codeplug", None)
        current = ""

        for i, z in enumerate(get_zones(cp)):
            if zone_id(z, i) == zid:
                current = zone_name(z, zid)
                break

        def validator(value: str) -> str | None:
            try:
                validate_zone_name(value, cp, exclude_zid=zid)
            except ZoneError as e:
                return str(e)
            return None

        def done(value: str | None) -> None:
            if value and self.app.rename_zone(zid, value):
                self.refresh_zones()

        self.app.push_screen(
            TextPromptScreen(
                "Переименовать зону",
                f"ID {zid}",
                value=current,
                max_length=16,
                validator=validator,
            ),
            done,
        )

    def action_new(self) -> None:
        cp = getattr(self.app, "codeplug", None)

        def validator(value: str) -> str | None:
            try:
                validate_zone_name(value, cp)
            except ZoneError as e:
                return str(e)
            return None

        def done(value: str | None) -> None:
            if value and self.app.create_zone(value):
                self.refresh_zones()

        self.app.push_screen(
            TextPromptScreen(
                "Новая зона",
                "Имя зоны (до 16 ASCII-символов)",
                max_length=16,
                validator=validator,
            ),
            done,
        )

    def action_delete(self) -> None:
        zid = self._selected_zid()

        if zid is None:
            self.app.notify("Выберите зону", severity="warning")
            return

        def done(ok: bool) -> None:
            if ok and self.app.delete_zone(zid):
                self.refresh_zones()

        self.app.push_screen(
            ConfirmScreen(
                f"Удалить зону {zid}?\n\n"
                "Зона будет удалена, а ID последующих зон сдвинутся. "
                "Каналы останутся в кодплаге.",
                title="Удаление зоны",
                confirm_label="Удалить",
                danger=True,
            ),
            done,
        )

    def action_close(self) -> None:
        self.dismiss(None)


class ZoneDetailScreen(ModalScreen[None]):
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape", "close", "Закрыть")
    ]

    def __init__(self, zid: int):
        super().__init__()
        self.zid = zid

    def compose(self) -> ComposeResult:
        yield Vertical(
            Label(f"Зона {self.zid}", id="zone_detail_title", classes="dialog-title"),
            DataTable(id="zone_members", cursor_type="row"),
            Horizontal(
                Button("Закрыть", id="zone_detail_close"),
                classes="dialog-buttons",
            ),
            classes="dialog",
        )

    def on_mount(self) -> None:
        app = self.app
        cp = getattr(app, "codeplug", None)
        table = self.query_one("#zone_members", DataTable)
        table.add_columns("Канал", "Имя", "RX", "TX")

        if cp is None:
            return

        members = zone_members_map(cp).get(self.zid, [])

        for idx in members:
            try:
                ch = cp.channels[idx]
                name = ch.name or "(пусто)"
                rx = freq_to_str(ch.rx_freq) if ch.rx_freq else "—"
                tx = freq_to_str(ch.tx_freq) if ch.tx_freq else "—"
            except Exception:
                name, rx, tx = "(ошибка)", "—", "—"

            table.add_row(str(idx + 1), name, rx, tx, key=str(idx))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "zone_detail_close":
            self.dismiss(None)

    def action_close(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Приложение
# ---------------------------------------------------------------------------

class MD9600App(App):
    TITLE = APP_TITLE

    CSS = """
    Screen { layout: vertical; }

    #filters {
        height: 3;
        padding: 0 1;
        background: $panel;
    }
    #search { width: 1fr; }
    #zone_filter { width: 30; }
    #mode_filter { width: 18; }

    #table { height: 1fr; }

    #status {
        height: 1;
        background: $panel;
        padding: 0 1;
    }

    Footer { background: $panel; }

    ConfirmScreen, TextPromptScreen, ImportPreviewScreen, HelpScreen,
    ZonesScreen, ZoneDetailScreen, RadioBusyScreen, JournalScreen {
        align: center middle;
        background: $background 70%;
    }

    .dialog {
        width: 76;
        max-width: 95%;
        height: auto;
        max-height: 90%;
        background: $panel;
        border: round $accent;
        padding: 1 2;
    }

    .dialog-title { text-style: bold; margin-bottom: 1; }
    .dialog-error { color: $error; height: auto; }
    .dialog-hint { color: $text-muted; height: auto; margin-top: 1; }
    .dialog-buttons { height: 3; align-horizontal: right; margin-top: 1; }
    .dialog-buttons Button { margin-left: 2; }
    .section-title { text-style: bold; margin-top: 1; }

    EditorScreen .dialog { width: 80; height: 90%; }
    #ed_scroll { height: 1fr; }
    #ed_fields {
        grid-size: 2;
        grid-columns: 20 1fr;
        grid-gutter: 0 1;
        height: auto;
    }
    #ed_fields Label { padding-top: 1; }
    #ed_zones { height: 8; border: round $primary; }
    #ed_error { height: auto; color: $error; }
    #ed_hint { color: $text-muted; }

    RadioBusyScreen .dialog { width: 62; max-width: 96%; }
    RadioBusyScreen LoadingIndicator { height: 3; }
    #busy_message { text-style: bold; }
    #busy_elapsed { color: $text-muted; height: 1; }
    #busy_progress { color: $accent; text-align: center; height: 2; }
    #busy_hint { color: $warning; height: 1; overflow: hidden; }

    ImportPreviewScreen .dialog, ZonesScreen .dialog, HelpScreen .dialog,
    JournalScreen .dialog {
        width: 90;
        max-width: 96%;
        height: 90%;
    }
    #preview_table { height: 10; }
    #preview_errors_scroll { height: 6; }
    #preview_errors { color: $warning; }
    #zone_table { height: 1fr; }
    #zone_members { height: 1fr; }
    #help_scroll, #journal_scroll { height: 1fr; }
    #help_text, #journal_text { width: 100%; height: auto; }
    """

    BINDINGS: ClassVar[list[Binding]] = [
        Binding("r", "read_radio", "Читать"),
        Binding("w", "write_radio", "В рацию"),
        Binding("v", "verify_radio", "Проверить", show=False),
        Binding("z", "show_zones", "Зоны"),
        Binding("n", "new_channel", "Новый"),
        Binding("e", "edit_channel", "Править"),
        Binding("d", "delete_channel", "Удалить"),
        Binding("c", "duplicate_channel", "Копия", show=False),
        Binding("x", "export_csv", "Экспорт"),
        Binding("i", "import_csv", "Импорт"),
        Binding("b", "backup", "Копия буфера", show=False),
        Binding("ctrl+z", "undo", "Отменить", show=False),
        Binding("ctrl+y", "redo", "Вернуть", show=False),
        Binding("slash", "focus_search", "Поиск", show=False),
        Binding("escape", "clear_filters", "Сброс фильтра", show=False),
        Binding("l", "journal", "Журнал", show=False),
        Binding("question_mark", "help", "Помощь"),
        Binding("f1", "help", "Помощь", show=False),
        Binding("q", "quit", "Выход"),
    ]

    def __init__(self, port: str | None = None):
        super().__init__()
        self.port = port
        self.codeplug: Codeplug | None = None
        self.dirty = False
        self.backup_path: str | None = None
        self.zone_map: dict[int, list[tuple[int, str]]] = {}

        self._saved_bytes: bytes | None = None
        self._undo: list[bytes] = []
        self._redo: list[bytes] = []

        self._radio_busy = False
        self._radio_cancel = threading.Event()
        self._pending_write_backup: str | None = None
        self._events: deque[str] = deque(maxlen=EVENT_LIMIT)

        self._search = ""
        self._zone_filter = ALL_FILTER
        self._mode_filter = ALL_FILTER
        self._updating_filters = False
        self._row_keys: list[int] = []

    # ---------- UI ----------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Horizontal(
            Input(placeholder="Поиск: имя или частота (/)", id="search"),
            Select(
                [("Все зоны", ALL_FILTER)],
                value=ALL_FILTER,
                allow_blank=False,
                id="zone_filter",
            ),
            Select(
                [
                    ("Все режимы", ALL_FILTER),
                    ("FM", "FM"),
                    ("DMR", "DMR"),
                ],
                value=ALL_FILTER,
                allow_blank=False,
                id="mode_filter",
            ),
            id="filters",
        )
        yield DataTable(id="table", cursor_type="row")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns(
            "Канал",
            "Имя",
            "RX МГц",
            "TX МГц",
            "Реж.",
            "RX тон",
            "TX тон",
            "CC",
            "Контакт",
            "Зоны",
        )

        self._detect_port()
        self._refresh_zone_options()
        self.refresh_table()
        table.focus()

    def _detect_port(self) -> None:
        if self.port:
            return

        try:
            self.port = detect_port()
        except Exception as e:
            _log(f"detect_port error {e!r}")
            self.port = None

    # ---------- фильтры и таблица ----------

    def _zone_labels(self) -> list[tuple[int, str]]:
        cp = self.codeplug

        if cp is None:
            return []

        out = []

        for i, z in enumerate(get_zones(cp)):
            zid = zone_id(z, i)
            out.append((zid, zone_name(z, zid)))

        return out

    def _refresh_zone_options(self) -> None:
        try:
            select = self.query_one("#zone_filter", Select)
        except Exception:
            return

        options = [("Все зоны", ALL_FILTER)]
        options.extend(
            (f"{zid}: {name}", str(zid))
            for zid, name in self._zone_labels()
        )

        self._updating_filters = True

        try:
            select.set_options(options)
            values = {value for _, value in options}

            if self._zone_filter not in values:
                self._zone_filter = ALL_FILTER

            select.value = self._zone_filter
        except Exception as e:
            _log(f"refresh_zone_options error {e!r}")
        finally:
            self._updating_filters = False

    def _channel_rows(self) -> tuple[list, int]:
        """Один проход по каналам: видимые строки и всего каналов.

        Раньше статус-бар и шапка считали общее число каналов отдельно,
        и на каждом обновлении таблицы кодплаг сканировался трижды.
        """
        cp = self.codeplug

        if cp is None:
            return [], 0

        needle = self._search.casefold()
        out = []
        total = 0

        for ch in cp.channels:
            if not _channel_valid(ch):
                continue

            total += 1

            if self._zone_filter != ALL_FILTER:
                try:
                    zid = int(self._zone_filter)
                except ValueError:
                    zid = None

                if zid is not None and not any(
                    z == zid for z, _ in self.zone_map.get(ch.index, [])
                ):
                    continue

            if (
                self._mode_filter != ALL_FILTER
                and MODES.get(ch.mode, "?") != self._mode_filter
            ):
                continue

            if needle:
                haystack = " ".join([
                    ch.name,
                    freq_to_str(ch.rx_freq),
                    freq_to_str(ch.tx_freq),
                    MODES.get(ch.mode, ""),
                    format_channel_zones(self.zone_map, ch.index),
                    str(ch.tx_color),
                    str(ch.contact_num),
                ]).casefold()

                if needle not in haystack:
                    continue

            out.append(ch)

        return out, total

    def _visible_channels(self) -> list:
        rows, _total = self._channel_rows()
        return rows

    def refresh_table(self, select_index: int | None = None) -> None:
        table = self.query_one("#table", DataTable)
        previous = select_index if select_index is not None else self._selected_index()

        rows, total = self._channel_rows()

        table.clear()
        self._row_keys = []

        for ch in rows:
            table.add_row(
                str(ch.index + 1),
                ch.name,
                freq_to_str(ch.rx_freq),
                freq_to_str(ch.tx_freq),
                MODES.get(ch.mode, "?"),
                tone_to_str(ch, "rx_tone") or "—",
                tone_to_str(ch, "tx_tone") or "—",
                str(ch.tx_color) if ch.mode == 1 else "—",
                str(ch.contact_num) if ch.mode == 1 else "—",
                format_channel_zones(self.zone_map, ch.index) or "—",
                key=str(ch.index),
            )
            self._row_keys.append(ch.index)

        if previous is not None:
            try:
                table.move_cursor(row=self._row_keys.index(previous))
            except Exception:
                pass

        self._update_status(total=total)
        self._update_header(total=total)
        self._refresh_zone_options()

    def _selected_index(self) -> int | None:
        if not self._row_keys:
            return None

        try:
            table = self.query_one("#table", DataTable)
            row = table.cursor_row
        except Exception:
            return None

        if row is None or row < 0 or row >= len(self._row_keys):
            return None

        return self._row_keys[row]

    def _total_channels(self) -> int:
        if self.codeplug is None:
            return 0

        return sum(1 for ch in self.codeplug.channels if _channel_valid(ch))

    def _update_status(self, total: int | None = None) -> None:
        try:
            status = self.query_one("#status", Static)
        except Exception:
            return

        if total is None:
            total = self._total_channels()

        visible = len(self._row_keys)
        zones = len(self._zone_labels())

        port = (
            f"[green]{escape(self.port)}[/]"
            if self.port
            else "[yellow]не найден[/]"
        )

        parts = [f"Порт: {port}"]

        if self._search or self._zone_filter != ALL_FILTER or self._mode_filter != ALL_FILTER:
            parts.append(f"Каналов: {visible}/{total}")
        else:
            parts.append(f"Каналов: {total}")

        parts.append(f"Зон: {zones}")

        if self._radio_busy:
            parts.append("[cyan]работа с рацией…[/]")
        elif self.dirty:
            parts.append("[yellow]● не сохранено[/]")
        else:
            parts.append("[green]✓ сохранено[/]")

        if self._search or self._zone_filter != ALL_FILTER or self._mode_filter != ALL_FILTER:
            parts.append("[cyan]фильтр[/]")

        status.update("  |  ".join(parts))

    def _update_header(self, total: int | None = None) -> None:
        try:
            if total is None:
                total = self._total_channels()
            zones = len(self._zone_labels())
            subtitle = (
                f"{self.port or 'порт не найден'} · "
                f"{total} кан. · {zones} зон"
            )

            if self.dirty:
                subtitle += " · ● не сохранено"

            self.sub_title = subtitle
        except Exception:
            pass

    def on_input_changed(self, event: Input.Changed) -> None:
        if self._updating_filters:
            return

        if event.input.id == "search":
            value = event.value.strip()

            if value == self._search:
                return

            self._search = value
            self.refresh_table()

    def on_select_changed(self, event: Select.Changed) -> None:
        if self._updating_filters:
            return

        if event.select.id == "zone_filter":
            if event.value == self._zone_filter:
                return

            self._zone_filter = event.value
            self.refresh_table()
        elif event.select.id == "mode_filter":
            if event.value == self._mode_filter:
                return

            self._mode_filter = event.value
            self.refresh_table()

    def action_focus_search(self) -> None:
        self.query_one("#search", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            self.query_one("#table", DataTable).focus()

    def action_clear_filters(self) -> None:
        if not (self._search or self._zone_filter != ALL_FILTER or self._mode_filter != ALL_FILTER):
            return

        self._updating_filters = True

        try:
            self.query_one("#search", Input).value = ""
            self.query_one("#zone_filter", Select).value = ALL_FILTER
            self.query_one("#mode_filter", Select).value = ALL_FILTER
        except Exception:
            pass
        finally:
            self._updating_filters = False

        self._search = ""
        self._zone_filter = ALL_FILTER
        self._mode_filter = ALL_FILTER
        self.refresh_table()

        try:
            self.query_one("#table", DataTable).focus()
        except Exception:
            pass

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Реагируем только на главную таблицу каналов, а не на таблицы
        # внутри модальных экранов.
        if event.data_table.id != "table":
            return

        event.stop()
        self.action_edit_channel()

    # ---------- undo / dirty ----------

    def _snapshot(self) -> None:
        if self.codeplug is None:
            return

        self._undo.append(bytes(self.codeplug))

        if len(self._undo) > UNDO_LIMIT:
            self._undo.pop(0)

        self._redo.clear()

    def _update_dirty(self) -> None:
        if self.codeplug is None:
            self.dirty = False
        elif self._saved_bytes is None:
            self.dirty = True
        else:
            self.dirty = bytes(self.codeplug) != self._saved_bytes

    def _restore(self, data: bytes) -> None:
        self.codeplug = Codeplug(data)
        self.zone_map = build_channel_zone_map(self.codeplug)
        self._update_dirty()
        self._refresh_zone_options()
        self.refresh_table()

    def _commit(
        self,
        new_bytes: bytes,
        message: str,
        select_index: int | None = None,
    ) -> None:
        self.codeplug = Codeplug(new_bytes)
        self.zone_map = build_channel_zone_map(self.codeplug)
        self._update_dirty()
        self._refresh_zone_options()
        self.refresh_table(select_index=select_index)
        self.notify_status(message)

    def action_undo(self) -> None:
        if not self._undo or self.codeplug is None:
            self.notify_status("Нечего отменять", error=True)
            return

        self._redo.append(bytes(self.codeplug))
        self._restore(self._undo.pop())
        self.notify_status("Изменение отменено")

    def action_redo(self) -> None:
        if not self._redo or self.codeplug is None:
            self.notify_status("Нечего возвращать", error=True)
            return

        self._undo.append(bytes(self.codeplug))
        self._restore(self._redo.pop())
        self.notify_status("Изменение возвращено")

    # ---------- статус и журнал ----------

    def _log_event(self, text: str, error: bool = False) -> None:
        prefix = "ОШИБКА" if error else "OK"
        self._events.append(
            f"{datetime.now():%H:%M:%S} [{prefix}] {text}"
        )
        _log(text)

    def notify_status(self, text: str, error: bool = False) -> None:
        self._log_event(text, error)

        try:
            self.notify(
                text,
                severity="error" if error else "information",
                timeout=8 if error else 5,
                markup=False,
            )
        except Exception:
            pass

    # ---------- действия с рацией ----------

    def action_read_radio(self) -> None:
        if self._radio_busy:
            self.notify_status("Операция с рацией уже выполняется", error=True)
            return

        if self.dirty and self.codeplug is not None:
            self.push_screen(
                ConfirmScreen(
                    "Есть несохранённые изменения.\n\n"
                    "При чтении они будут потеряны. Продолжить?",
                    title="Чтение из рации",
                    confirm_label="Прочитать",
                    danger=True,
                ),
                lambda ok: self._radio_job("read") if ok else None,
            )
            return

        self._radio_job("read")

    def action_write_radio(self) -> None:
        if self.codeplug is None:
            self.notify_status("Сначала прочитайте кодплаг [r]", error=True)
            return

        if self._radio_busy:
            self.notify_status("Операция с рацией уже выполняется", error=True)
            return

        backup = (
            f"codeplug_before_write_{datetime.now():%Y%m%d_%H%M%S}.g77"
        )
        self._pending_write_backup = backup

        changed = self._changed_channel_count()
        total = self._total_channels()
        zones = len(self._zone_labels())

        message = (
            "Записать кодплаг в рацию?\n\n"
            f"Каналов: {total}\n"
            f"Зон: {zones}\n"
            f"Изменено каналов: {changed}\n\n"
            f"Перед записью будет создан backup:\n{backup}\n\n"
            "Для подтверждения введите WRITE."
        )

        self.push_screen(
            ConfirmScreen(
                message,
                title="Запись в рацию",
                confirm_label="Записать",
                require_text="WRITE",
                danger=True,
            ),
            lambda ok: self._radio_job("write") if ok else None,
        )

    def action_verify_radio(self) -> None:
        if self.codeplug is None:
            self.notify_status("Сначала прочитайте кодплаг [r]", error=True)
            return

        if self._radio_busy:
            self.notify_status("Операция с рацией уже выполняется", error=True)
            return

        self._radio_job("verify")

    def _radio_job(self, job: str) -> None:
        if self._radio_busy:
            self.notify_status("Операция с рацией уже выполняется", error=True)
            return

        port = self.port

        if not port:
            try:
                port = detect_port()
            except Exception as e:
                _log(f"detect_port error {e!r}")
                port = None

        if not port:
            candidates = ", ".join(port_candidates()) or "нет доступных портов"
            self.notify_status(
                "Рация не найдена. Проверьте USB-кабель и драйвер. "
                f"Порты в системе: {candidates}. "
                "Укажите нужный: --port COM14.",
                error=True,
            )
            return

        self.port = port

        try:
            radio = OpenGD77Radio(port)
        except Exception as e:
            self.notify_status(
                f"Не удалось открыть порт {port}: {e}. "
                "Проверьте кабель, драйвер и права доступа.",
                error=True,
            )
            return

        self._radio_busy = True
        self._radio_cancel.clear()
        self._update_status()

        messages = {
            "read": "Чтение кодплага из рации…",
            "write": "Запись кодплага в рацию…",
            "verify": "Проверка записи: чтение из рации…",
        }

        hints = {
            "read": "Чтение ~15 с. Не отключайте рацию и кабель. Esc — стоп.",
            "verify": "Сравнение ~15 с. Не отключайте рацию. Esc — стоп.",
            "write": "Не отключайте кабель и не закрывайте приложение.",
        }

        self.push_screen(
            RadioBusyScreen(
                messages.get(job, "Работа с рацией…"),
                show_progress=job in ("read", "verify", "write"),
                cancellable=job in ("read", "verify"),
                on_cancel=self._radio_cancel.set,
                hint=hints.get(job),
            )
        )

        def report(done: int, total: int) -> None:
            try:
                self.call_from_thread(self._radio_progress, done, total)
            except Exception:
                pass

        def work() -> None:
            try:
                if job == "read":
                    cp = read_codeplug_safe(
                        radio,
                        progress=report,
                        cancel=self._radio_cancel.is_set,
                    )
                    backup = (
                        f"codeplug_backup_{datetime.now():%Y%m%d_%H%M%S}.g77"
                    )
                    Path(backup).write_bytes(bytes(cp))
                    self.call_from_thread(self._read_done, cp, backup)

                elif job == "write":
                    snapshot = bytes(self.codeplug)
                    backup = self._pending_write_backup or (
                        f"codeplug_before_write_"
                        f"{datetime.now():%Y%m%d_%H%M%S}.g77"
                    )
                    Path(backup).write_bytes(snapshot)

                    h = hashlib.sha256(snapshot).hexdigest()[:16]
                    _log(f"write start len={len(snapshot)} sha={h}")

                    configure_radio_port(radio)
                    radio._md9600_written_sectors = {}
                    radio.write_codeplug(self.codeplug, progress=report)

                    self.call_from_thread(self._write_done, backup)

                elif job == "verify":
                    cp = read_codeplug_safe(
                        radio,
                        progress=report,
                        cancel=self._radio_cancel.is_set,
                    )
                    same = bytes(cp) == bytes(self.codeplug)
                    changed = 0 if same else self._changed_against(cp)
                    self.call_from_thread(self._verify_done, same, changed)

                else:
                    self.call_from_thread(
                        self.notify_status,
                        f"Неизвестная задача: {job}",
                        True,
                    )

            except RadioCancelled:
                self.call_from_thread(self._radio_cancelled)

            except Exception as e:
                _log(f"job error {job} {e!r}")
                self.call_from_thread(self._radio_failed, str(e))

            finally:
                _safe_close(radio)
                self.call_from_thread(self._radio_finished)

        self.run_worker(work, thread=True, exclusive=True)

    def _read_done(self, cp: Codeplug, backup: str) -> None:
        self.codeplug = cp
        self._saved_bytes = bytes(cp)
        self._undo.clear()
        self._redo.clear()
        self.backup_path = backup

        log_zone_diagnostics(cp)
        self.zone_map = build_channel_zone_map(cp)
        self._update_dirty()
        self._refresh_zone_options()
        self.refresh_table()

        self.notify_status(
            f"Прочитано: {self._total_channels()} каналов, "
            f"{len(self._zone_labels())} зон. Backup: {backup}"
        )

    def _write_done(self, backup: str) -> None:
        self._saved_bytes = bytes(self.codeplug)
        self._update_dirty()
        self.refresh_table()
        self.notify_status(
            f"Записано. Backup: {backup}. "
            "Проверить запись — [v]."
        )

    def _verify_done(self, same: bool, changed: int) -> None:
        if same:
            self.notify_status("Проверка пройдена: рация совпадает с буфером")
        else:
            self.notify_status(
                f"Есть различия: изменено каналов — {changed}. "
                "Можно записать заново [w].",
                error=True,
            )

    def _radio_failed(self, message: str) -> None:
        self.notify_status(
            f"Ошибка операции: {friendly_serial_error(message)}. "
            "Подробности — в журнале [l].",
            error=True,
        )

    def _radio_cancelled(self) -> None:
        self.notify_status(
            "Операция отменена. Рация осталась в обычном режиме."
        )

    def _radio_progress(self, done: int, total: int) -> None:
        if isinstance(self.screen, RadioBusyScreen):
            self.screen.set_progress(done, total)

    def _radio_finished(self) -> None:
        self._radio_busy = False
        self._radio_cancel.clear()

        if isinstance(self.screen, RadioBusyScreen):
            self.pop_screen()

        self._update_status()
        self._update_header()

    def _changed_channel_count(self) -> int:
        if self.codeplug is None or self._saved_bytes is None:
            return 0

        return self._changed_against(Codeplug(self._saved_bytes))

    def _changed_against(self, other: Codeplug) -> int:
        count = 0

        for i in range(1024):
            try:
                if (
                    bytes(self.codeplug.channels[i].data)
                    != bytes(other.channels[i].data)
                ):
                    count += 1
            except Exception:
                pass

        return count

    # ---------- каналы ----------

    def action_new_channel(self) -> None:
        if self.codeplug is None:
            self.notify_status("Сначала прочитайте кодплаг [r]", error=True)
            return

        self._open_editor(None, None)

    def action_edit_channel(self) -> None:
        idx = self._selected_index()

        if idx is None or self.codeplug is None:
            self.notify_status("Выберите канал", error=True)
            return

        try:
            ch = self.codeplug.channels[idx]
        except Exception:
            self.notify_status(f"Канал #{idx + 1} не найден", error=True)
            return

        self._open_editor(ch, idx)

    def _open_editor(self, channel: Channel | None, index: int | None) -> None:
        if self.codeplug is None:
            self.notify_status("Сначала прочитайте кодплаг [r]", error=True)
            return

        channel_zones = []

        if index is not None:
            channel_zones = [
                zid for zid, _ in self.zone_map.get(index, [])
            ]

        self.push_screen(
            EditorScreen(
                channel,
                index,
                self._zone_labels(),
                channel_zones,
            ),
            self._editor_done,
        )

    def _editor_done(self, result: dict | None) -> None:
        if result is None or self.codeplug is None:
            return

        try:
            idx = result["index"]
            cp = Codeplug(bytes(self.codeplug))

            if idx is None:
                used = used_indices(cp)
                idx = 0

                while idx in used and idx < len(cp.channels):
                    idx += 1

                if idx >= len(cp.channels):
                    self.notify_status(
                        "Нет свободных слотов (максимум 1024)",
                        error=True,
                    )
                    return

                ch = _new_channel(cp)
            else:
                try:
                    ch = cp.channels[idx]
                except Exception:
                    ch = _new_channel(cp)

            ch.name = result["name"]
            ch.rx_freq = result["rx"]
            ch.tx_freq = result["tx"]
            ch.mode = result["mode"]
            set_tone_value(ch, "rx_tone", result["rx_tone"])
            set_tone_value(ch, "tx_tone", result["tx_tone"])
            ch.tx_color = result["tx_color"]
            ch.contact_num = result["contact_num"]

            try:
                ch.valid = True
            except Exception:
                pass

            cp.channels[idx] = ch
            set_channel_enable_bit(cp, idx)

            try:
                new_bytes = set_channel_zones_raw(cp, idx, result["zones"])
            except ZoneError as e:
                self.notify_status(
                    f"Не удалось обновить зоны: {e}. Изменения не сохранены.",
                    error=True,
                )
                return

            self._snapshot()
            self._commit(
                new_bytes,
                f"Канал #{idx + 1} сохранён. Запись в рацию — [w]",
                select_index=idx,
            )

        except Exception as e:
            _log(f"apply_editor error {e!r}")
            self.notify_status(
                f"Не удалось сохранить канал: {e}", error=True
            )

    def action_delete_channel(self) -> None:
        idx = self._selected_index()

        if idx is None or self.codeplug is None:
            self.notify_status("Выберите канал", error=True)
            return

        try:
            ch = self.codeplug.channels[idx]
            name = ch.name or f"#{idx + 1}"
        except Exception:
            name = f"#{idx + 1}"

        self.push_screen(
            ConfirmScreen(
                f"Удалить канал #{idx + 1} «{name}»?\n\n"
                "Канал будет очищен и убран из всех зон.",
                title="Удаление канала",
                confirm_label="Удалить",
                danger=True,
            ),
            lambda ok: self._delete_channel(idx) if ok else None,
        )

    def _delete_channel(self, idx: int) -> None:
        if self.codeplug is None:
            return

        try:
            cp = Codeplug(bytes(self.codeplug))
            ch = Channel()

            try:
                ch.data[:] = b"\x00" * len(ch.data)
            except Exception:
                pass

            try:
                ch.valid = False
            except Exception:
                pass

            cp.channels[idx] = ch
            clear_channel_enable_bit(cp, idx)

            new_bytes = set_channel_zones_raw(cp, idx, set())

        except ZoneError as e:
            self.notify_status(
                f"Канал не удалён: {e}", error=True
            )
            return

        except Exception as e:
            self.notify_status(
                f"Не удалось удалить канал: {e}", error=True
            )
            return

        self._snapshot()
        self._commit(
            new_bytes,
            f"Канал #{idx + 1} удалён и убран из зон. Запись — [w]",
        )

    def action_duplicate_channel(self) -> None:
        idx = self._selected_index()

        if idx is None or self.codeplug is None:
            self.notify_status("Выберите канал", error=True)
            return

        try:
            cp = Codeplug(bytes(self.codeplug))
            used = used_indices(cp)
            free = 0

            while free in used and free < len(cp.channels):
                free += 1

            if free >= len(cp.channels):
                self.notify_status("Нет свободных слотов", error=True)
                return

            src = cp.channels[idx]
            ch = Channel()
            ch.data[:] = src.data[:]
            ch.name = (src.name + " copy")[:NAME_MAX_LEN]

            try:
                ch.valid = True
            except Exception:
                pass

            cp.channels[free] = ch
            set_channel_enable_bit(cp, free)

            zones = {zid for zid, _ in self.zone_map.get(idx, [])}

            try:
                new_bytes = set_channel_zones_raw(cp, free, zones)
            except ZoneError as e:
                self.notify_status(
                    f"Не удалось скопировать зоны: {e}", error=True
                )
                return

        except Exception as e:
            self.notify_status(
                f"Не удалось дублировать канал: {e}", error=True
            )
            return

        self._snapshot()
        self._commit(
            new_bytes,
            f"Канал #{idx + 1} скопирован в слот #{free + 1}",
            select_index=free,
        )

    # ---------- зоны ----------

    def action_show_zones(self) -> None:
        if self.codeplug is None:
            self.notify_status("Сначала прочитайте кодплаг [r]", error=True)
            return

        self.push_screen(ZonesScreen())

    def rename_zone(self, zid: int, name: str) -> bool:
        if self.codeplug is None:
            return False

        try:
            new_bytes = rename_zone_raw(self.codeplug, zid, name)
        except ZoneError as e:
            self.notify_status(str(e), error=True)
            return False

        self._snapshot()
        self._commit(new_bytes, f"Зона {zid} переименована в «{name}»")
        return True

    def create_zone(self, name: str) -> bool:
        if self.codeplug is None:
            return False

        old_count = len(self._zone_labels())

        try:
            new_bytes = create_zone_raw(self.codeplug, name)
        except ZoneError as e:
            self.notify_status(str(e), error=True)
            return False

        self._snapshot()
        self._commit(new_bytes, f"Зона «{name}» создана (ID {old_count})")
        return True

    def delete_zone(self, zid: int) -> bool:
        if self.codeplug is None:
            return False

        try:
            new_bytes = delete_zone_raw(self.codeplug, zid)
        except ZoneError as e:
            self.notify_status(str(e), error=True)
            return False

        self._snapshot()
        self._commit(
            new_bytes,
            f"Зона {zid} удалена. ID последующих зон сдвинуты.",
        )
        return True

    def _parse_zone_ids(self, s: str) -> set[int]:
        s = (s or "").strip()

        if not s:
            return set()

        if any(ch in s for ch in ",;"):
            parts = re.split(r"[,;]+", s)
        else:
            parts = re.split(r"\s+", s)

        ids = set()
        labels = self._zone_labels()

        for token in parts:
            token = token.strip().lstrip("#")

            if not token:
                continue

            if token.isdigit():
                zid = int(token)

                if zid not in {z for z, _ in labels}:
                    raise ValueError(
                        f"зона {zid} не найдена. "
                        "Клавиша [z] показывает список зон."
                    )

                ids.add(zid)
                continue

            tl = token.casefold()

            exact = [zid for zid, name in labels if name.casefold() == tl]

            if len(exact) == 1:
                ids.add(exact[0])
                continue

            startswith = [
                zid for zid, name in labels
                if name.casefold().startswith(tl)
            ]

            if len(startswith) == 1:
                ids.add(startswith[0])
                continue

            contains = [
                zid for zid, name in labels
                if tl in name.casefold()
            ]

            if len(contains) == 1:
                ids.add(contains[0])
                continue

            raise ValueError(
                f"не удалось распознать зону: {token!r}. "
                "Используйте ID зоны, клавиша [z] показывает список."
            )

        return ids

    # ---------- CSV ----------

    def action_export_csv(self) -> None:
        if self.codeplug is None:
            self.notify_status("Сначала прочитайте кодплаг [r]", error=True)
            return

        path = f"channels_{datetime.now():%Y%m%d_%H%M%S}.csv"
        fields = list(CSV_FIELDS)

        if "zones" not in fields:
            fields.append("zones")

        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerow(fields)

                for ch in self.codeplug.channels:
                    if not _channel_valid(ch):
                        continue

                    zone_ids = ",".join(
                        str(zid)
                        for zid, _ in self.zone_map.get(ch.index, [])
                    )

                    writer.writerow([
                        ch.index,
                        ch.name,
                        freq_to_str(ch.rx_freq),
                        freq_to_str(ch.tx_freq),
                        MODES.get(ch.mode, "FM"),
                        tone_to_str(ch, "rx_tone"),
                        tone_to_str(ch, "tx_tone"),
                        ch.tx_color if ch.mode == 1 else "",
                        ch.contact_num if ch.mode == 1 else "",
                        zone_ids,
                    ])
        except Exception as e:
            self.notify_status(f"Ошибка экспорта: {e}", error=True)
            return

        self.notify_status(f"Экспортировано: {path}")

    def action_import_csv(self) -> None:
        if self.codeplug is None:
            self.notify_status("Сначала прочитайте кодплаг [r]", error=True)
            return

        self.push_screen(CsvPathScreen(), self._import_path_done)

    def _import_path_done(self, path: str | None) -> None:
        if not path:
            return

        try:
            rows, errors = self._parse_csv(path)
        except FileNotFoundError:
            self.notify_status(f"Файл не найден: {path}", error=True)
            return
        except Exception as e:
            self.notify_status(f"Ошибка CSV: {e}", error=True)
            return

        if not rows and not errors:
            self.notify_status("CSV пуст: нет строк для импорта", error=True)
            return

        self.push_screen(
            ImportPreviewScreen(path, rows, errors),
            lambda ok: self._apply_import(rows) if ok else None,
        )

    def _parse_csv(self, path: str) -> tuple[list[ParsedRow], list[str]]:
        rows: list[ParsedRow] = []
        errors: list[str] = []

        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f, delimiter=";")

            if reader.fieldnames is None:
                raise ValueError("CSV пуст или неверный разделитель")

            for raw in reader:
                line = reader.line_num

                try:
                    name = validate_channel_name(raw.get("name", ""))
                    rx = parse_frequency_mhz(raw.get("rx_freq", ""), "RX")
                    tx = parse_frequency_mhz(raw.get("tx_freq", ""), "TX")
                    mode = normalize_mode(raw.get("mode", "") or "FM")
                    rx_tone = parse_tone_hz(raw.get("rx_tone", ""), "RX тон")
                    tx_tone = parse_tone_hz(raw.get("tx_tone", ""), "TX тон")
                    tx_color = parse_color_code(raw.get("tx_color", ""))
                    contact_num = parse_contact_num(
                        raw.get("contact_num", "")
                    )
                except ValueError as e:
                    errors.append(f"Строка {line}: {e}")
                    continue

                zones: set[int] = set()
                zone_error = None
                spec = (raw.get("zones") or "").strip()

                if spec:
                    try:
                        zones = self._parse_zone_ids(spec)
                    except Exception as e:
                        zone_error = str(e)

                rows.append(ParsedRow(
                    line=line,
                    name=name,
                    rx=rx,
                    tx=tx,
                    mode=mode,
                    rx_tone=rx_tone,
                    tx_tone=tx_tone,
                    tx_color=tx_color,
                    contact_num=contact_num,
                    zones=zones,
                    zone_error=zone_error,
                ))

        return rows, errors

    def _apply_import(self, rows: list[ParsedRow]) -> None:
        if self.codeplug is None or not rows:
            return

        cp = Codeplug(bytes(self.codeplug))
        used = used_indices(cp)
        template = _find_channel_template(cp)

        nxt = 0
        added = 0
        zone_failed = 0
        first_added = None

        for row in rows:
            while nxt in used and nxt < len(cp.channels):
                nxt += 1

            if nxt >= len(cp.channels):
                break

            try:
                ch = _new_channel(cp, template)
                ch.name = row.name
                ch.rx_freq = row.rx
                ch.tx_freq = row.tx
                ch.mode = row.mode
                set_tone_value(ch, "rx_tone", row.rx_tone)
                set_tone_value(ch, "tx_tone", row.tx_tone)
                ch.tx_color = row.tx_color
                ch.contact_num = row.contact_num

                try:
                    ch.valid = True
                except Exception:
                    pass

                cp.channels[nxt] = ch
                set_channel_enable_bit(cp, nxt)
                used.add(nxt)

                if first_added is None:
                    first_added = nxt

                added += 1

                if row.zones:
                    try:
                        new_bytes = set_channel_zones_raw(
                            cp, nxt, row.zones
                        )
                        cp = Codeplug(new_bytes)
                    except Exception as e:
                        zone_failed += 1
                        _log(
                            f"import zones failed line={row.line} "
                            f"ch={nxt} {e!r}"
                        )

            except Exception as e:
                _log(f"import row failed line={row.line} {e!r}")

            nxt += 1

        if added == 0:
            self.notify_status(
                "Импорт не добавил ни одного канала. "
                "Проверьте ошибки в предпросмотре.",
                error=True,
            )
            return

        self._snapshot()

        extra = f", ошибок зон: {zone_failed}" if zone_failed else ""
        self._commit(
            bytes(cp),
            f"Импорт: добавлено {added}{extra}. Запись в рацию — [w]",
            select_index=first_added,
        )

    # ---------- файлы и справка ----------

    def action_backup(self) -> None:
        if self.codeplug is None:
            self.notify_status("Кодплаг не загружен", error=True)
            return

        path = f"codeplug_backup_{datetime.now():%Y%m%d_%H%M%S}.g77"

        try:
            Path(path).write_bytes(bytes(self.codeplug))
        except Exception as e:
            self.notify_status(f"Не удалось сохранить backup: {e}", error=True)
            return

        self.notify_status(f"Backup сохранён: {path}")

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_journal(self) -> None:
        self.push_screen(JournalScreen(list(self._events)))

    def action_quit(self) -> None:
        if self._radio_busy:
            self.notify_status(
                "Дождитесь окончания операции с рацией", error=True
            )
            return

        if self.dirty and self.codeplug is not None:
            self.push_screen(
                ConfirmScreen(
                    "Есть несохранённые изменения.\n\n"
                    "Выйти без записи в рацию? "
                    "Сохранить можно клавишей [w].",
                    title="Выход",
                    confirm_label="Выйти без записи",
                    danger=True,
                ),
                lambda ok: self.exit() if ok else None,
            )
            return

        self.exit()

    # ---------- доступность действий ----------

    def check_action(
        self,
        action: str,
        parameters: tuple[object, ...],
    ) -> bool | None:
        if action == "read_radio":
            return not self._radio_busy

        if action in (
            "write_radio",
            "verify_radio",
            "show_zones",
            "new_channel",
            "export_csv",
            "import_csv",
            "backup",
        ):
            return self.codeplug is not None and not self._radio_busy

        if action in ("edit_channel", "delete_channel", "duplicate_channel"):
            return (
                self.codeplug is not None
                and not self._radio_busy
                and self._selected_index() is not None
            )

        if action == "undo":
            return bool(self._undo) and not self._radio_busy

        if action == "redo":
            return bool(self._redo) and not self._radio_busy

        return True


def main():
    parser = argparse.ArgumentParser(description="MD-9600 Codeplug TUI")
    parser.add_argument(
        "--port",
        default=None,
        help="serial port (default: auto-detect)",
    )

    args = parser.parse_args()
    app = MD9600App(port=args.port)
    app.run()


if __name__ == "__main__":
    main()
