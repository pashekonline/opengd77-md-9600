from __future__ import annotations

from debug_log import _log

CHANNEL_MAX = 1024

# Raw-раскладка зон в кодплаге MD-9600 / OpenGD77.
ZONE_BITMAP_OFFSET = 0x8010
ZONE_BITMAP_SIZE = 32
ZONE_RECORD_OFFSET = 0x8030
ZONE_RECORD_SIZE = 176
ZONE_MAX_SLOTS = 68          # столько зон видит вендореная библиотека
ZONE_CHANNEL_CAP = 80

# В OpenGD77 пустые слоты зоны обычно 0x0000.
# 0xFFFF тоже считаем пустым на всякий случай.
EMPTY_VALUES = {0x0000, 0xFFFF}

# Зоны OpenGD77 хранят номера каналов как 1..1024.
_DEFAULT_ONE_BASED = True

_ZONE_ATTRS = (
    "channel_nums",
    "channels",
    "channel_indices",
    "members",
    "channel_list",
    "channel_numbers",
)

_ZONE_DIAG_LOGGED = False


class ZoneError(RuntimeError):
    pass


def _is_empty_slot(v: int) -> bool:
    return v in EMPTY_VALUES


def _is_valid_channel_index(v: int) -> bool:
    return isinstance(v, int) and 0 <= v < CHANNEL_MAX


def _norm_slot(v) -> int:
    if v is None:
        return 0x0000

    if isinstance(v, bool):
        return 0x0000

    if isinstance(v, int):
        return v & 0xFFFF

    for attr in ("index", "value", "channel", "number", "mem"):
        try:
            x = getattr(v, attr)
        except Exception:
            continue

        if isinstance(x, bool):
            continue

        if isinstance(x, int):
            return x & 0xFFFF

        try:
            return int(x) & 0xFFFF
        except Exception:
            pass

    if isinstance(v, (bytes, bytearray, memoryview)):
        b = bytes(v)
        if len(b) >= 2:
            return int.from_bytes(b[:2], "little")

    return 0x0000


def _as_list(obj):
    if obj is None:
        return None

    if isinstance(obj, (str, bytes, bytearray, memoryview)):
        return None

    if isinstance(obj, (list, tuple)):
        return list(obj)

    if hasattr(obj, "values") and callable(getattr(obj, "values")):
        try:
            return list(obj.values())
        except Exception:
            pass

    try:
        return list(obj)
    except TypeError:
        try:
            return [obj]
        except Exception:
            return None
    except Exception:
        return None


def get_zones(cp):
    if cp is None:
        return []

    for attr in (
        "zones",
        "zone_list",
        "zones_list",
        "zone_array",
        "zones_array",
    ):
        try:
            obj = getattr(cp, attr)
        except Exception:
            continue

        if obj is None:
            continue

        if callable(obj):
            try:
                obj = obj()
            except Exception:
                continue

        lst = _as_list(obj)

        if lst is not None:
            return lst

    return []


def zone_id(z, fallback_index: int) -> int:
    for attr in ("index", "id", "number", "zone_id", "zone_number"):
        try:
            v = getattr(z, attr)
        except Exception:
            continue

        if isinstance(v, bool):
            continue

        if isinstance(v, int):
            return v

        try:
            return int(v)
        except Exception:
            pass

    return fallback_index


def zone_name(z, fallback_id: int | None = None) -> str:
    for attr in ("name", "zone_name", "label", "title"):
        try:
            v = getattr(z, attr)
        except Exception:
            continue

        if isinstance(v, str):
            s = v.strip("\x00\xff ").strip()
            if s:
                return s

        if isinstance(v, (bytes, bytearray, memoryview)):
            b = bytes(v)
            s = b.split(b"\x00", 1)[0].decode("utf-8", "replace")
            s = s.strip("\x00\xff ").strip()
            if s:
                return s

    if fallback_id is None:
        return "Zone ?"

    return f"Zone {fallback_id}"


def _zone_raw_size(z) -> int:
    for attr in ("SIZE", "size", "length", "data_size"):
        try:
            v = getattr(z, attr)
        except Exception:
            continue

        try:
            return int(v)
        except Exception:
            pass

    try:
        return len(bytes(z.data))
    except Exception:
        return 0


def _zone_record_size(z) -> int:
    size = _zone_raw_size(z)

    if size < 18:
        try:
            size = len(bytes(z.data))
        except Exception:
            size = 176

    if size < 18:
        size = 176

    return size


