import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))

from opengd77.codeplug import Channel, Codeplug

import gd77_patch  # noqa: F401 применяет патчи BlockView
from cp_utils import set_channel_enable_bit
from gd77_zones import (
    ZONE_MAX_SLOTS,
    ZoneError,
    create_zone_raw,
    delete_zone_raw,
    get_zones,
    rename_zone_raw,
    zone_id,
    zone_members_map,
    zone_name,
)


def _clean_cp() -> Codeplug:
    cp = Codeplug(bytes(Codeplug.SIZE))

    for idx, name in ((0, "A"), (1, "B"), (2, "C")):
        ch = Channel()
        ch.data[:] = b"\x00" * len(ch.data)
        ch.name = name
        ch.rx_freq = 145_500_000
        ch.tx_freq = 145_000_000
        cp.channels[idx] = ch
        set_channel_enable_bit(cp, idx)

    return cp


def _names(cp) -> list[str]:
    return [
        zone_name(z, zone_id(z, i))
        for i, z in enumerate(get_zones(cp))
    ]


def test_create_rename_delete_zone():
    cp = _clean_cp()

    cp = Codeplug(create_zone_raw(cp, "HOME", [0, 1]))
    assert _names(cp) == ["HOME"]
    assert zone_members_map(cp) == {0: [0, 1]}

    cp = Codeplug(rename_zone_raw(cp, 0, "HOME2"))
    assert _names(cp) == ["HOME2"]
    assert zone_members_map(cp) == {0: [0, 1]}

    cp = Codeplug(create_zone_raw(cp, "MOBILE", [2]))
    assert _names(cp) == ["HOME2", "MOBILE"]
    assert zone_members_map(cp) == {0: [0, 1], 1: [2]}

    cp = Codeplug(delete_zone_raw(cp, 0))
    assert _names(cp) == ["MOBILE"]
    assert zone_members_map(cp) == {0: [2]}


def test_zone_name_validation():
    cp = _clean_cp()
    cp = Codeplug(create_zone_raw(cp, "HOME"))

    with pytest.raises(ZoneError):
        create_zone_raw(cp, "home")

    with pytest.raises(ZoneError):
        create_zone_raw(cp, "")

    with pytest.raises(ZoneError):
        create_zone_raw(cp, "ДОМ")

    with pytest.raises(ZoneError):
        create_zone_raw(cp, "X" * 17)

    with pytest.raises(ZoneError):
        rename_zone_raw(cp, 99, "NEW")


def test_zone_slots_limit():
    cp = _clean_cp()

    for i in range(ZONE_MAX_SLOTS):
        cp = Codeplug(create_zone_raw(cp, f"Z{i}"))

    assert len(get_zones(cp)) == ZONE_MAX_SLOTS

    with pytest.raises(ZoneError):
        create_zone_raw(cp, "OVERFLOW")


def test_delete_zone_compacts_ids():
    cp = _clean_cp()
    cp = Codeplug(create_zone_raw(cp, "A"))
    cp = Codeplug(create_zone_raw(cp, "B"))
    cp = Codeplug(create_zone_raw(cp, "C"))

    cp = Codeplug(delete_zone_raw(cp, 1))
    assert _names(cp) == ["A", "C"]

    # ID зон после удаления идут подряд, без дыр.
    assert [zone_id(z, i) for i, z in enumerate(get_zones(cp))] == [0, 1]
