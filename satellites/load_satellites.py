#!/usr/bin/env python3
"""Загрузка спутников в рацию OpenGD77 (MD-9600).

Читает satellites.json (лежит рядом со скриптом), скачивает актуальные TLE
с Celestrak по norad_id и записывает блок SATELLITE_TLE (type=3) в
custom-data регион SPI-flash рации (@0x20000).

Формат satellites.json:
[
  {
    "name": "ISS",
    "norad_id": 25544,
    "rx_freq_mhz": 437.800,
    "tx_freq_mhz": 145.990,
    "ctcss_tx_hz": 67.0,
    "arm_ctcss_hz": null,
    "secondary_rx_freq_mhz": 145.825,
    "secondary_tx_freq_mhz": 145.825,
    "tertiary_rx_freq_mhz": 145.800,
    "tertiary_tx_freq_mhz": null
  },
  ...
]

name — до 8 ASCII символов (позывной спутника в меню рации).
Частоты в МГц, тоны CTCSS в Гц (null = нет).
rx/tx — голосовой трансивер; secondary — APRS; tertiary — доп. пара.

Использование:
  python load_satellites.py                  # satellites.json рядом
  python load_satellites.py my.json          # другой файл
  python load_satellites.py --check          # показать спутники в рации
  python load_satellites.py --port /dev/ttyACM0
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))

import serial

from debug_log import _log

# --- формат (по FORMAT.md из chirp-opengd77-aes / satellite.c прошивки) ---

AREA_FLASH = 1
CUSTOM_DATA_ADDR = 0x20000
CUSTOM_HDR_LEN = 12
CUSTOM_MAGIC = b"OpenGD77"
CUSTOM_TYPE_SATELLITE = 3

SAT_COUNT = 25
SAT_REC_LEN = 100
SAT_BLOCK_LEN = 2520
SAT_NAME_LEN = 8

SECTOR_SIZE = 4096

# LUT декомпрессии TLE из satellite.c прошивки (decompressTleData)
SAT_COMPRESS_LUT = "0123456789. +-*"


def _sat_compress_line(s: str, nbytes: int) -> bytes:
    """Nibble-pack строку из символов LUT: 2 символа на байт, паддинг '0'."""
    s = (s + "0" * (2 * nbytes))[:2 * nbytes]
    out = bytearray(nbytes)
    for i in range(nbytes):
        out[i] = (SAT_COMPRESS_LUT.index(s[2 * i]) << 4) | \
            SAT_COMPRESS_LUT.index(s[2 * i + 1])
    return bytes(out)


def _tle_line1_fields(l1: str) -> str:
    """24 символа: year(2)+epochDay(12)+firstDeriv(10) из колонок TLE line 1."""
    return l1[18:20] + l1[20:32] + l1[33:43]


def _tle_line2_fields(l2: str) -> str:
    """55 символов: incl(8)+RAAN(8)+ecc(7)+argP(8)+MA(8)+MM(11)+rev(5)."""
    return (l2[8:16] + l2[17:25] + l2[26:33] + l2[34:42] +
            l2[43:51] + l2[52:63] + l2[63:68])


def _css_to_bcd(tone_hz) -> int:
    """CTCSS Гц -> BCD тон*10 (67.0 -> 0x0670); 0/None = нет."""
    if not tone_hz:
        return 0
    v = int(round(float(tone_hz) * 10))
    return int(f"{v:04x}", 16)


def encode_satellite(sat: dict) -> bytes:
    """Кодирует одну 100-байтную запись спутника."""
    rec = bytearray(SAT_REC_LEN)

    nm = sat["name"].encode("ascii", "ignore")[:SAT_NAME_LEN]
    rec[0:len(nm)] = nm

    rec[8:20] = _sat_compress_line(_tle_line1_fields(sat["tle1"]), 12)
    rec[20:48] = _sat_compress_line(_tle_line2_fields(sat["tle2"]), 28)

    def f32(hz):
        # прошивка трактует поля как герцы (uint32);
        # деление на 10 давало сдвиг запятой (437.798 -> 43.779)
        return int(round(hz or 0))

    struct.pack_into("<II", rec, 48,
                     f32(sat.get("rx1")), f32(sat.get("tx1")))
    struct.pack_into("<HH", rec, 56,
                     _css_to_bcd(sat.get("ctcss1")),
                     _css_to_bcd(sat.get("arm1")))
    struct.pack_into("<II", rec, 60,
                     f32(sat.get("rx2")), f32(sat.get("tx2")))
    struct.pack_into("<II", rec, 68,
                     f32(sat.get("rx3")), f32(sat.get("tx3")))
    # rec[76:100] — spare, нули

    return bytes(rec)


def build_satellite_block(sats: list[dict]) -> bytes:
    """2520-байтный блок из до 25 спутников; пустые слоты нули."""
    if len(sats) > SAT_COUNT:
        raise ValueError(
            f"слишком много спутников: {len(sats)} > {SAT_COUNT}")

    block = bytearray(SAT_BLOCK_LEN)
    for i, sat in enumerate(sats):
        block[i * SAT_REC_LEN:(i + 1) * SAT_REC_LEN] = encode_satellite(sat)
    return bytes(block)


# --- USB CPS протокол ---

class RadioSession:
    def __init__(self, port: str):
        self.pipe = serial.Serial(
            port, 115200, timeout=2.0, write_timeout=2.0,
            dsrdtr=False, rtscts=False)

    def __enter__(self):
        self.pipe.reset_input_buffer()
        self.pipe.write(bytes([ord("C"), 0]))
        self.pipe.flush()
        time.sleep(0.1)
        self.pipe.read(64)
        return self

    def __exit__(self, *exc):
        try:
            self.pipe.write(bytes([ord("C"), 5]))
            self.pipe.flush()
            time.sleep(0.05)
            self.pipe.read(64)
        finally:
            self.pipe.close()
        return False

    def _read_area(self, addr: int, length: int) -> bytes:
        out = b""
        rem = length
        while rem > 0:
            n = min(rem, 1024)
            req = bytes([ord("R"), AREA_FLASH,
                         (addr >> 24) & 0xFF, (addr >> 16) & 0xFF,
                         (addr >> 8) & 0xFF, addr & 0xFF,
                         (n >> 8) & 0xFF, n & 0xFF])
            self.pipe.reset_input_buffer()
            self.pipe.write(req)
            self.pipe.flush()
            time.sleep(0.05)
            r = self.pipe.read(n + 3)
            if len(r) < 3 or r[0] != ord("R"):
                raise RuntimeError(
                    f"чтение не удалось @0x{addr:X} (got {r[:8]!r})")
            out += r[3:3 + n]
            addr += n
            rem -= n
        return out

    def _flash_prepare(self, sector: int, retries: int = 3) -> None:
        for attempt in range(retries):
            req = bytes([ord("X"), 1,
                         (sector >> 16) & 0xFF,
                         (sector >> 8) & 0xFF,
                         sector & 0xFF])
            self.pipe.reset_input_buffer()
            self.pipe.write(req)
            self.pipe.flush()
            time.sleep(0.2)
            r = self.pipe.read(8)
            if r and r[0] != ord("-"):
                return
            _log(f"sat_prepare sector=0x{sector:X} attempt={attempt+1} bad={r!r}")
            time.sleep(0.3)
        raise RuntimeError(f"prepare sector 0x{sector:X} не удался")

    def _flash_send(self, addr: int, data: bytes, retries: int = 3) -> None:
        off = 0
        while off < len(data):
            chunk = data[off:off + 1024]
            a = addr + off
            req = bytes([ord("X"), 2,
                         (a >> 24) & 0xFF, (a >> 16) & 0xFF,
                         (a >> 8) & 0xFF, a & 0xFF,
                         (len(chunk) >> 8) & 0xFF,
                         len(chunk) & 0xFF]) + chunk
            ok = False
            for attempt in range(retries):
                self.pipe.reset_input_buffer()
                self.pipe.write(req)
                self.pipe.flush()
                time.sleep(0.05)
                r = self.pipe.read(8)
                if r and r[0] != ord("-"):
                    ok = True
                    break
                _log(f"sat_send addr=0x{a:X} attempt={attempt+1} bad={r!r}")
                time.sleep(0.2)
            if not ok:
                raise RuntimeError(f"send @0x{a:X} не удался")
            off += len(chunk)

    def _flash_commit(self, retries: int = 3) -> None:
        for attempt in range(retries):
            self.pipe.reset_input_buffer()
            self.pipe.write(bytes([ord("X"), 3]))
            self.pipe.flush()
            time.sleep(0.7)
            r = self.pipe.read(8)
            if r and r[0] != ord("-"):
                return
            _log(f"sat_commit attempt={attempt+1} bad={r!r}")
        raise RuntimeError("commit не удался")

    def write_region(self, raw_addr: int, new_data: bytes,
                     old_data: bytes | None = None) -> int:
        """Побайтово пишет регион, по 4К секторам: только изменившиеся.
        Прошивка сама читает сектор, патчит, стирает и пишет обратно."""
        end = raw_addr + len(new_data)
        first = raw_addr // SECTOR_SIZE
        last = (end - 1) // SECTOR_SIZE
        written = 0

        for sector in range(first, last + 1):
            s0 = sector * SECTOR_SIZE
            s1 = s0 + SECTOR_SIZE
            seg0 = max(raw_addr, s0)
            seg1 = min(end, s1)
            i0 = seg0 - raw_addr
            i1 = seg1 - raw_addr
            seg = bytes(new_data[i0:i1])

            if old_data is not None and \
                    bytes(old_data[i0:i1]) == seg:
                continue

            self._flash_prepare(sector)
            self._flash_send(seg0, seg)
            self._flash_commit()

            rb = self._read_area(seg0, len(seg))
            if rb != seg:
                raise RuntimeError(f"verify failed @0x{seg0:X}")

            written += 1
            time.sleep(0.3)

        return written


# --- работа с custom-data цепочкой ---

def find_satellite_block(sess: RadioSession):
    """Ищет блок SATELLITE_TLE в цепочке custom-data.
    Возвращает (hdr_addr, dlen) или None если блока нет."""
    if sess._read_area(CUSTOM_DATA_ADDR, 8) != CUSTOM_MAGIC:
        raise RuntimeError(
            "custom-data регион без magic 'OpenGD77' — "
            "скачайте codeplug из рации сначала")

    off = CUSTOM_HDR_LEN
    while off + 8 <= 0x10000:
        hdr = sess._read_area(CUSTOM_DATA_ADDR + off, 8)
        dtype, dlen = struct.unpack("<II", hdr)
        if dlen in (0, 0xFFFFFFFF):
            return None
        if dtype == CUSTOM_TYPE_SATELLITE:
            return CUSTOM_DATA_ADDR + off, dlen
        off += 8 + dlen
    raise RuntimeError("custom-data регион переполнен")


def write_satellites(sess: RadioSession, block: bytes) -> None:
    """Перезаписывает блок SATELLITE_TLE по месту (same-size in-place)."""
    state = find_satellite_block(sess)

    if state is None:
        raise RuntimeError(
            "блок SATELLITE_TLE не найден в рации. "
            "Создайте его один раз через CPS/CHIRP (dondch/chirp-opengd77-aes)")
    hdr_addr, dlen = state

    if dlen != SAT_BLOCK_LEN:
        raise RuntimeError(
            f"существующий блок {dlen} байт (ожидается {SAT_BLOCK_LEN})")

    payload_addr = hdr_addr + 8
    old = sess._read_area(payload_addr, SAT_BLOCK_LEN)

    if old == block:
        print("TLE уже идентичны — запись не требуется")
        return

    written = sess.write_region(payload_addr, block, old)
    _log(f"sat_write sectors={written}")
    print(f"записано секторов: {written}")


# --- main ---

CELESTRAK_TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?CATNR={norad}&FORMAT=TLE"


def fetch_tle(norad_id: int, name: str, timeout: float = 15.0) -> tuple[str, str]:
    """Скачивает TLE с Celestrak по NORAD ID, возвращает (tle1, tle2)."""
    import urllib.request

    url = CELESTRAK_TLE_URL.format(norad=norad_id)
    req = urllib.request.Request(url, headers={"User-Agent": "md9600-sat-loader/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", "replace")

    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    l1 = next((ln for ln in lines if ln.startswith("1 ")), None)
    l2 = next((ln for ln in lines if ln.startswith("2 ")), None)

    if not l1 or not l2:
        raise ValueError(f"{name} (NORAD {norad_id}): Celestrak не вернул TLE")

    return l1, l2


def load_config(path: str) -> list[dict]:
    """Читает satellites.json: массив объектов с name, norad_id и частотами
    в МГц. TLE скачиваются с Celestrak по norad_id. Если в записи есть
    tle1/tle2 — используются они, без скачивания."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))

    if isinstance(raw, dict):
        raw = raw.get("satellites")
    if not isinstance(raw, list):
        raise ValueError("JSON должен быть массивом спутников")

    sats = []
    for s in raw:
        mhz = lambda v: int(round(float(v or 0) * 1_000_000))
        hz = lambda v: float(v or 0)
        sat = {
            "name": s["name"],
            "rx1": mhz(s.get("rx_freq_mhz")),
            "tx1": mhz(s.get("tx_freq_mhz")),
            "ctcss1": hz(s.get("ctcss_tx_hz")) or None,
            "arm1": hz(s.get("arm_ctcss_hz")) or None,
            "rx2": mhz(s.get("secondary_rx_freq_mhz")),
            "tx2": mhz(s.get("secondary_tx_freq_mhz")),
            "rx3": mhz(s.get("tertiary_rx_freq_mhz")),
            "tx3": mhz(s.get("tertiary_tx_freq_mhz")),
        }
        if s.get("tle1") and s.get("tle2"):
            sat["tle1"] = s["tle1"]
            sat["tle2"] = s["tle2"]
        elif s.get("norad_id"):
            print(f"  {s['name']}: загрузка TLE с Celestrak "
                  f"(NORAD {s['norad_id']})...")
            t1, t2 = fetch_tle(s["norad_id"], s["name"])
            sat["tle1"] = t1
            sat["tle2"] = t2
        else:
            print(f"  пропущен {s['name']}: нет norad_id")
            continue

        sats.append(sat)

    return sats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Загрузка спутников в OpenGD77 (MD-9600)")
    parser.add_argument(
        "json", nargs="?", default=None,
        help="JSON со спутниками (по умолчанию satellites.json рядом со скриптом)")
    parser.add_argument("--port", default=None, help="COM port")
    parser.add_argument(
        "--check", action="store_true",
        help="только показать текущее состояние рации")
    args = parser.parse_args()

    json_path = args.json or str(
        Path(__file__).resolve().parent / "satellites.json")

    if not args.check and not Path(json_path).exists():
        parser.error(f"файл не найден: {json_path}")

    port = args.port
    if not port:
        import glob
        ports = glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")
        if not ports:
            print("порт не найден, укажите --port")
            sys.exit(2)
        port = ports[0]

    with RadioSession(port) as sess:
        state = find_satellite_block(sess)

        if args.check:
            if state is None:
                print("блок SATELLITE_TLE отсутствует")
                return
            hdr_addr, dlen = state
            print(f"блок найден @0x{hdr_addr:X} len={dlen}")
            payload = sess._read_area(hdr_addr + 8, SAT_BLOCK_LEN)
            _print_sats(payload)
            return

        sats = load_config(json_path)
        if not sats:
            raise ValueError("нет спутников для записи")

        block = build_satellite_block(sats)
        print(f"спутников к записи: {len(sats)}")
        write_satellites(sess, block)
        print("готово")


def _print_sats(payload: bytes) -> None:
    LUT = SAT_COMPRESS_LUT

    def decompress(b, nchars):
        out = []
        for byte in b[:nchars // 2]:
            out.append(LUT[byte >> 4])
            out.append(LUT[byte & 0xF])
        return "".join(out)

    for i in range(SAT_COUNT):
        rec = payload[i * SAT_REC_LEN:(i + 1) * SAT_REC_LEN]
        if rec[0] == 0:
            continue
        name = rec[0:8].decode("ascii", "replace").rstrip("\x00 ")
        l1 = decompress(rec[8:20], 24)
        l2 = decompress(rec[20:48], 56)
        rx1, tx1 = struct.unpack("<II", rec[48:56])
        c1, a1 = struct.unpack("<HH", rec[56:60])
        print(f"[{i}] {name!r}")
        print(f"    L1: {l1}")
        print(f"    L2: {l2}")
        print(f"    rx1={rx1} tx1={tx1} "
              f"ctcss={c1} arm={a1}")


if __name__ == "__main__":
    main()
