import sys
from decimal import Decimal, InvalidOperation

from _vendor_path import _VENDOR  # noqa: F401

if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
from opengd77.codeplug import Channel

TONE_NONE_RAW = b"\xff\xff"

NAME_MAX_LEN = 16
FREQ_MIN_HZ = 1_000_000
FREQ_MAX_HZ = 1_000_000_000
TONE_MIN_HZ = 60.0
TONE_MAX_HZ = 260.0
COLOR_CODE_MAX = 15
CONTACT_MAX = 65535

CSV_FIELDS = [
    "index",
    "name",
    "rx_freq",
    "tx_freq",
    "mode",
    "rx_tone",
    "tx_tone",
    "tx_color",
    "contact_num",
]

MODES = {0: "FM", 1: "DMR"}
MODES_REV = {v: k for k, v in MODES.items()}


class ValidationError(ValueError):
    """Понятная пользователю ошибка валидации поля."""


def _decimal(text: str, label: str) -> Decimal:
    s = (text or "").strip().replace(",", ".")

    if not s:
        raise ValidationError(f"{label}: значение не заполнено")

    try:
        return Decimal(s)
    except (InvalidOperation, ValueError) as e:
        raise ValidationError(
            f"{label}: «{text.strip()}» не число (пример: 145.500)"
        ) from e


def parse_frequency_mhz(text: str, label: str = "Частота") -> int:
    """Строка в МГц -> Гц. Принимает и точку, и запятую."""
    value = _decimal(text, label)

    if value <= 0:
        raise ValidationError(f"{label}: частота должна быть больше нуля")

    hz = int(value * 1_000_000)

    if not (FREQ_MIN_HZ <= hz <= FREQ_MAX_HZ):
        raise ValidationError(
            f"{label}: допустимый диапазон 1–1000 МГц"
        )

    return hz


def parse_tone_hz(text: str, label: str = "Тон CTCSS") -> int | None:
    """Строка в Гц -> десятые доли Гц. Пустая строка = тона нет."""
    s = (text or "").strip()

    if not s:
        return None

    value = _decimal(s, label)

    if value == 0:
        return None

    if not (Decimal(str(TONE_MIN_HZ)) <= value <= Decimal(str(TONE_MAX_HZ))):
        raise ValidationError(
            f"{label}: допустимый диапазон {TONE_MIN_HZ:g}–{TONE_MAX_HZ:g} Гц"
        )

    return int(value * 10)


def validate_channel_name(text: str) -> str:
    name = (text or "").strip()

    if not name:
        raise ValidationError("Имя канала не может быть пустым")

    if len(name) > NAME_MAX_LEN:
        raise ValidationError(
            f"Имя канала: максимум {NAME_MAX_LEN} символов"
        )

    if not name.isascii() or not name.isprintable():
        raise ValidationError(
            "Имя канала: только ASCII-символы (рация не покажет кириллицу)"
        )

    return name


def parse_color_code(text: str) -> int:
    s = (text or "").strip()

    if not s:
        return 0

    try:
        value = int(s)
    except ValueError as e:
        raise ValidationError("Color code: введите целое число 0–15") from e

    if not (0 <= value <= COLOR_CODE_MAX):
        raise ValidationError("Color code: допустимо 0–15")

    return value


def parse_contact_num(text: str) -> int:
    s = (text or "").strip()

    if not s:
        return 0

    try:
        value = int(s)
    except ValueError as e:
        raise ValidationError("Номер контакта: введите целое число") from e

    if not (0 <= value <= CONTACT_MAX):
        raise ValidationError(f"Номер контакта: допустимо 0–{CONTACT_MAX}")

    return value


def normalize_mode(text: str) -> int:
    s = (text or "").strip().upper()

    if s in ("", "FM", "ANALOG", "ANALOGUE", "0"):
        return 0

    if s in ("DMR", "DIGITAL", "1"):
        return 1

    raise ValidationError("Режим: выберите FM или DMR")