def _zone_capacity_from_size(size: int) -> int:
    if size <= 16:
        return 0

    return (size - 16) // 2


def _parse_zone_data(z):
    """
    Формат OpenGD77 зоны:
      0x00..0x0F  имя, 16 байт
      0x10..      массив uint16 LE номеров каналов

    Пустые слоты: 0x0000.
    """
    try:
        data = bytes(z.data)
    except Exception:
        return None, None, 0

    size = _zone_record_size(z)

    if size <= 0 or size > len(data):
        size = len(data)

    offset = 16

    if offset >= size:
        return None, None, 0

    capacity = _zone_capacity_from_size(size)

    if capacity <= 0 or capacity > CHANNEL_MAX:
        return None, None, 0

    slots = []

    for i in range(capacity):
        p = offset + i * 2
        b = data[p:p + 2]

        if len(b) != 2:
            slots.append(0x0000)
        else:
            slots.append(int.from_bytes(b, "little"))

    return slots, offset, capacity


def _raw_slots_from_attr(z):
    for attr in _ZONE_ATTRS:
        try:
            seq = getattr(z, attr)
        except Exception:
            continue

        lst = _as_list(seq)

        if lst is None:
            continue

        return attr, [_norm_slot(x) for x in lst]

    return None, None


def _raw_zone_slots(z):
    """
    Возвращает:
      (attr_name, raw_slots, capacity, data_offset)

    Для чтения авторитетнее zone.data, потому что channel_nums
    может отдавать только заполненные слоты и терять реальную ёмкость.
    """
    data_slots, data_offset, data_capacity = _parse_zone_data(z)

    if data_slots is not None and data_capacity > 0:
        return "data", data_slots, data_capacity, data_offset

    attr, attr_slots = _raw_slots_from_attr(z)

    if attr_slots is not None:
        return attr or "channel_nums", attr_slots, len(attr_slots), None

    return None, None, 0, None


def _detect_empty_marker(raw_slots) -> int:
    """
    Определяет, каким значением заполнены пустые слоты.
    Для OpenGD77 почти всегда 0x0000.
    """
    if not raw_slots:
        return 0x0000

    zero_count = sum(1 for v in raw_slots if v == 0x0000)
    ffff_count = sum(1 for v in raw_slots if v == 0xFFFF)

    if zero_count >= ffff_count:
        return 0x0000

    return 0xFFFF


def _detect_one_based(raw_slots, cp=None) -> bool:
    """
    OpenGD77 зоны используют 1-based номера каналов.
    Пустые слоты 0x0000 / 0xFFFF игнорируются.
    """
    vals = [
        v for v in (raw_slots or [])
        if not _is_empty_slot(v)
    ]

    if not vals:
        return _DEFAULT_ONE_BASED

    # Если есть 1024, это точно 1-based, потому что 0-based максимум 1023.
    if any(v == CHANNEL_MAX for v in vals):
        return True

    # Если есть значения > 1024, 1-based невозможен.
    if any(v < 0 or v > CHANNEL_MAX for v in vals):
        return False

    # Дополнительно сверяемся с валидностью каналов, если есть cp.
    if cp is not None:
        score0 = 0
        score1 = 0

        try:
            for v in vals:
                if 0 <= v < CHANNEL_MAX:
                    try:
                        ch = cp.channels[v]
                        if getattr(ch, "valid", False):
                            score0 += 1
                    except Exception:
                        pass

                if 1 <= v <= CHANNEL_MAX:
                    try:
                        ch = cp.channels[v - 1]
                        if getattr(ch, "valid", False):
                            score1 += 1
                    except Exception:
                        pass
        except Exception:
            pass

        if score1 > score0:
            return True

        if score0 > score1:
            return False

    return _DEFAULT_ONE_BASED


def _zone_members(z, cp=None) -> list[int]:
    """
    Возвращает 0-based индексы каналов зоны.
    """
    attr, raw, cap, offset = _raw_zone_slots(z)

    if raw is None:
        return []

    one_based = _detect_one_based(raw, cp)

    out = []
    seen = set()

    for v in raw:
        if _is_empty_slot(v):
            continue

        if one_based:
            if 1 <= v <= CHANNEL_MAX:
                idx = v - 1
            else:
                continue
        else:
            if 0 <= v < CHANNEL_MAX:
                idx = v
            else:
                continue

        if _is_valid_channel_index(idx) and idx not in seen:
            seen.add(idx)
            out.append(idx)

    return out


