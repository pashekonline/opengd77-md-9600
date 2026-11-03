import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "vendor"))

from opengd77.codeplug import Codeplug

import gd77_flash
import gd77_patch  # noqa: F401 применяет патчи BlockView/ZonesView
from gd77_flash import (
    RadioCancelled,
    _read_chunk_safe,
    _read_flash_safe,
    friendly_serial_error,
    read_codeplug_safe,
)


class FakePipe:
    """Минимальный serial-порт, отвечающий на R-запросы из памяти."""

    def __init__(self, memory, *, zero_at=None, max_chunk=None):
        self.memory = dict(memory)
        self.zero_at = zero_at
        self.max_chunk = max_chunk
        self.is_open = False
        self.timeout = 1.0
        self.write_timeout = 1.0
        self.baudrate = 9600
        self.port = "FAKE"
        self._in = bytearray()
        self.requests = []

    def open(self):
        self.is_open = True

    def close(self):
        self.is_open = False

    def reset_input_buffer(self):
        self._in.clear()

    def reset_output_buffer(self):
        pass

    @property
    def in_waiting(self):
        return len(self._in)

    def read_all(self):
        data = bytes(self._in)
        self._in.clear()
        return data

    def flush(self):
        pass

    def read(self, size):
        if not self._in:
            return b""

        data = bytes(self._in[:size])
        del self._in[:size]
        return data

    def write(self, data):
        req = bytes(data)

        if req[0] == ord("C"):
            self._in.extend(b"-")
            return

        if req[0] != ord("R"):
            return

        _, mode, addr, length = struct.unpack(">BBIH", req)
        self.requests.append((mode, addr, length))

        if self.zero_at is not None and addr == self.zero_at:
            self._in.extend(struct.pack(">BH", ord("R"), 0))
            return

        payload = b"\xff" * length

        for base, segment in self.memory.items():
            if base <= addr < base + len(segment):
                start = addr - base
                payload = segment[start:start + length]
                break

        if self.max_chunk is not None:
            payload = payload[: self.max_chunk]

        self._in.extend(struct.pack(">BH", ord("R"), len(payload)))
        self._in.extend(payload)


class FakeRadio:
    def __init__(self, pipe):
        self.port = pipe
        self.calls = []

    def _show_screen(self, lines):
        self.calls.append("show_screen")

    def _flash_green(self):
        self.calls.append("flash_green")

    def _save(self):
        self.calls.append("save")

    def _C_scr_close(self):
        self.calls.append("screen_close")


def _part(file_addr, radio_addr, size, memtype=1):
    return Codeplug.CPPart(memtype, file_addr, radio_addr, size)


def test_read_codeplug_safe_reads_parts_and_progress():
    payload = bytes(range(64))
    pipe = FakePipe({0x1000: payload})
    radio = FakeRadio(pipe)
    seen = []

    cp = read_codeplug_safe(
        radio,
        parts=[_part(0x0000, 0x1000, len(payload))],
        progress=lambda done, total: seen.append((done, total)),
    )

    assert cp.data[0:64] == payload
    assert seen[-1] == (64, 64)
    assert pipe.baudrate == 115200
    assert pipe.is_open is True
    # CPS-хендшейк выполняется до чтения, экран закрывается в конце.
    assert radio.calls[0] == "show_screen"
    assert radio.calls[-1] == "screen_close"


def test_read_codeplug_safe_accepts_short_chunks():
    payload = bytes(range(64))
    pipe = FakePipe({0x2000: payload}, max_chunk=16)
    radio = FakeRadio(pipe)

    cp = read_codeplug_safe(
        radio,
        parts=[_part(0x0040, 0x2000, len(payload))],
    )

    assert cp.data[0x40:0x80] == payload


def test_read_chunk_safe_splits_large_ranges_into_32_byte_requests():
    payload = bytes(range(256)) * 3
    pipe = FakePipe({0x4000: payload})

    data = _read_chunk_safe(pipe, 1, 0x4000, len(payload))

    assert data == payload
    assert pipe.requests
    assert all(length <= 32 for _, _, length in pipe.requests)


def test_read_flash_safe_uses_chunked_reader_for_sectors():
    payload = bytes(range(256)) * 16
    pipe = FakePipe({0x7000: payload})
    radio = FakeRadio(pipe)

    data = _read_flash_safe(radio, 0x7000, len(payload))

    assert data == payload
    assert all(length <= 32 for _, _, length in pipe.requests)


