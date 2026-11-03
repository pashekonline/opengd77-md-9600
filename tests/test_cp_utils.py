import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))

from opengd77.codeplug import Channel, Codeplug

import gd77_patch  # noqa: F401 применяет патчи BlockView
from cp_utils import (
    ValidationError,
    _channel_valid,
    clear_channel_enable_bit,
    is_empty_channel,
    normalize_mode,
    parse_color_code,
    parse_contact_num,
    parse_frequency_mhz,
    parse_tone_hz,
    set_channel_enable_bit,
    set_tone_value,
    tone_to_str,
    validate_channel_name,
)
from gd77_zones import _raw_zone_slots, get_zones, set_channel_zones_raw


def _blank_cp() -> Codeplug:
    cp = Codeplug()
    return cp


def test_is_empty_channel():
    ch = Channel()
    ch.data[:] = b"\x00" * len(ch.data)
    assert is_empty_channel(ch)

    ch.data[:] = b"\xff" * len(ch.data)
    assert not is_empty_channel(ch), "0xFF-слот не пустой: рация покажет 1666 МГц"

    ch.name = "TEST"
    assert not is_empty_channel(ch)


def test_channel_valid():
    ch = Channel()
    ch.data[:] = b"\x00" * len(ch.data)
    ch.name = "TEST"
    ch.rx_freq = 145_500_000
    ch.tx_freq = 145_500_000
    assert _channel_valid(ch)

    ch2 = Channel()
    ch2.data[:] = b"\xff" * len(ch2.data)
    assert not _channel_valid(ch2), "0xFF-мусор не должен быть валидным"


def test_enable_bit_roundtrip():
    cp = _blank_cp()

    # изначально биты установлены (0xFF-память)
    assert set_channel_enable_bit(cp, 95) is False
    assert clear_channel_enable_bit(cp, 95) is True
    assert clear_channel_enable_bit(cp, 95) is False, "повторный сброс — no-op"
    assert set_channel_enable_bit(cp, 95) is True
    assert set_channel_enable_bit(cp, 95) is False, "повторная установка — no-op"


def test_enable_bit_flash_bank():
    cp = _blank_cp()
    # канал 200 — во FLASH банке 1 (0xCD C0 блок)
    assert set_channel_enable_bit(cp, 200) is False
    assert clear_channel_enable_bit(cp, 200) is True


def test_delete_channel_full_cycle():
    """Удаление: слот нулями + сброс бита + удаление из зон."""
    data = bytearray(b"\xff" * Codeplug.SIZE)
    cp = Codeplug(bytes(data))

    # создаём канал 5 и добавляем в зону 0
    ch = Channel()
    ch.data[:] = b"\x00" * len(ch.data)
    ch.name = "CH5"
    ch.rx_freq = 145_500_000
    ch.tx_freq = 145_500_000
    cp.channels[5] = ch
    set_channel_enable_bit(cp, 5)

    before = bytes(cp)
    new_bytes = set_channel_zones_raw(cp, 5, set())

    cp2 = Codeplug(new_bytes)
    ch2 = Channel()
    ch2.data[:] = b"\x00" * len(ch2.data)
    cp2.channels[5] = ch2
    clear_channel_enable_bit(cp2, 5)

    after = bytes(cp2)
    assert after != before
    # слот чистый
    assert cp2.channels[5].name == ""
    # зоны не ссылаются на канал 5 (0-based) => 6 (1-based)
    for z in get_zones(cp2):
        raw = _raw_zone_slots(z)[1] or []
        assert 6 not in raw


def test_parse_frequency_accepts_comma_and_dot():
    assert parse_frequency_mhz("145.500") == 145_500_000
    assert parse_frequency_mhz("145,5") == 145_500_000
    assert parse_frequency_mhz(" 1 ") == 1_000_000

    for bad in ("", "abc", "0", "-1", "1001"):
        try:
            parse_frequency_mhz(bad)
        except ValidationError:
            pass
        else:
            raise AssertionError(f"должно быть ошибкой: {bad!r}")


def test_parse_tone():
    assert parse_tone_hz("") is None
    assert parse_tone_hz("0") is None
    assert parse_tone_hz("88.5") == 885
    assert parse_tone_hz("88,5") == 885

    for bad in ("10", "300", "abc"):
        try:
            parse_tone_hz(bad)
        except ValidationError:
            pass
        else:
            raise AssertionError(f"должно быть ошибкой: {bad!r}")


def test_validate_channel_name():
    assert validate_channel_name("  CALL  ") == "CALL"

    for bad in ("", "   ", "ПРИВЕТ", "A" * 17, "A\x00B"):
        try:
            validate_channel_name(bad)
        except ValidationError:
            pass
        else:
            raise AssertionError(f"должно быть ошибкой: {bad!r}")


def test_parse_color_and_contact():
    assert parse_color_code("") == 0
    assert parse_color_code("15") == 15
    assert parse_contact_num("") == 0
    assert parse_contact_num("12345") == 12345

    for fn, bad in ((parse_color_code, "16"), (parse_contact_num, "abc")):
        try:
            fn(bad)
        except ValidationError:
            pass
        else:
            raise AssertionError(f"должно быть ошибкой: {bad!r}")


def test_normalize_mode():
    assert normalize_mode("FM") == 0
    assert normalize_mode("dmr") == 1
    assert normalize_mode("") == 0

    try:
        normalize_mode("AM")
    except ValidationError:
        pass
    else:
        raise AssertionError("AM должен быть ошибкой")


def test_tone_zero_is_none_and_roundtrip():
    ch = Channel()
    ch.data[:] = b"\x00" * len(ch.data)
    assert tone_to_str(ch, "rx_tone") == ""

    set_tone_value(ch, "rx_tone", 885)
    assert tone_to_str(ch, "rx_tone") == "88.5"

    set_tone_value(ch, "rx_tone", None)
    assert tone_to_str(ch, "rx_tone") == ""


if __name__ == "__main__":
    test_is_empty_channel()
    test_channel_valid()
    test_enable_bit_roundtrip()
    test_enable_bit_flash_bank()
    test_delete_channel_full_cycle()
    test_parse_frequency_accepts_comma_and_dot()
    test_parse_tone()
    test_validate_channel_name()
    test_parse_color_and_contact()
    test_normalize_mode()
    test_tone_zero_is_none_and_roundtrip()
    print("all tests passed")