def zone_members(z) -> list[int]:
    return _zone_members(z, None)


def _pack_zone_slots(vals) -> bytes:
    return b"".join(
        int(v & 0xFFFF).to_bytes(2, "little")
        for v in vals
    )


def set_zone_members(z, members, cp=None) -> None:
    """
    Старый объектный способ. Оставлен для совместимости,
    но основным теперь должен быть set_channel_zones_raw().
    """
    clean = []
    seen = set()

    for m in members or ():
        try:
            im = int(m)
        except Exception:
            continue

        if not _is_valid_channel_index(im):
            continue

        if im in seen:
            continue

        seen.add(im)
        clean.append(im)

    attr, raw, cap, offset = _raw_zone_slots(z)

    if cap <= 0:
        raise ZoneError("не удалось определить ёмкость зоны")

    if offset is None:
        offset = 16

    empty_marker = _detect_empty_marker(raw or [])
    one_based = _detect_one_based(raw or [], cp)

    vals = []

    for idx in clean[:cap]:
        vals.append(idx + 1 if one_based else idx)

    while len(vals) < cap:
        vals.append(empty_marker)

    vals = vals[:cap]

    try:
        data = z.data
    except Exception as e:
        raise ZoneError(f"нет доступа к zone.data: {e!r}") from e

    try:
        size = int(getattr(z, "SIZE", len(bytes(data))))
    except Exception:
        size = len(bytes(data))

    if size <= offset:
        raise ZoneError("некорректный размер zone.data для записи каналов")

    max_by_size = (size - offset) // 2

    if max_by_size <= 0:
        raise ZoneError("в zone.data нет места для массива каналов")

    if cap > max_by_size:
        cap = max_by_size
        vals = vals[:cap]

        while len(vals) < cap:
            vals.append(empty_marker)

    packed = _pack_zone_slots(vals)

    assigned = False

    try:
        if isinstance(data, bytearray):
            data[offset:offset + len(packed)] = packed
            assigned = True
    except Exception:
        assigned = False

    if not assigned:
        try:
            if isinstance(data, memoryview) and not getattr(data, "readonly", False):
                data[offset:offset + len(packed)] = packed
                assigned = True
        except Exception:
            assigned = False

    if not assigned:
        try:
            new_data = bytearray(bytes(data))
            new_data[offset:offset + len(packed)] = packed
            z.data = new_data
            assigned = True
        except Exception:
            assigned = False

    if not assigned and attr and attr != "data":
        try:
            setattr(z, attr, vals)
            assigned = True
        except Exception:
            try:
                setattr(z, attr, clean)
                assigned = True
            except Exception:
                assigned = False

    if not assigned:
        raise ZoneError("не удалось записать список каналов в зону")

    got = _zone_members(z, cp)

    if got != clean:
        raise ZoneError(
            f"запись зоны не подтвердилась: expected={clean} got={got}"
        )


def assign_channel_to_zones(
    cp,
    ch_idx: int,
    wanted_zone_ids,
    remove_from_others: bool = True,
) -> list[int]:
    """
    Старая обёртка. Лучше использовать set_channel_zones_raw().
    """
    wanted = set()

    for x in wanted_zone_ids or ():
        try:
            ix = int(x)
        except Exception:
            continue

        wanted.add(ix)

    zones = get_zones(cp)

    if not zones:
        if wanted:
            raise ZoneError(
                "Зоны не найдены в кодплаге. "
                "Проверьте md9600_debug.log: zones count=0"
            )
        return []

    existing_ids = set()

    for i, z in enumerate(zones):
        existing_ids.add(zone_id(z, i))

    unknown = wanted - existing_ids

    if unknown:
        raise ZoneError(f"Неизвестные ID зон: {sorted(unknown)}")

    changed = []

    for fallback_i, z in enumerate(zones):
        zid = zone_id(z, fallback_i)
        members = _zone_members(z, cp)
        present = ch_idx in members
        want = zid in wanted

        if want == present:
            continue

        if not want and not remove_from_others:
            continue

        attr, raw, cap, offset = _raw_zone_slots(z)

        if want:
            if cap and len(members) >= cap:
                raise ZoneError(
                    f"Зона {zid} ({zone_name(z, zid)}) полна, нет свободных слотов. "
                    f"members={len(members)} cap={cap}"
                )

            new_members = members + [ch_idx]

        else:
            new_members = [m for m in members if m != ch_idx]

        set_zone_members(z, new_members, cp)
        changed.append(zid)

    return changed


