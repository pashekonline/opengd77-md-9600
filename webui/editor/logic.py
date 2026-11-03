"""Прикладная логика редактора: каналы, зоны, CSV.

Повторяет методы MD9600App из src/md9600_tui.py, работая поверх
cp_utils / gd77_zones / opengd77 без правки самих скриптов.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from opengd77.codeplug import Channel, Codeplug

from cp_utils import (
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
from gd77_zones import (
    ZoneError,
    create_zone_raw,
    delete_zone_raw,
    rename_zone_raw,
    set_channel_zones_raw,
)


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
    zones: set[int] = field(default_factory=set)
    zone_error: str | None = None


# ---------------------------------------------------------------------------
# Каналы
# ---------------------------------------------------------------------------

def validate_channel_form(data: dict) -> tuple[dict, dict]:
    """Валидация полей формы канала. Возвращает (values, errors)."""
    errors: dict[str, str] = {}
    values: dict = {}

    def check(key: str, fn, *args):
        try:
            values[key] = fn(data.get(key, ""), *args)
        except ValueError as e:
            errors[key] = str(e)

    check("name", validate_channel_name)
    check("rx", parse_frequency_mhz, "RX частота")
    check("tx", parse_frequency_mhz, "TX частота")
    check("mode", normalize_mode)
    check("rx_tone", parse_tone_hz, "RX тон")
    check("tx_tone", parse_tone_hz, "TX тон")
    check("cc", parse_color_code)
    check("contact", parse_contact_num)

    if "mode" in values and values["mode"] != 1:
        values["cc"] = values.get("cc", 0)
        values["contact"] = values.get("contact", 0)

    return values, errors


def apply_channel_edit(state, index: int | None, values: dict, zones: set[int]) -> tuple[bool, str]:
    with state.lock:
        if state.data is None:
            return False, "Кодплаг не загружен"

        try:
            cp = Codeplug(state.data)

            if index is None:
                used = used_indices(cp)
                idx = 0

                while idx in used and idx < len(cp.channels):
                    idx += 1

                if idx >= len(cp.channels):
                    return False, "Нет свободных слотов (максимум 1024)"

                ch = _new_channel(cp)
            else:
                idx = index

                try:
                    ch = cp.channels[idx]
                except Exception:
                    ch = _new_channel(cp)

            ch.name = values["name"]
            ch.rx_freq = values["rx"]
            ch.tx_freq = values["tx"]
            ch.mode = values["mode"]
            set_tone_value(ch, "rx_tone", values["rx_tone"])
            set_tone_value(ch, "tx_tone", values["tx_tone"])
            ch.tx_color = values["cc"]
            ch.contact_num = values["contact"]

            try:
                ch.valid = True
            except Exception:
                pass

            cp.channels[idx] = ch
            set_channel_enable_bit(cp, idx)

            try:
                new_bytes = set_channel_zones_raw(cp, idx, zones)
            except ZoneError as e:
                return False, (
                    f"Не удалось обновить зоны: {e}. Изменения не сохранены."
                )

            state.snapshot()
            state.commit(
                new_bytes,
                f"Канал #{idx + 1} сохранён. Запись в рацию — «Записать»",
            )
            return True, f"Канал #{idx + 1} сохранён"

        except Exception as e:
            _log(f"apply_editor error {e!r}")
            return False, f"Не удалось сохранить канал: {e}"


def delete_channel(state, idx: int) -> tuple[bool, str]:
    with state.lock:
        if state.data is None:
            return False, "Кодплаг не загружен"

        try:
            cp = Codeplug(state.data)
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
            return False, f"Канал не удалён: {e}"
        except Exception as e:
            return False, f"Не удалось удалить канал: {e}"

        state.snapshot()
        state.commit(
            new_bytes,
            f"Канал #{idx + 1} удалён и убран из зон. Запись — «Записать»",
        )
        return True, f"Канал #{idx + 1} удалён"


def duplicate_channel(state, idx: int) -> tuple[bool, str]:
    with state.lock:
        if state.data is None:
            return False, "Кодплаг не загружен"

        try:
            cp = Codeplug(state.data)
            used = used_indices(cp)
            free = 0

            while free in used and free < len(cp.channels):
                free += 1

            if free >= len(cp.channels):
                return False, "Нет свободных слотов"

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

            zones = {zid for zid, _ in state.zone_map.get(idx, [])}

            try:
                new_bytes = set_channel_zones_raw(cp, free, zones)
            except ZoneError as e:
                return False, f"Не удалось скопировать зоны: {e}"

        except Exception as e:
            return False, f"Не удалось дублировать канал: {e}"

        state.snapshot()
        state.commit(
            new_bytes,
            f"Канал #{idx + 1} скопирован в слот #{free + 1}",
        )
        return True, f"Скопирован в слот #{free + 1}"


# ---------------------------------------------------------------------------
# Зоны
# ---------------------------------------------------------------------------

def rename_zone(state, zid: int, name: str) -> tuple[bool, str]:
    with state.lock:
        if state.data is None:
            return False, "Кодплаг не загружен"

        try:
            new_bytes = rename_zone_raw(state.codeplug(), zid, name)
        except ZoneError as e:
            return False, str(e)

        state.snapshot()
        state.commit(new_bytes, f"Зона {zid} переименована в «{name}»")
        return True, f"Зона {zid} переименована"


def create_zone(state, name: str) -> tuple[bool, str]:
    with state.lock:
        if state.data is None:
            return False, "Кодплаг не загружен"

        old_count = len(state.zone_labels())

        try:
            new_bytes = create_zone_raw(state.codeplug(), name)
        except ZoneError as e:
            return False, str(e)

        state.snapshot()
        state.commit(new_bytes, f"Зона «{name}» создана (ID {old_count})")
        return True, f"Зона «{name}» создана (ID {old_count})"


def delete_zone(state, zid: int) -> tuple[bool, str]:
    with state.lock:
        if state.data is None:
            return False, "Кодплаг не загружен"

        try:
            new_bytes = delete_zone_raw(state.codeplug(), zid)
        except ZoneError as e:
            return False, str(e)

        state.snapshot()
        state.commit(
            new_bytes,
            f"Зона {zid} удалена. ID последующих зон сдвинуты.",
        )
        return True, f"Зона {zid} удалена"


def parse_zone_ids(state, s: str) -> set[int]:
    s = (s or "").strip()

    if not s:
        return set()

    if any(ch in s for ch in ",;"):
        parts = re.split(r"[,;]+", s)
    else:
        parts = re.split(r"\s+", s)

    ids = set()
    labels = state.zone_labels()

    for token in parts:
        token = token.strip().lstrip("#")

        if not token:
            continue

        if token.isdigit():
            zid = int(token)

            if zid not in {z for z, _ in labels}:
                raise ValueError(f"зона {zid} не найдена")

            ids.add(zid)
            continue

        tl = token.casefold()
        exact = [zid for zid, name in labels if name.casefold() == tl]

        if len(exact) == 1:
            ids.add(exact[0])
            continue

        startswith = [
            zid for zid, name in labels if name.casefold().startswith(tl)
        ]

        if len(startswith) == 1:
            ids.add(startswith[0])
            continue

        contains = [zid for zid, name in labels if tl in name.casefold()]

        if len(contains) == 1:
            ids.add(contains[0])
            continue

        raise ValueError(f"не удалось распознать зону: {token!r}")

    return ids


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def export_csv(state) -> tuple[str, str]:
    """Строки CSV в формате TUI (разделитель «;», BOM добавляет view)."""
    buf = io.StringIO()
    fields = list(CSV_FIELDS)

    if "zones" not in fields:
        fields.append("zones")

    writer = csv.writer(buf, delimiter=";")
    writer.writerow(fields)

    cp = state.codeplug()

    for ch in cp.channels:
        if not _channel_valid(ch):
            continue

        zone_ids = ",".join(
            str(zid) for zid, _ in state.zone_map.get(ch.index, [])
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

    name = f"channels_{datetime.now():%Y%m%d_%H%M%S}.csv"
    return name, buf.getvalue()


def parse_csv(state, text: str) -> tuple[list[ParsedRow], list[str]]:
    rows: list[ParsedRow] = []
    errors: list[str] = []

    reader = csv.DictReader(io.StringIO(text), delimiter=";")

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
            contact_num = parse_contact_num(raw.get("contact_num", ""))
        except ValueError as e:
            errors.append(f"Строка {line}: {e}")
            continue

        zones: set[int] = set()
        zone_error = None
        spec = (raw.get("zones") or "").strip()

        if spec:
            try:
                zones = parse_zone_ids(state, spec)
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


def apply_import(state, rows: list[ParsedRow]) -> tuple[bool, str]:
    with state.lock:
        if state.data is None or not rows:
            return False, "Кодплаг не загружен или список каналов пуст"

        cp = Codeplug(state.data)
        used = used_indices(cp)
        template = _find_channel_template(cp)

        nxt = 0
        added = 0
        zone_failed = 0

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
            return False, (
                "Импорт не добавил ни одного канала. "
                "Проверьте ошибки в предпросмотре."
            )

        state.snapshot()
        extra = f", ошибок зон: {zone_failed}" if zone_failed else ""
        state.commit(
            bytes(cp),
            f"Импорт: добавлено {added}{extra}. Запись — «Записать»",
        )
        return True, f"Импортировано каналов: {added}"


# ---------------------------------------------------------------------------
# Backup
# ---------------------------------------------------------------------------

def make_backup(state) -> tuple[bool, str]:
    with state.lock:
        if state.data is None:
            return False, "Кодплаг не загружен"

        path = f"codeplug_backup_{datetime.now():%Y%m%d_%H%M%S}.g77"

        try:
            Path(path).write_bytes(state.data)
        except Exception as e:
            return False, f"Не удалось сохранить backup: {e}"

        state.notify(f"Backup сохранён: {path}")
        return True, path