def test_read_codeplug_safe_avoids_blocking_port_calls():
    class NoBlockingCallsPipe(FakePipe):
        def flush(self):
            raise AssertionError("flush() не должен вызываться при чтении")

        def reset_output_buffer(self):
            raise AssertionError(
                "reset_output_buffer() не должен вызываться при чтении"
            )

        def reset_input_buffer(self):
            raise AssertionError(
                "reset_input_buffer() не должен вызываться, "
                "когда доступен in_waiting"
            )

    payload = bytes(range(32))
    pipe = NoBlockingCallsPipe({0x5000: payload})
    radio = FakeRadio(pipe)

    cp = read_codeplug_safe(
        radio,
        parts=[_part(0x0000, 0x5000, len(payload))],
    )

    assert cp.data[0:32] == payload


def test_read_codeplug_safe_rejects_zero_length_response():
    pipe = FakePipe({0x3000: b"\x00" * 32}, zero_at=0x3000)
    radio = FakeRadio(pipe)

    with pytest.raises(RuntimeError):
        read_codeplug_safe(
            radio,
            parts=[_part(0x0000, 0x3000, 32)],
        )

    # По две попытки на каждой скорости (115200 и 9600), а не бесконечный
    # цикл.
    assert 1 <= len(pipe.requests) <= 4


def test_read_codeplug_safe_can_be_cancelled_between_chunks():
    payload = bytes(range(256))
    pipe = FakePipe({0x6000: payload})
    radio = FakeRadio(pipe)
    seen = []

    def cancel():
        return bool(seen)

    with pytest.raises(RadioCancelled):
        read_codeplug_safe(
            radio,
            parts=[_part(0x0000, 0x6000, len(payload))],
            progress=lambda done, total: seen.append(done),
            cancel=cancel,
        )

    assert seen
    # Отмена не должна запускать повторное чтение на 9600.
    assert pipe.baudrate == 115200
    assert radio.calls[-1] == "screen_close"


def test_read_codeplug_safe_reconfigures_port_timeout_once_per_session():
    class CountingPipe(FakePipe):
        def __init__(self, memory):
            self.timeout_sets = 0
            super().__init__(memory)

        def __setattr__(self, name, value):
            if name == "timeout":
                self.timeout_sets += 1
            super().__setattr__(name, value)

    payload = bytes(range(128))
    pipe = CountingPipe({0x8000: payload})
    radio = FakeRadio(pipe)

    cp = read_codeplug_safe(
        radio,
        parts=[_part(0x0000, 0x8000, len(payload))],
    )

    assert cp.data[0:128] == payload
    # Таймаут ставится один раз на сессию и восстанавливается в конце:
    # раньше каждое обращение к порту значило лишние IOCTL переконфигурации.
    assert pipe.timeout_sets <= 6


def test_read_codeplug_safe_closes_screen_when_cancelled_on_probe():
    pipe = FakePipe({0x6000: b"\xff" * 32})
    radio = FakeRadio(pipe)
    checks = {"n": 0}

    def cancel():
        # Разрешаем открыть порт и войти в CPS, отменяем на пробном чтении.
        checks["n"] += 1
        return checks["n"] > 2

    with pytest.raises(RadioCancelled):
        read_codeplug_safe(
            radio,
            parts=[_part(0x0000, 0x6000, 32)],
            cancel=cancel,
        )

    assert radio.calls[0] == "show_screen"
    # Экран CPS закрывается даже при отмене на пробном чтении.
    assert radio.calls[-1] == "screen_close"


class _FakePortInfo:
    def __init__(self, device, vid=None, pid=None, description=""):
        self.device = device
        self.vid = vid
        self.pid = pid
        self.description = description
        self.manufacturer = None
        self.product = None
        self.hwid = ""
        self.serial_number = None


def _no_posix_ports(monkeypatch):
    class _NoGlob:
        @staticmethod
        def glob(pattern):
            return []

    monkeypatch.setattr(gd77_flash, "glob", _NoGlob())


def test_detect_port_prefers_opengd77_usb_id(monkeypatch):
    _no_posix_ports(monkeypatch)
    monkeypatch.setattr(
        gd77_flash,
        "_serial_ports",
        lambda: [
            _FakePortInfo("COM1"),
            _FakePortInfo("COM14", 0x1FC9, 0x0094),
        ],
    )

    assert gd77_flash.detect_port() == "COM14"