# ---------------------------------------------------------------------------
# RAW zone patch: самый надёжный способ для opengd77xfer/MD-9600
# ---------------------------------------------------------------------------

def _encode_name_patterns(name: str) -> list[bytes]:
    patterns = []

    try:
        raw = name.encode("ascii", "replace")[:16]
    except Exception:
        raw = b""

    for pad in (0x00, 0xFF):
        p = raw + bytes([pad]) * (16 - len(raw))
        if p not in patterns:
            patterns.append(p)

    # На всякий случай пробуем UTF-16LE, если вдруг имя не ASCII.
    try:
        u = name.encode("utf-16-le")[:16]

        for pad in (0x00, 0xFF):
            p = u + bytes([pad]) * (16 - len(u))
            if p not in patterns:
                patterns.append(p)
    except Exception:
        pass

    return patterns


def _read_slots_from_buf(buf, offset: int, cap: int) -> list[int]:
    slots = []

    for i in range(cap):
        p = offset + 16 + i * 2
        b = buf[p:p + 2]

        if len(b) != 2:
            slots.append(0x0000)
        else:
            slots.append(int.from_bytes(bytes(b), "little"))

    return slots


def _write_slots_to_buf(buf, offset: int, slots: list[int]) -> None:
    packed = _pack_zone_slots(slots)
    buf[offset + 16:offset + 16 + len(packed)] = packed


def _plausible_slots(slots: list[int], empty: int) -> bool:
    for v in slots:
        if v == empty:
            continue

        if not (1 <= v <= CHANNEL_MAX):
            return False

    return True


def _expected_raw_values(z, cp=None) -> set[int]:
    attr, raw, cap, offset = _raw_zone_slots(z)
    one_based = _detect_one_based(raw or [], cp)
    members = _zone_members(z, cp)

    out = set()

    for m in members:
        out.add(m + 1 if one_based else m)

    return out


def _find_zone_offset(buf, z, fallback_index: int, cp=None) -> int | None:
    """
    Ищет raw-адрес зоны в байтах кодплага по имени зоны.
    Возвращает offset начала 16-байтного имени зоны.
    """
    name = zone_name(z, zone_id(z, fallback_index))
    size = _zone_record_size(z)
    cap = _zone_capacity_from_size(size)

    if cap <= 0:
        return None

    expected = _expected_raw_values(z, cp)
    best_pos = None
    best_score = -10**9

    for pat in _encode_name_patterns(name):
        start = 0

        while True:
            pos = buf.find(pat, start)

            if pos < 0:
                break

            start = pos + 1

            if pos + size > len(buf):
                continue

            slots = _read_slots_from_buf(buf, pos, cap)
            empty = _detect_empty_marker(slots)

            if not _plausible_slots(slots, empty):
                continue

            active = {v for v in slots if v != empty}

            if active == expected:
                score = 1000
            else:
                score = (
                    len(active & expected) * 20
                    - len(active ^ expected) * 5
                    - abs(len(active) - len(expected)) * 2
                )

            # Небольшой бонус за ранний offset, чтобы не выбирать случайное совпадение.
            score -= pos / 1_000_000.0

            if score > best_score:
                best_score = score
                best_pos = pos

    if best_pos is not None and best_score > 0:
        return best_pos

    return None