def _channel_valid(ch) -> bool:
    try:
        return (
            len(ch.name) > 0
            and ch.name.isprintable()
            and FREQ_MIN_HZ <= ch.rx_freq <= FREQ_MAX_HZ
            and FREQ_MIN_HZ <= ch.tx_freq <= FREQ_MAX_HZ
        )
    except Exception:
        return False


def is_empty_channel(ch) -> bool:
    try:
        raw = bytes(ch.data)
    except Exception:
        return False

    return all(b == 0x00 for b in raw)


def _enable_bit_offset(cp, idx: int):
    try:
        blocks = type(cp).channel_blocks
    except Exception:
        return None

    for cb in blocks:
        i = idx - cb.index_offset

        if 0 <= i < cb.item_count:
            return cb.raw_offset + i // 8, 1 << (i % 8)

    return None


def set_channel_enable_bit(cp, idx: int) -> bool:
    """
    Устанавливает бит включения канала в enable-bitmap его банка.
    Без бита рация не показывает канал, даже если слот заполнен.
    """
    off = _enable_bit_offset(cp, idx)

    if off is None:
        return False

    byte_i, mask = off

    try:
        if not (cp.data[byte_i] & mask):
            cp.data[byte_i] |= mask
            return True
    except Exception:
        pass

    return False


def clear_channel_enable_bit(cp, idx: int) -> bool:
    """
    Сбрасывает бит включения канала в enable-bitmap его банка.
    Рация показывает канал, если бит установлен, даже если слот пустой.
    Возвращает True, если бит был сброшен.
    """
    off = _enable_bit_offset(cp, idx)

    if off is None:
        return False

    byte_i, mask = off

    try:
        if cp.data[byte_i] & mask:
            cp.data[byte_i] &= ~mask & 0xFF
            return True
    except Exception:
        pass

    return False


def used_indices(cp) -> set[int]:
    used = set()

    for c in cp.channels:
        try:
            if getattr(c, "valid", False) or _channel_valid(c):
                used.add(c.index)
        except Exception:
            pass

    return used


def _find_channel_template(cp) -> Channel:
    if cp is not None:
        try:
            for c in cp.channels:
                try:
                    if getattr(c, "valid", False) or _channel_valid(c):
                        return c
                except Exception:
                    pass
        except Exception:
            pass

    ch = Channel()

    try:
        ch.valid = True
    except Exception:
        pass

    return ch


def _new_channel(cp=None, template: Channel | None = None) -> Channel:
    tmpl = template if template is not None else _find_channel_template(cp)
    ch = Channel()

    try:
        ch.data[:] = tmpl.data[:]
    except Exception:
        pass

    try:
        ch.valid = True
    except Exception:
        pass

    return ch


def tone_to_str(ch, attr: str) -> str:
    off = 32 if attr == "rx_tone" else 34
    raw = bytes(ch.data[off:off + 2])
    v = getattr(ch, attr)

    # 0xFFFF — штатное «тона нет»; 0 — тоже пустое значение,
    # которое встречается в нулевых/шаблонных слотах.
    if raw == TONE_NONE_RAW or raw == b"\x00\x00" or v == 0:
        return ""

    return f"{v / 10:.1f}"


def set_tone_value(ch, attr: str, tenths: int | None) -> None:
    off = 32 if attr == "rx_tone" else 34

    if tenths is None:
        ch.data[off:off + 2] = TONE_NONE_RAW
        return

    setattr(ch, attr, int(tenths))


def set_tone(ch, attr: str, s: str) -> None:
    set_tone_value(ch, attr, parse_tone_hz(s))


def freq_to_str(v: int) -> str:
    return f"{v / 1_000_000:.5f}".rstrip("0").rstrip(".")


def str_to_freq(s: str) -> int:
    return parse_frequency_mhz(s)