def test_detect_port_uses_description_hint(monkeypatch):
    _no_posix_ports(monkeypatch)
    monkeypatch.setattr(
        gd77_flash,
        "_serial_ports",
        lambda: [
            _FakePortInfo("COM1"),
            _FakePortInfo("COM9", description="OpenGD77 USB Serial"),
        ],
    )

    assert gd77_flash.detect_port() == "COM9"


def test_detect_port_falls_back_to_single_usb_port(monkeypatch):
    _no_posix_ports(monkeypatch)
    monkeypatch.setattr(
        gd77_flash,
        "_serial_ports",
        lambda: [
            _FakePortInfo("COM1", description="Встроенный COM"),
            _FakePortInfo("COM13", description="USB Serial Device"),
        ],
    )

    assert gd77_flash.detect_port() == "COM13"


def test_detect_port_refuses_to_guess(monkeypatch):
    _no_posix_ports(monkeypatch)
    monkeypatch.setattr(
        gd77_flash,
        "_serial_ports",
        lambda: [
            _FakePortInfo("COM5", 0x1FC9, 0x0094),
            _FakePortInfo("COM6", 0x1FC9, 0x0094),
        ],
    )

    assert gd77_flash.detect_port() is None


def test_friendly_serial_error_translates_common_failures():
    assert "недоступен" in friendly_serial_error(
        PermissionError(13, "Access is denied")
    )
    assert "не найден" in friendly_serial_error(
        "could not open port 'COM14': FileNotFoundError(2, ...)"
    )
    assert "Порт закрыт" in friendly_serial_error("PortNotOpenError")
    assert "не отвечает" in friendly_serial_error(
        "нет ответа на R @0x00100 (получено 0 из 3 байт)"
    )
    assert "Связь с рацией потеряна" in friendly_serial_error(
        "read failed: [Errno 5] Input/output error"
    )

    # Реальный случай из лога: рацию выключили посреди чтения, и в
    # сообщении сразу и «нет ответа», и PermissionError, и пропавший порт.
    assert "не найден" in friendly_serial_error(
        "не удалось прочитать кодплаг: 115200: RuntimeError('нет ответа "
        "на R'); 9600: write failed PermissionError(13, 'Отказано в "
        "доступе'); could not open port COM14"
    )


def test_write_codeplug_progress_wrapper(monkeypatch):
    """Обёртка write_codeplug считает total по частям и чистит хуки."""
    from types import SimpleNamespace

    calls = []

    def orig(self, cp):
        # имитируем запись двух секторов через внутренний репортёр
        gd77_flash._report_write_progress(self, gd77_flash.SECTOR_SIZE)
        gd77_flash._report_write_progress(self, gd77_flash.SECTOR_SIZE)

    monkeypatch.setattr(gd77_flash, "_ORIG_WRITE_CODEPLUG", orig)

    radio = SimpleNamespace()
    cp = SimpleNamespace(
        parts=[
            SimpleNamespace(size=gd77_flash.SECTOR_SIZE),
            SimpleNamespace(size=gd77_flash.SECTOR_SIZE),
        ]
    )

    gd77_flash._md9600_write_codeplug(
        radio, cp, progress=lambda d, t: calls.append((d, t))
    )

    assert calls == [
        (gd77_flash.SECTOR_SIZE, 2 * gd77_flash.SECTOR_SIZE),
        (2 * gd77_flash.SECTOR_SIZE, 2 * gd77_flash.SECTOR_SIZE),
    ]
    assert radio._md9600_write_progress is None


def test_write_codeplug_progress_cleanup_on_error(monkeypatch):
    from types import SimpleNamespace

    def boom(self, cp):
        raise RuntimeError("flash failed")

    monkeypatch.setattr(gd77_flash, "_ORIG_WRITE_CODEPLUG", boom)

    radio = SimpleNamespace()
    cp = SimpleNamespace(parts=[SimpleNamespace(size=100)])

    with pytest.raises(RuntimeError):
        gd77_flash._md9600_write_codeplug(
            radio, cp, progress=lambda d, t: None
        )

    assert radio._md9600_write_progress is None


def test_report_write_progress_without_hook_is_noop():
    from types import SimpleNamespace

    radio = SimpleNamespace()
    gd77_flash._report_write_progress(radio, gd77_flash.SECTOR_SIZE)