def set_channel_zones_raw(cp, ch_idx: int, wanted_zone_ids) -> bytes:
    """
    Меняет зоны напрямую в raw-байтах кодплага.

    Возвращает новый bytes кодплага.
    Вызывающий код должен сделать:
        self.codeplug = Codeplug(new_bytes)
    """
    if not _is_valid_channel_index(ch_idx):
        raise ZoneError(f"некорректный индекс канала: {ch_idx}")

    wanted = set()

    for x in wanted_zone_ids or ():
        try:
            ix = int(x)
        except Exception:
            continue

        wanted.add(ix)

    zones = get_zones(cp)

    if not zones:
        if wanted:
            raise ZoneError("Зоны не найдены в кодплаге")
        return bytes(cp)

    existing_ids = set()

    for i, z in enumerate(zones):
        existing_ids.add(zone_id(z, i))

    unknown = wanted - existing_ids

    if unknown:
        raise ZoneError(f"Неизвестные ID зон: {sorted(unknown)}")

    buf = bytearray(bytes(cp))
    changed = []

    for i, z in enumerate(zones):
        zid = zone_id(z, i)
        want = zid in wanted

        members = _zone_members(z, cp)
        present_obj = ch_idx in members

        # Если по объектной модели ничего менять не нужно, всё равно проверяем raw,
        # но можно пропустить для скорости.
        if want == present_obj:
            continue

        offset = _find_zone_offset(buf, z, i, cp)

        if offset is None:
            _log(
                f"raw_zone_patch: zone={zid} name={zone_name(z, zid)!r} "
                f"offset_not_found"
            )
            raise ZoneError(
                f"не найден raw-блок зоны {zid} ({zone_name(z, zid)})"
            )

        size = _zone_record_size(z)
        cap = _zone_capacity_from_size(size)

        slots = _read_slots_from_buf(buf, offset, cap)
        empty = _detect_empty_marker(slots)
        one_based = _detect_one_based(slots, cp)

        target = ch_idx + 1 if one_based else ch_idx
        present_raw = target in slots

        if want == present_raw:
            continue

        if want:
            try:
                free = slots.index(empty)
            except ValueError:
                raise ZoneError(
                    f"Зона {zid} ({zone_name(z, zid)}) полна, нет свободных слотов"
                )

            slots[free] = target

        else:
            # Удаляем канал со сдвигом влево: прошивка читает зону до первого
            # нуля (терминатора), поэтому зануление "в середине" обрезало бы
            # все последующие каналы. Убираем ВСЕ вхождения target и плотно
            # уплотняем список.
            new_slots = []
            seen = False

            for v in slots:
                if v == target:
                    seen = True
                    continue
                if seen and v == empty:
                    # конец содержательной части - дальше все empty
                    break
                new_slots.append(v)

            new_slots.extend([empty] * (len(slots) - len(new_slots)))
            slots = new_slots

        _write_slots_to_buf(buf, offset, slots)
        changed.append(zid)

        _log(
            f"raw_zone_patch zone={zid} ch={ch_idx} target={target} "
            f"want={want} offset=0x{offset:X} empty={hex(empty)} "
            f"one_based={one_based}"
        )

    if changed:
        _log(f"raw_zone_patch done ch={ch_idx} changed_zones={changed}")

    return bytes(buf)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

def build_channel_zone_map(cp) -> dict[int, list[tuple[int, str]]]:
    """
    Возвращает map: channel_index -> [(zone_id, zone_name), ...]
    """
    mp: dict[int, list[tuple[int, str]]] = {}

    for i, z in enumerate(get_zones(cp)):
        zid = zone_id(z, i)
        name = zone_name(z, zid)

        for ch in _zone_members(z, cp):
            mp.setdefault(ch, []).append((zid, name))

    return mp


def format_channel_zones(
    zone_map: dict[int, list[tuple[int, str]]],
    idx: int,
    max_items: int = 3,
    name_len: int = 10,
) -> str:
    items = zone_map.get(idx, [])

    if not items:
        return ""

    shown = items[:max_items]
    parts = []

    for zid, name in shown:
        short = name

        if len(short) > name_len:
            short = short[:name_len] + "…"

        parts.append(f"{zid}:{short}")

    s = ", ".join(parts)

    if len(items) > max_items:
        s += f" +{len(items) - max_items}"

    return s


def zone_summary(cp, limit: int | None = None) -> list[str]:
    out = []

    for i, z in enumerate(get_zones(cp)):
        zid = zone_id(z, i)
        name = zone_name(z, zid)
        members = _zone_members(z, cp)
        attr, raw, cap, offset = _raw_zone_slots(z)

        cap_s = str(cap) if cap else "?"
        out.append(f"{zid}: {name} [{len(members)}/{cap_s}]")

    if limit is not None:
        out = out[:limit]

    return out


