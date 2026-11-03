"""Спутниковый блок для WEB UI.

Источник данных — satellites/satellites.json (тот же файл, что у
CLI-загрузчика satellites/load_satellites.py). Внешние данные:
каталог TLE https://satgs.ru/satonline.txt и транспондеры
https://satgs.ru/transmitters.zip (в памяти с TTL-кэшем).
WEB UI умеет: редактировать список (add/edit/delete с сохранением в
JSON), подбирать NORAD ID по имени, показывать транспондеры для
перетаскивания в пары частот, искать актуальные TLE и записывать
2520-байтный блок в рацию через RadioSession.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import threading
import urllib.request
import zipfile
from pathlib import Path
from time import monotonic

ROOT = Path(__file__).resolve().parents[2]

for p in (ROOT / "src", ROOT / "vendor", ROOT / "satellites"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import load_satellites as ls

CONFIG_PATH = ROOT / "satellites" / "satellites.json"

SATONLINE_URL = "https://satgs.ru/satonline.txt"
TRANSMITTERS_URL = "https://satgs.ru/transmitters.zip"

CATALOG_TTL = 3600.0
TRANSMITTERS_TTL = 6 * 3600.0

MAX_NAME_LEN = 32

JSON_KEYS = (
    "name",
    "norad_id",
    "rx_freq_mhz",
    "tx_freq_mhz",
    "ctcss_tx_hz",
    "arm_ctcss_hz",
    "secondary_rx_freq_mhz",
    "secondary_tx_freq_mhz",
    "tertiary_rx_freq_mhz",
    "tertiary_tx_freq_mhz",
)

# Порядок и состав полей в файле: все поля явно, с null по умолчанию
# (конвенция 9ba64f5)
SAVE_KEYS = (
    "name",
    "norad_id",
    "tle1",
    "tle2",
    "rx_freq_mhz",
    "tx_freq_mhz",
    "ctcss_tx_hz",
    "arm_ctcss_hz",
    "secondary_rx_freq_mhz",
    "secondary_tx_freq_mhz",
    "tertiary_rx_freq_mhz",
    "tertiary_tx_freq_mhz",
)

_TONE_RE = re.compile(r"CTCSS\s*([\d]{2,3}(?:[.,]\d)?)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Файл конфигурации
# ---------------------------------------------------------------------------

def read_config(path: str | Path | None = None) -> list[dict]:
    """Сырой список спутников из JSON, без скачивания TLE."""
    p = Path(path or CONFIG_PATH)
    raw = json.loads(p.read_text(encoding="utf-8"))

    if isinstance(raw, dict):
        raw = raw.get("satellites") or []

    if not isinstance(raw, list):
        raise ValueError("JSON должен быть массивом спутников")

    return raw


def save_config(rows: list[dict], path: str | Path | None = None) -> None:
    """Атомарно сохраняет список спутников в JSON (тот же формат, что CLI)."""
    p = Path(path or CONFIG_PATH)
    tmp = p.with_suffix(".json.tmp")
    data = [{k: r.get(k) for k in SAVE_KEYS} for r in rows]
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# HTTP-кэши: каталог TLE и транспондеры satgs.ru
# ---------------------------------------------------------------------------

_http_lock = threading.Lock()
_tle_catalog: dict = {"fetched": 0.0, "data": {}}
_transmitters: dict = {"fetched": 0.0, "data": {}}


def _http_get(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(
        url, headers={"User-Agent": "md9600-webui/1.0"}
    )

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _parse_catalog(text: str) -> dict[int, tuple[str, str, str]]:
    """satonline.txt -> {norad: (name, line1, line2)}."""
    data: dict[int, tuple[str, str, str]] = {}
    pending_name = ""
    lines = text.splitlines()
    i = 0

    while i < len(lines):
        line = lines[i].rstrip()

        if line.startswith("1 "):
            try:
                norad = int(line[2:7])
            except ValueError:
                i += 1
                continue

            l2 = lines[i + 1].rstrip() if i + 1 < len(lines) else ""

            if l2.startswith("2 "):
                data[norad] = (pending_name or f"NORAD {norad}", line, l2)
                pending_name = ""
                i += 2
                continue

        elif line and not line.startswith("2 "):
            pending_name = line

        i += 1

    return data


def load_tle_catalog(force: bool = False) -> dict[int, tuple[str, str, str]]:
    """Каталог TLE с satgs.ru/satonline.txt, в памяти с TTL."""
    with _http_lock:
        fresh = (
            not force
            and _tle_catalog["data"]
            and monotonic() - _tle_catalog["fetched"] < CATALOG_TTL
        )

        if fresh:
            return _tle_catalog["data"]

        text = _http_get(SATONLINE_URL).decode("utf-8", "replace")
        _tle_catalog["data"] = _parse_catalog(text)
        _tle_catalog["fetched"] = monotonic()

        return _tle_catalog["data"]


def fetch_tle(norad_id: int, name: str = "",
              force: bool = False) -> tuple[str, str]:
    """TLE по NORAD ID из каталога satgs.ru. Бросает ValueError если нет."""
    data = load_tle_catalog(force=force)
    entry = data.get(int(norad_id))

    if entry is None:
        raise ValueError(
            f"NORAD {norad_id} ({name}) не найден в каталоге satgs.ru"
        )

    return entry[1], entry[2]


def _hz_to_mhz(v) -> float | None:
    if not v:
        return None

    return round(int(v) / 1_000_000, 6)


def _parse_transmitters(raw: bytes) -> dict[int, list[dict]]:
    """transmitters.zip -> {norad: [rows]} с приведением к МГц."""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        data = json.loads(zf.read("transmitters.json").decode("utf-8"))

    grouped: dict[int, list[dict]] = {}

    for t in data:
        nid = t.get("norad_cat_id")

        if not nid:
            continue

        desc = (t.get("description") or "").strip()
        tone = None
        m = _TONE_RE.search(desc)

        if m:
            try:
                tone = float(m.group(1).replace(",", "."))
            except ValueError:
                tone = None

        row = {
            "rx": _hz_to_mhz(t.get("downlink_low")),
            "tx": _hz_to_mhz(t.get("uplink_low")),
            "mode": (t.get("mode") or "").upper(),
            "umode": (t.get("uplink_mode") or "").upper(),
            "desc": desc[:60],
            "tone": tone,
            "invert": bool(t.get("invert")),
            "active": t.get("status") == "active" and t.get("alive", True),
        }

        grouped.setdefault(int(nid), []).append(row)

    for rows in grouped.values():
        rows.sort(key=lambda r: (not r["active"], r["rx"] or 0))

    return grouped


def load_transmitters(force: bool = False) -> dict[int, list[dict]]:
    """Транспондеры с satgs.ru/transmitters.zip, в памяти с TTL."""
    with _http_lock:
        fresh = (
            not force
            and _transmitters["data"]
            and monotonic() - _transmitters["fetched"] < TRANSMITTERS_TTL
        )

        if fresh:
            return _transmitters["data"]

        raw = _http_get(TRANSMITTERS_URL, timeout=60.0)
        _transmitters["data"] = _parse_transmitters(raw)
        _transmitters["fetched"] = monotonic()

        return _transmitters["data"]


def find_transmitters(norad_id: int) -> list[dict]:
    return load_transmitters().get(int(norad_id), [])


# ---------------------------------------------------------------------------
# TLE
# ---------------------------------------------------------------------------

def tle_for(sat: dict, cache: dict) -> tuple[str, str] | None:
    """TLE из самого конфига (tle1/tle2) либо из кэша после поиска."""
    if sat.get("tle1") and sat.get("tle2"):
        return sat["tle1"], sat["tle2"]

    return cache.get(sat["name"])


def epoch_of(tle1: str) -> str:
    """Поле эпохи из строки 1 TLE (YYDDD.DDDDDDDD)."""
    try:
        return tle1[18:32].strip()
    except Exception:
        return ""


def fetch_all(cache: dict, progress=None) -> tuple[int, int]:
    """Обновляет TLE всех norad_id из конфига по каталогу satgs.ru.

    Каталог скачивается одним запросом (force), пишем в cache по имени
    спутника, возвращаем (успех, всего).
    """
    sats = read_config()
    total = len(sats)
    ok = 0

    if progress:
        progress(0, total)

    force = True

    for i, sat in enumerate(sats):
        if progress:
            progress(i, total)

        nid = sat.get("norad_id")

        if not nid:
            continue

        try:
            l1, l2 = fetch_tle(nid, sat["name"], force=force)
            cache[sat["name"]] = (l1, l2)
            ok += 1
        except Exception:
            # старая запись в cache остаётся, если была
            continue

        force = False

    if progress:
        progress(total, total)

    return ok, total


# ---------------------------------------------------------------------------
# Поиск NORAD по имени (каталог satgs.ru)
# ---------------------------------------------------------------------------

def radio_name(full: str) -> str:
    """Имя для 8-байтного поля записи рации: полное обрезается аккуратно."""
    n = (full or "").strip()

    if len(n) <= ls.SAT_NAME_LEN:
        return n

    cut = n[:ls.SAT_NAME_LEN].rstrip()

    while cut and cut[-1] in " (-[":
        cut = cut[:-1]

    return cut


def find_norad(query: str, limit: int = 20) -> list[dict]:
    """Ищет объекты по подстроке имени в каталоге TLE."""
    q = (query or "").strip().casefold()

    if not q:
        return []

    data = load_tle_catalog()
    out = []

    for norad, (name, _l1, _l2) in data.items():
        if q in name.casefold():
            out.append({"norad_id": norad, "name": name})

            if len(out) >= limit:
                break

    return out


# ---------------------------------------------------------------------------
# Сборка и запись блока
# ---------------------------------------------------------------------------

def build_block(cache: dict, progress=None) -> bytes:
    """2520-байтный блок из конфига; недостающие TLE догружает с Celestrak.

    Прогресс — (счёт спутников, всего).
    """
    sats = read_config()
    total = len(sats)
    records = []

    mhz = lambda v: round(float(v or 0) * 1_000_000)
    hz = lambda v: float(v or 0) or None

    for i, sat in enumerate(sats):
        if progress:
            progress(i, total)

        tle = tle_for(sat, cache)

        if tle is None:
            nid = sat.get("norad_id")

            if not nid:
                continue

            tle = fetch_tle(nid, sat["name"])
            cache[sat["name"]] = tle

        records.append({
            "name": radio_name(sat["name"]),
            "tle1": tle[0],
            "tle2": tle[1],
            "rx1": mhz(sat.get("rx_freq_mhz")),
            "tx1": mhz(sat.get("tx_freq_mhz")),
            "ctcss1": hz(sat.get("ctcss_tx_hz")),
            "arm1": hz(sat.get("arm_ctcss_hz")),
            "rx2": mhz(sat.get("secondary_rx_freq_mhz")),
            "tx2": mhz(sat.get("secondary_tx_freq_mhz")),
            "rx3": mhz(sat.get("tertiary_rx_freq_mhz")),
            "tx3": mhz(sat.get("tertiary_tx_freq_mhz")),
        })

    if progress:
        progress(total, total)

    if not records:
        raise ValueError(
            "нет спутников с TLE: нажмите «Найти TLE» или заполните norad_id"
        )

    return ls.build_satellite_block(records)


def write_to_radio(port: str, block: bytes, progress=None) -> str:
    """Пишет блок SATELLITE_TLE в рацию через RadioSession CLI-скрипта.

    Логика та же, что ls.write_satellites, но с пошаговым прогрессом
    (шаг из 5): 1 — блок собран, 2 — блок найден в custom-data,
    3 — старый блок прочитан, 4 — секторы записаны, 5 — готово.
    """
    def step(n: int) -> None:
        if progress:
            progress(n, 5)

    step(1)

    with ls.RadioSession(port) as sess:
        state = ls.find_satellite_block(sess)

        if state is None:
            raise RuntimeError(
                "блок SATELLITE_TLE не найден в рации. Создайте его "
                "один раз через CPS/CHIRP (dondch/chirp-opengd77-aes)"
            )

        hdr_addr, dlen = state

        if dlen != ls.SAT_BLOCK_LEN:
            raise RuntimeError(
                f"существующий блок {dlen} байт "
                f"(ожидается {ls.SAT_BLOCK_LEN})"
            )

        step(2)

        payload_addr = hdr_addr + 8
        old = sess._read_area(payload_addr, ls.SAT_BLOCK_LEN)
        step(3)

        if old == block:
            step(5)
            return "same"

        sess.write_region(payload_addr, block, old)
        step(4)

    step(5)
    return "written"


# ---------------------------------------------------------------------------
# Форма редактирования
# ---------------------------------------------------------------------------

def validate_sat_form(data: dict, existing: list[dict],
                      slot: int | None) -> tuple[dict, dict]:
    """Валидация полей формы спутника. Возвращает (row, errors)."""
    errors: dict[str, str] = {}
    row: dict = {}

    name = (data.get("name") or "").strip()

    if not name:
        errors["name"] = "Имя обязательно"
    elif len(name) > MAX_NAME_LEN:
        errors["name"] = f"Имя: максимум {MAX_NAME_LEN} символов"
    elif not name.isascii() or not name.isprintable():
        errors["name"] = "Имя: только ASCII-символы"
    else:
        row["name"] = name

    nid = (data.get("norad_id") or "").strip()

    if not nid:
        errors["norad_id"] = "NORAD ID обязателен для поиска TLE"
    else:
        try:
            value = int(nid)

            if not 1 <= value <= 99999:
                raise ValueError

            row["norad_id"] = value
        except ValueError:
            errors["norad_id"] = "NORAD ID: целое 1–99999"

    def freq(key: str, label: str):
        s = (data.get(key) or "").strip().replace(",", ".")

        if not s or s == "—":
            row[key] = None
            return

        try:
            v = float(s)
        except ValueError:
            errors[key] = f"{label}: не число"
            return

        if not 0 < v <= 1000:
            errors[key] = f"{label}: диапазон 0–1000 МГц"
            return

        row[key] = v

    def tone(key: str, label: str):
        s = (data.get(key) or "").strip().replace(",", ".")

        if not s or s == "—":
            row[key] = None
            return

        try:
            v = float(s)
        except ValueError:
            errors[key] = f"{label}: не число"
            return

        if not 60 <= v <= 260:
            errors[key] = f"{label}: тон 60–260 Гц"
            return

        row[key] = v

    freq("rx_freq_mhz", "RX")
    freq("tx_freq_mhz", "TX")
    tone("ctcss_tx_hz", "CTCSS")
    tone("arm_ctcss_hz", "ARM")
    freq("secondary_rx_freq_mhz", "RX2")
    freq("secondary_tx_freq_mhz", "TX2")
    freq("tertiary_rx_freq_mhz", "RX3")
    freq("tertiary_tx_freq_mhz", "TX3")

    if not row.get("rx_freq_mhz") and not row.get("tx_freq_mhz"):
        errors.setdefault("rx_freq_mhz", "Заполните хотя бы пару RX/TX")

    for i, s in enumerate(existing):
        if i != slot and s.get("name") == row.get("name"):
            errors["name"] = f"Спутник «{row['name']}» уже есть в списке"
            break

    if len(existing) >= ls.SAT_COUNT and slot is None:
        errors.setdefault("name", f"Максимум {ls.SAT_COUNT} спутников")

    return row, errors
