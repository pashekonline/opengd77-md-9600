import sys
from dataclasses import fields as dc_fields
from dataclasses import is_dataclass

from _vendor_path import _VENDOR  # noqa: F401

if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
from opengd77.binvar import BlockView
from opengd77.codeplug import Codeplug, MemType, Zone, ZonesView

from debug_log import _log

CODE_VERSION = "md9600-clean-v1.1"


def _fixed_getitem(self, key):
    key = self._normalize_key(key)
    cb = self._find_chunk_block(key)
    chunk = cb.chunk(self.buf, key)
    obj = self.cls.from_buffer(chunk)
    obj.index = key
    return obj


BlockView.__getitem__ = _fixed_getitem

_orig_setitem = getattr(BlockView, "__setitem__", None)


def _fixed_setitem(self, key, value):
    try:
        key = self._normalize_key(key)
        cb = self._find_chunk_block(key)
        chunk = cb.chunk(self.buf, key)

        raw = bytes(getattr(value, "data", value))
        n = len(chunk)

        raw = raw[:n]
        if len(raw) < n:
            raw += b"\xff" * (n - len(raw))

        chunk[:] = raw

    except Exception:
        if _orig_setitem is not None:
            return _orig_setitem(self, key, value)
        raise


BlockView.__setitem__ = _fixed_setitem


def _zone_bitmap_slots(view) -> list[int]:
    """
    Битовая карта зон, как у каналов: bit i = байт i//8, маска 1<<(i%8).
    Вендореная ZonesView считает биты MSB-first и поэтому теряет зоны
    64–67; здесь считаем LSB-first и не теряем их.
    """
    slots = []
    zbytes = view.zbytes

    for slot in range(view.SIZE):
        byte_i = slot // 8

        if byte_i >= len(zbytes):
            break

        if zbytes[byte_i] & (1 << (slot % 8)):
            slots.append(slot)

    return slots


def _fixed_zones_len(self):
    return len(_zone_bitmap_slots(self))


def _fixed_zones_getitem(self, key):
    # Вендореная версия проверяет key >= len(self), а len — это количество
    # включённых зон. При разреженной битовой карте это ломает доступ по
    # реальному номеру слота, поэтому проверяем границу SIZE.
    if not isinstance(key, int):
        raise KeyError("only simple indexing is supported")

    if key < 0:
        key = self.SIZE + key

    if key < 0 or key >= self.SIZE:
        raise KeyError(f"{key} out of range (have {self.SIZE})")

    addr = self._find_addr_by_index(key)

    if addr is None:
        raise KeyError(f"{key} out of range")

    chunk = self.buf[addr: addr + 16 + (2 * self.ch_per_zone)]

    try:
        z = Zone(chunk)
    except Exception as e:
        raise KeyError(f"{key} internal error: {e}") from e

    z.index = key
    return z


def _fixed_zones_iter(self):
    for slot in _zone_bitmap_slots(self):
        yield self[slot]


ZonesView.__len__ = _fixed_zones_len
ZonesView.__getitem__ = _fixed_zones_getitem
ZonesView.__iter__ = _fixed_zones_iter


def _part_tuple(part):
    """
    Универсально достает (memtype, file_addr, radio_addr, length/size)
    из CPPart, независимо от того, как он устроен в библиотеке.
    """
    name_sets = (
        ("memtype", "file_addr", "radio_addr", "length"),
        ("memtype", "file_addr", "radio_addr", "size"),
        ("type", "file_addr", "radio_addr", "length"),
        ("type", "file_addr", "radio_addr", "size"),
        ("memtype", "file", "radio", "length"),
        ("memtype", "file", "radio", "size"),
        ("type", "file", "radio", "length"),
        ("type", "file", "radio", "size"),
    )

    for names in name_sets:
        try:
            vals = tuple(int(getattr(part, n)) for n in names)
            if len(vals) == 4:
                return vals
        except Exception:
            pass

    try:
        vals = tuple(int(x) for x in part)
        if len(vals) >= 4:
            return vals[:4]
    except Exception:
        pass

    try:
        if is_dataclass(part):
            vals = []
            for f in dc_fields(part):
                v = getattr(part, f.name)
                try:
                    vals.append(int(v))
                except Exception:
                    vals.append(v)

            if len(vals) >= 4:
                try:
                    return tuple(int(x) for x in vals[:4])
                except Exception:
                    pass
    except Exception:
        pass

    return None


def _fix_parts() -> None:
    Codeplug.parts = (
        Codeplug.CPPart(MemType.FLASH, 0x00E0, 0x00E0, 0x6000 - 0x00E0),
        Codeplug.CPPart(MemType.FLASH, 0x7500, 0x7500, 0xB000 - 0x7500),
        Codeplug.CPPart(MemType.FLASH, 0xB000, 0x7B000, 0x13E60),
        Codeplug.CPPart(MemType.FLASH, 0x1EE60, 0x00000, 0x11A0),
    )

    logged = []

    for p in Codeplug.parts:
        t = _part_tuple(p)

        if t is not None and len(t) >= 4:
            try:
                logged.append(
                    f"{int(t[0])}:{hex(int(t[1]))}->{hex(int(t[2]))}:{hex(int(t[3]))}"
                )
            except Exception:
                logged.append(repr(t))
        else:
            logged.append(repr(p))

    _log("parts=" + "; ".join(logged))


_fix_parts()
_log(CODE_VERSION)