def log_zone_diagnostics(cp) -> None:
    global _ZONE_DIAG_LOGGED

    if _ZONE_DIAG_LOGGED:
        return

    _ZONE_DIAG_LOGGED = True

    try:
        zones = get_zones(cp)
        _log(f"zones count={len(zones)}")

        if not zones:
            try:
                attrs = [n for n in dir(cp) if "zone" in n.lower()]
                _log(f"cp zone attrs={attrs}")

                for n in attrs[:30]:
                    try:
                        v = getattr(cp, n)
                        _log(f"cp.{n} type={type(v)}")
                    except Exception as e:
                        _log(f"cp.{n} error={e!r}")
            except Exception as e:
                _log(f"cp zone attrs error={e!r}")

            return

        for i, z in enumerate(zones[:8]):
            zid = zone_id(z, i)
            name = zone_name(z, zid)
            attr, raw, cap, offset = _raw_zone_slots(z)
            empty_marker = _detect_empty_marker(raw or [])
            one_based = _detect_one_based(raw or [], cp)
            members = _zone_members(z, cp)

            _log(
                f"zone[{i}] id={zid} name={name!r} "
                f"type={type(z)} attr={attr} cap={cap} offset={offset} "
                f"empty_marker={hex(empty_marker)} one_based={one_based}"
            )

            try:
                public_attrs = [n for n in dir(z) if not n.startswith("_")]
                _log(f"zone[{i}] attrs={public_attrs[:80]}")
            except Exception as e:
                _log(f"zone[{i}] attrs error={e!r}")

            if raw is not None:
                _log(f"zone[{i}] raw_first={raw[:32]}")

            _log(f"zone[{i}] members={members[:80]}")

            try:
                d = bytes(z.data)
                _log(f"zone[{i}] data_len={len(d)} data_hex_head={d[:96].hex()}")
            except Exception as e:
                _log(f"zone[{i}] data error={e!r}")

    except Exception as e:
        _log(f"zone diagnostics error={e!r}")


# ---------------------------------------------------------------------------
# Raw-операции над зонами: просмотр состава, переименование, создание, удаление
# ---------------------------------------------------------------------------

def zone_capacity(z) -> int:
    """Сколько каналов помещается в зону (обычно 80)."""
    return _zone_capacity_from_size(_zone_record_size(z))


def zone_members_map(cp) -> dict[int, list[int]]:
    """Возвращает map: zone_id -> [0-based индексы каналов]."""
    out: dict[int, list[int]] = {}

    for i, z in enumerate(get_zones(cp)):
        zid = zone_id(z, i)
        out[zid] = list(_zone_members(z, cp))

    return out


def _zone_slot_offset(slot: int) -> int:
    return ZONE_RECORD_OFFSET + slot * ZONE_RECORD_SIZE


def _zone_bit(buf, slot: int) -> bool:
    if not (0 <= slot < ZONE_MAX_SLOTS):
        return False

    byte_i = slot // 8
    mask = 1 << (slot % 8)
    return bool(buf[ZONE_BITMAP_OFFSET + byte_i] & mask)


def _set_zone_bit(buf, slot: int, enabled: bool) -> None:
    if not (0 <= slot < ZONE_MAX_SLOTS):
        return

    byte_i = slot // 8
    mask = 1 << (slot % 8)
    pos = ZONE_BITMAP_OFFSET + byte_i

    if enabled:
        buf[pos] |= mask
    else:
        buf[pos] &= ~mask & 0xFF


def _zone_enabled_slots(buf) -> list[int]:
    return [s for s in range(ZONE_MAX_SLOTS) if _zone_bit(buf, s)]


def _read_zone_record(buf, slot: int) -> bytes:
    off = _zone_slot_offset(slot)
    return bytes(buf[off:off + ZONE_RECORD_SIZE])


def _write_zone_record(buf, slot: int, record: bytes) -> None:
    off = _zone_slot_offset(slot)
    data = (record + b"\x00" * ZONE_RECORD_SIZE)[:ZONE_RECORD_SIZE]
    buf[off:off + ZONE_RECORD_SIZE] = data


def _decode_zone_name(raw: bytes) -> str:
    return (
        raw.split(b"\x00", 1)[0]
        .decode("ascii", "replace")
        .strip("\x00\xff ")
        .strip()
    )


def _encode_zone_name(name: str) -> bytes:
    return name.encode("ascii")[:16].ljust(16, b"\x00")


def _compact_zone_records(buf) -> int:
    """
    Уплотняет включённые зоны к началу блока и возвращает их количество.
    Биты зон за пределами ZONE_MAX_SLOTS не трогаются.
    """
    records = [_read_zone_record(buf, s) for s in _zone_enabled_slots(buf)]

    for slot in range(ZONE_MAX_SLOTS):
        if slot < len(records):
            _write_zone_record(buf, slot, records[slot])
        else:
            _write_zone_record(buf, slot, b"\x00" * ZONE_RECORD_SIZE)

        _set_zone_bit(buf, slot, slot < len(records))

    return len(records)


def validate_zone_name(name: str, cp=None, exclude_zid: int | None = None) -> str:
    name = (name or "").strip()

    if not name:
        raise ZoneError("Имя зоны не может быть пустым")

    if len(name) > 16:
        raise ZoneError("Имя зоны: максимум 16 символов")

    if not name.isascii() or not name.isprintable():
        raise ZoneError("Имя зоны: только ASCII-символы")

    if cp is not None:
        wanted = name.casefold()

        for i, z in enumerate(get_zones(cp)):
            zid = zone_id(z, i)

            if exclude_zid is not None and zid == exclude_zid:
                continue

            if zone_name(z, zid).casefold() == wanted:
                raise ZoneError(f"Зона с именем «{name}» уже существует")

    return name


def _require_zone_slot(buf, zid: int) -> int:
    if not isinstance(zid, int) or not (0 <= zid < ZONE_MAX_SLOTS):
        raise ZoneError(f"некорректный ID зоны: {zid}")

    if not _zone_bit(buf, zid):
        raise ZoneError(f"зона {zid} не включена в кодплаге")

    return zid


def rename_zone_raw(cp, zid: int, new_name: str) -> bytes:
    """Переименовывает зону по raw-адресу. Возвращает новые bytes кодплага."""
    buf = bytearray(bytes(cp))
    slot = _require_zone_slot(buf, zid)
    name = validate_zone_name(new_name, cp, exclude_zid=zid)

    record = bytearray(_read_zone_record(buf, slot))
    record[:16] = _encode_zone_name(name)
    _write_zone_record(buf, slot, record)

    if _decode_zone_name(_read_zone_record(buf, slot)) != name:
        raise ZoneError("не удалось подтвердить переименование зоны")

    _log(f"zone_rename slot={slot} name={name!r}")
    return bytes(buf)


def create_zone_raw(cp, name: str, members=()) -> bytes:
    """
    Создаёт зону в первом свободном слоте (до 68 зон).
    members — 0-based индексы каналов.
    """
    buf = bytearray(bytes(cp))
    name = validate_zone_name(name, cp)
    count = _compact_zone_records(buf)

    if count >= ZONE_MAX_SLOTS:
        raise ZoneError(
            f"Нет свободных слотов зон: библиотека видит максимум "
            f"{ZONE_MAX_SLOTS} зон"
        )

    clean = []
    seen = set()

    for m in members or ():
        try:
            idx = int(m)
        except Exception:
            continue

        if _is_valid_channel_index(idx) and idx not in seen:
            seen.add(idx)
            clean.append(idx)

    if len(clean) > ZONE_CHANNEL_CAP:
        raise ZoneError(
            f"В зону можно добавить не более {ZONE_CHANNEL_CAP} каналов"
        )

    record = bytearray(ZONE_RECORD_SIZE)
    record[:16] = _encode_zone_name(name)

    for i, idx in enumerate(clean):
        record[16 + i * 2:18 + i * 2] = (idx + 1).to_bytes(2, "little")

    _write_zone_record(buf, count, record)
    _set_zone_bit(buf, count, True)

    if _decode_zone_name(_read_zone_record(buf, count)) != name:
        raise ZoneError("не удалось подтвердить создание зоны")

    _log(f"zone_create slot={count} name={name!r} members={clean}")
    return bytes(buf)


def delete_zone_raw(cp, zid: int) -> bytes:
    """
    Удаляет зону и уплотняет оставшиеся, чтобы библиотека продолжала
    видеть все зоны подряд. ID последующих зон сдвигаются.
    """
    buf = bytearray(bytes(cp))
    slot = _require_zone_slot(buf, zid)

    records = [_read_zone_record(buf, s) for s in _zone_enabled_slots(buf)]
    slots = _zone_enabled_slots(buf)

    try:
        remove_at = slots.index(slot)
    except ValueError as e:
        raise ZoneError(f"зона {zid} не найдена") from e

    del records[remove_at]

    for i in range(ZONE_MAX_SLOTS):
        if i < len(records):
            _write_zone_record(buf, i, records[i])
        else:
            _write_zone_record(buf, i, b"\x00" * ZONE_RECORD_SIZE)

        _set_zone_bit(buf, i, i < len(records))

    _log(f"zone_delete slot={slot} remaining={len(records)}")
    return bytes(buf)
