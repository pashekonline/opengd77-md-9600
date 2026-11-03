from __future__ import annotations

import sys

from _vendor_path import _VENDOR  # noqa: F401

if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))

import contextlib
import glob
import struct
import time
from dataclasses import fields as dc_fields
from dataclasses import is_dataclass

from opengd77.codeplug import Codeplug
from opengd77.comm import OpenGD77Radio

from debug_log import _log

try:
    from opengd77.codeplug import MemType
except Exception:
    MemType = None

try:
    import serial
except Exception:
    serial = None


SECTOR_SIZE = 0x1000

_ORIG_READ_FLASH = getattr(OpenGD77Radio, "_read_flash", None)


class RadioCancelled(RuntimeError):
    """Операция с рацией прервана пользователем."""


def _check_cancel(cancel) -> None:
    if cancel is None:
        return

    try:
        cancelled = bool(cancel())
    except Exception:
        cancelled = False

    if cancelled:
        raise RadioCancelled("операция прервана пользователем")


def _delta_ms(start, end) -> str:
    if start is None or end is None:
        return "?"
    return f"{(end - start) * 1000.0:.1f}"


def friendly_serial_error(exc: Exception | str) -> str:
    """Переводит типовые ошибки serial/Windows в понятный текст."""
    if isinstance(exc, RadioCancelled):
        return "Операция отменена."

    txt = str(exc)
    low = txt.lower()

    if "прерван" in low or "cancelled" in low or "canceled" in low:
        return "Операция отменена."

    # Порядок важен: при выключении рации в сообщении бывает и
    # PermissionError, и «нет ответа», а верный вывод — порт пропал.
    if (
        "could not open port" in low
        or "filenotfounderror" in low
        or "не удается найти" in low
        or "не удаётся найти" in low
        or "система не может найти" in low
    ):
        return (
            "Порт не найден: рация выключилась или отключилась от USB. "
            "Включите рацию, переподключите кабель и повторите."
        )

    if (
        "portnotopen" in low
        or "port not open" in low
        or "not open" in low
    ):
        return (
            "Порт закрыт: рация отключилась или кабель был выдернут. "
            "Переподключите рацию и повторите."
        )

    if (
        "input/output error" in low
        or "errno 5" in low
        or "disconnected" in low
    ):
        return (
            "Связь с рацией потеряна. Переподключите USB-кабель "
            "и повторите чтение."
        )

    if (
        "permissionerror" in low
        or "access is denied" in low
        or "errno 13" in low
        or "отказано в доступе" in low
        or "writefile failed" in low
    ):
        return (
            "Порт недоступен: его может держать другая программа (CPS, "
            "терминал), не хватает прав доступа, либо рация отключилась. "
            "Закройте другие программы и переподключите USB."
        )

    if (
        "timeout" in low
        or "нет ответа" in low
        or "недочитано" in low
        or "некорректную длину" in low
    ):
        return (
            "Рация не отвечает. Проверьте, что она включена и находится "
            "в режиме CPS, затем повторите."
        )

    return txt


def _is_fatal(exc: Exception) -> bool:
    if isinstance(exc, RadioCancelled):
        return False

    txt = str(exc).lower()
    return any(
        x in txt
        for x in (
            "errno 5",
            "input/output error",
            "disconnected",
            "no such device",
            "bad file descriptor",
            "port not open",
            "portnotopen",
            "not open",
            "serialexception",
        )
    )


def _reset_io(pipe) -> None:
    # Дренируем входной буфер через in_waiting, не вызывая
    # reset_input_buffer()/PurgeComm на каждом чанке: на части
    # USB-serial драйверов Windows эти вызовы могут блокироваться.
    try:
        waiting = int(getattr(pipe, "in_waiting", -1))
    except Exception:
        waiting = -1

    if waiting > 0:
        try:
            pipe.read(waiting)
            return
        except Exception:
            pass
    elif waiting == 0:
        return

    try:
        pipe.reset_input_buffer()
    except Exception:
        pass


def _read_response(pipe, timeout: float = 1.0) -> bytes:
    r = b""

    old_timeout = None
    try:
        old_timeout = pipe.timeout
    except Exception:
        pass

    try:
        try:
            pipe.timeout = 0.1
        except Exception:
            pass

        deadline = time.time() + timeout

        while time.time() < deadline and len(r) < 8:
            try:
                chunk = pipe.read(8 - len(r))
                if chunk:
                    r += chunk
                else:
                    time.sleep(0.02)
            except Exception:
                break

    finally:
        try:
            if old_timeout is not None:
                pipe.timeout = old_timeout
        except Exception:
            pass

    return r


def _find_pipe(radio):
    for attr in ("serial", "ser", "s", "_serial", "port", "_port"):
        obj = getattr(radio, attr, None)
        if obj is None:
            continue

        needed = ("write", "flush", "read")
        if all(hasattr(obj, m) for m in needed):
            return obj

    needed = ("write", "flush", "read")
    if all(hasattr(radio, m) for m in needed):
        return radio

    return None


def _ensure_pipe_open(pipe, baudrate: int = 115200) -> bool:
    if pipe is None:
        return False

    try:
        if hasattr(pipe, "is_open") and not pipe.is_open:
            pipe.open()
            _log("pipe_opened")
    except Exception as e:
        _log(f"pipe_open_error={e!r}")
        return False

    for attr, value in (
        ("baudrate", baudrate),
        ("timeout", 1.0),
        ("write_timeout", 1.0),
        ("dsrdtr", False),
        ("rtscts", False),
    ):
        try:
            setattr(pipe, attr, value)
        except Exception:
            pass

    try:
        pipe.reset_input_buffer()
    except Exception:
        pass

    try:
        pipe.reset_output_buffer()
    except Exception:
        pass

    return True


def _ensure_radio_open(radio, baudrate: int = 115200) -> bool:
    opened = False

    for name in ("open", "connect", "_open", "_connect"):
        fn = getattr(radio, name, None)
        if callable(fn):
            try:
                fn()
                _log(f"radio_open_method={name}")
                opened = True
                break
            except Exception as e:
                _log(f"radio_open_method={name} error={e!r}")

    pipe = _find_pipe(radio)

    if pipe is not None:
        if _ensure_pipe_open(pipe, baudrate):
            opened = True
        else:
            _log("radio_pipe_open_failed")

    return opened


def _get_pipe(radio, baudrate: int = 115200):
    _ensure_radio_open(radio, baudrate)

    pipe = _find_pipe(radio)

    if pipe is None:
        raise RuntimeError("cannot find serial pipe in OpenGD77Radio")

    if not _ensure_pipe_open(pipe, baudrate):
        raise RuntimeError("serial pipe is not open")

    return pipe


def _close_radio_pipe(radio) -> None:
    pipe = _find_pipe(radio)

    if pipe is None:
        return

    try:
        if getattr(pipe, "is_open", False):
            pipe.close()
    except Exception:
        pass


def _extract_payload(args, kwargs=None):
    data = None
    ints = []

    for a in args:
        if isinstance(a, (bytes, bytearray, memoryview)):
            data = a
        elif isinstance(a, int):
            ints.append(a)

    if kwargs:
        if data is None:
            for k in ("data", "buf", "buffer", "payload", "value"):
                v = kwargs.get(k, None)
                if isinstance(v, (bytes, bytearray, memoryview)):
                    data = v
                    break

        for k in ("addr", "address", "offset"):
            v = kwargs.get(k, None)
            if isinstance(v, int):
                ints.append(v)

    addr = ints[-1] if ints else 0
    return data, addr


def _coerce_bytes(x) -> bytes:
    if isinstance(x, bytes):
        return x
    if isinstance(x, (bytearray, memoryview)):
        return bytes(x)
    if isinstance(x, list):
        return bytes(x)
    return bytes(x)


def _try_call(fn, arg_tuples):
    last_exc = None

    for args in arg_tuples:
        try:
            return fn(*args)
        except TypeError as e:
            last_exc = e
        except Exception as e:
            if _is_fatal(e):
                raise
            last_exc = e

    if last_exc is not None:
        raise last_exc

    raise RuntimeError("no valid call signature")


def _unique_ints(vals):
    out = []
    seen = set()

    for v in vals:
        try:
            iv = int(v)
        except Exception:
            continue

        if iv in seen:
            continue

        seen.add(iv)
        out.append(iv)

    return out


def _candidate_ints(objects, attrs):
    vals = []

    for obj in objects:
        if obj is None:
            continue

        for attr in attrs:
            try:
                v = getattr(obj, attr)
            except Exception:
                continue

            if isinstance(v, bool):
                continue

            if isinstance(v, int):
                vals.append(v)
                continue

            try:
                iv = int(v)
                vals.append(iv)
                continue
            except Exception:
                pass

            try:
                vv = getattr(v, "value", None)
                if isinstance(vv, int) and not isinstance(vv, bool):
                    vals.append(vv)
            except Exception:
                pass

    return _unique_ints(vals)


def _flash_memtypes():
    objects = []

    if MemType is not None:
        objects.append(MemType)

    objects.extend([OpenGD77Radio, Codeplug])

    vals = _candidate_ints(
        objects,
        (
            "FLASH",
            "MEMTYPE_FLASH",
            "AREA_FLASH",
            "MEM_FLASH",
            "CODEPLUG_FLASH",
        ),
    )

    if 1 not in vals:
        vals.append(1)

    return vals


def _flash_areas():
    objects = []

    if MemType is not None:
        objects.append(MemType)

    objects.extend([OpenGD77Radio, Codeplug])

    vals = _candidate_ints(
        objects,
        (
            "AREA_FLASH",
            "FLASH",
            "MEMTYPE_FLASH",
            "MEM_FLASH",
        ),
    )

    for x in (1, 0, 2, 3, 4):
        if x not in vals:
            vals.append(x)

    return vals


def _lowlevel_read_area(pipe, area: int, addr: int, size: int):
    out = bytearray()
    a = addr
    rem = size

    old_timeout = None
    try:
        old_timeout = pipe.timeout
    except Exception:
        pass

    try:
        try:
            pipe.timeout = 0.2
        except Exception:
            pass

        while rem > 0:
            n = min(rem, 1024)

            req = bytes([
                ord("R"),
                area & 0xFF,
                (a >> 24) & 0xFF,
                (a >> 16) & 0xFF,
                (a >> 8) & 0xFF,
                a & 0xFF,
                (n >> 8) & 0xFF,
                n & 0xFF,
            ])

            _reset_io(pipe)
            pipe.write(req)
            pipe.flush()
            time.sleep(0.05)

            r = b""
            deadline = time.time() + 2.0

            while time.time() < deadline and len(r) < n + 3:
                try:
                    chunk = pipe.read(n + 3 - len(r))
                    if chunk:
                        r += chunk
                    else:
                        time.sleep(0.02)
                except Exception as e:
                    if _is_fatal(e):
                        raise
                    break

            if len(r) < n + 3 or r[0] != ord("R"):
                return None

            out.extend(r[3:3 + n])
            a += n
            rem -= n

        return bytes(out)

    finally:
        try:
            if old_timeout is not None:
                pipe.timeout = old_timeout
        except Exception:
            pass


def _read_flash_safe(radio, addr: int, size: int) -> bytes:
    errors = []

    if _ORIG_READ_FLASH is not None:
        try:
            return _coerce_bytes(
                _try_call(
                    _ORIG_READ_FLASH,
                    (
                        (radio, addr, size),
                        (radio, size, addr),
                    ),
                )
            )
        except Exception as e:
            if _is_fatal(e):
                raise
            errors.append(f"_ORIG_READ_FLASH:{e!r}")

    for name in ("_read_flash", "read_flash"):
        fn = getattr(radio, name, None)
        if not callable(fn):
            continue

        try:
            return _coerce_bytes(
                _try_call(
                    fn,
                    (
                        (addr, size),
                        (size, addr),
                    ),
                )
            )
        except Exception as e:
            if _is_fatal(e):
                raise
            errors.append(f"{name}:{e!r}")

    memtypes = _flash_memtypes()

    try:
        pipe = _get_pipe(radio)
    except Exception as e:
        errors.append(f"get_pipe:{e!r}")
        pipe = None

    if pipe is not None:
        # Сессия ставит короткий таймаут только на время своего чтения
        # и возвращает port.timeout, чтобы vendor-запись читала ответы
        # с привычными 2 секундами.
        with _read_timeout_session(pipe):
            for mt in memtypes:
                try:
                    data = _read_chunk_safe(pipe, mt, addr, size)

                    if len(data) == size:
                        _log(f"read_flash_safe memtype={mt} ok")
                        return data

                    errors.append(f"memtype[{mt}]:short {len(data)}/{size}")
                except Exception as e:
                    if _is_fatal(e):
                        raise
                    errors.append(f"memtype[{mt}]:{e!r}")

    if pipe is not None:
        for area in _flash_areas():
            try:
                data = _lowlevel_read_area(pipe, area, addr, size)
                if data is not None and len(data) == size:
                    _log(f"read_flash_safe lowlevel area={area} ok")
                    return data
            except Exception as e:
                if _is_fatal(e):
                    raise
                errors.append(f"lowlevel_area[{area}]:{e!r}")

    raise RuntimeError(
        "read flash method not found: " + "; ".join(errors[-12:])
    )


def _read_flash_range(radio, addr: int, size: int, chunk: int = SECTOR_SIZE) -> bytes:
    out = bytearray()

    while len(out) < size:
        n = min(chunk, size - len(out))
        b = bytes(_read_flash_safe(radio, addr + len(out), n))

        if len(b) != n:
            _log(
                f"read_range_short addr=0x{addr + len(out):06X} "
                f"want={n} got={len(b)}"
            )
            b = b[:n].ljust(n, b"\xff")

        out.extend(b)

    return bytes(out)


def configure_radio_port(radio, baudrate: int = 115200) -> bool:
    """
    Настраивает последовательный порт до открытия сессии.

    Vendor-конструктор OpenGD77Radio оставляет baudrate по умолчанию (9600),
    а MD-9600 работает на 115200. Если открыть порт, выставить параметры и
    закрыть, при последующем port_session() настройки сохранятся.
    """
    pipe = _find_pipe(radio)

    if pipe is None:
        return False

    try:
        was_open = bool(getattr(pipe, "is_open", False))
    except Exception:
        was_open = False

    try:
        if not was_open:
            pipe.open()

        for attr, value in (
            ("baudrate", baudrate),
            ("timeout", 2.0),
            ("write_timeout", 2.0),
            ("dsrdtr", False),
            ("rtscts", False),
        ):
            try:
                setattr(pipe, attr, value)
            except Exception:
                pass

        _reset_io(pipe)
        _log(
            f"port_configured baudrate={getattr(pipe, 'baudrate', '?')} "
            f"port={getattr(pipe, 'port', '?')}"
        )
        return True

    except Exception as e:
        _log(f"port_configure_error={e!r}")
        return False

    finally:
        if not was_open:
            try:
                pipe.close()
            except Exception:
                pass


def _apply_read_timeout(pipe, value: float) -> None:
    """Ставит таймаут только если он действительно меняется.

    Присваивание pyserial-овского timeout каждый раз зря дёргает
    переконфигурацию порта (SetCommState/SetCommTimeouts), а в цикле
    чтения это тысячи лишних обращений к драйверу.
    """
    try:
        if getattr(pipe, "timeout", None) == value:
            return
        pipe.timeout = value
    except Exception:
        pass


@contextlib.contextmanager
def _read_timeout_session(pipe, tick: float = 0.2):
    """Один короткий таймаут чтения на всю сессию, с восстановлением."""
    try:
        old = pipe.timeout
    except Exception:
        old = None

    _apply_read_timeout(pipe, tick)

    try:
        yield
    finally:
        if old is not None:
            _apply_read_timeout(pipe, old)


def _read_exact(pipe, count: int, timeout: float) -> bytes:
    """Читает ровно count байт или возвращает сколько успело прийти."""
    buf = bytearray()
    deadline = time.time() + timeout

    # Короткий таймаут держит сессия чтения (_read_timeout_session);
    # здесь он ставится лишь при необходимости.
    _apply_read_timeout(pipe, 0.2)

    while len(buf) < count and time.time() < deadline:
        try:
            waiting = int(getattr(pipe, "in_waiting", -1))
        except Exception:
            waiting = -1

        if waiting == 0:
            # Ждём первый байт с обычным serial-timeout, а не крутимся
            # в busy-loop: так ответ приходит без задержки в 10 мс.
            n = 1
        elif waiting < 0:
            # Порт без in_waiting: полагаемся на timeout чтения.
            n = count - len(buf)
        else:
            n = min(count - len(buf), waiting)

        try:
            chunk = pipe.read(n)
        except Exception as e:
            if _is_fatal(e):
                raise
            break

        if chunk:
            buf.extend(chunk)
        else:
            time.sleep(0.005)

    return bytes(buf)


def _read_chunk_once(
    pipe,
    memtype: int,
    addr: int,
    length: int,
    *,
    retries: int = 3,
    cancel=None,
    timing: bool = False,
) -> bytes:
    """
    Один R-запрос (не больше 32 байт) с повторами.

    Vendor _read_memory уходит в бесконечный цикл, если рация отвечает
    нулевой длиной. Здесь такой ответ считается ошибкой и повторяется.
    """
    last_error = None

    for attempt in range(1, retries + 1):
        _check_cancel(cancel)

        t0 = time.perf_counter()
        t_write = None
        t_header = None
        t_data = None

        try:
            _reset_io(pipe)

            req = struct.pack(
                ">BBIH", ord("R"), int(memtype), int(addr), int(length)
            )
            # Без flush(): на части USB-serial драйверов Windows
            # FlushFileBuffers может блокироваться, а 8 байт запроса
            # драйвер отправляет и сам.
            pipe.write(req)
            t_write = time.perf_counter()

            hdr = _read_exact(pipe, 3, 0.75)
            t_header = time.perf_counter()

            if len(hdr) != 3:
                raise RuntimeError(
                    f"нет ответа на R @0x{addr:05X} "
                    f"(получено {len(hdr)} из 3 байт)"
                )

            c, got = struct.unpack(">BH", hdr)

            if c != ord("R"):
                raise RuntimeError(
                    f"неверный ответ R @0x{addr:05X}: {hdr.hex()}"
                )

            if got <= 0 or got > 4096:
                raise RuntimeError(
                    f"рация вернула некорректную длину {got} "
                    f"@0x{addr:05X}"
                )

            data = _read_exact(pipe, got, 3.0)
            t_data = time.perf_counter()

            if len(data) != got:
                raise RuntimeError(
                    f"недочитано @0x{addr:05X}: "
                    f"{len(data)} из {got} байт"
                )

            # Первый чанк каждого 4К-сектора и любой повтор: видно,
            # где теряется время — на записи, заголовке или данных.
            if timing or attempt > 1 or (addr % SECTOR_SIZE) < 32:
                _log(
                    f"read_timing addr=0x{addr:05X} attempt={attempt} "
                    f"write_ms={_delta_ms(t0, t_write)} "
                    f"header_ms={_delta_ms(t_write, t_header)} "
                    f"data_ms={_delta_ms(t_header, t_data)} "
                    f"total_ms={_delta_ms(t0, t_data)}"
                )

            return data

        except RadioCancelled:
            raise

        except Exception as e:
            if _is_fatal(e):
                raise

            last_error = e

            if t_write is not None:
                _log(
                    f"read_timing addr=0x{addr:05X} attempt={attempt} "
                    f"failed=1 write_ms={_delta_ms(t0, t_write)} "
                    f"header_ms={_delta_ms(t_write, t_header)} "
                    f"data_ms={_delta_ms(t_header, t_data)} "
                    f"total_ms={_delta_ms(t0, time.perf_counter())}"
                )

            _log(
                f"read_chunk addr=0x{addr:05X} len={length} "
                f"attempt={attempt}/{retries} error={e!r}"
            )

            if attempt < retries:
                time.sleep(0.15)

    raise RuntimeError(
        f"не удалось прочитать 0x{addr:05X} ({length} байт): {last_error}"
    )


def _read_chunk_safe(
    pipe,
    memtype: int,
    addr: int,
    length: int,
    *,
    retries: int = 3,
    cancel=None,
    timing: bool = False,
) -> bytes:
    """
    Читает произвольный диапазон кусками по 32 байта.

    Рация принимает не больше MAX_TRANSFER_SIZE (32) байт за один
    R-запрос, поэтому большие диапазоны (например, 4К-сектор при
    read-modify-write) разбиваются здесь.
    """
    if length <= 0:
        return b""

    out = bytearray()

    while len(out) < length:
        _check_cancel(cancel)

        n = min(32, length - len(out))
        data = _read_chunk_once(
            pipe,
            memtype,
            addr + len(out),
            n,
            retries=retries,
            cancel=cancel,
            timing=timing,
        )

        if not data:
            raise RuntimeError(
                f"пустой ответ @0x{addr + len(out):05X}"
            )

        out.extend(data[:n])

    return bytes(out)


def _read_screen_begin(radio) -> None:
    """Показывает на рации экран чтения и входит в CPS-режим."""
    try:
        radio._show_screen((b"CPS", b"Reading", b"Codeplug"))
        radio._flash_green()
        radio._save()
    except Exception as e:
        # Экран — косметика; само чтение может быть доступно и без него.
        _log(f"read_screen_error={e!r}")


def _read_codeplug_at(
    radio,
    pipe,
    part_list,
    total: int,
    progress=None,
    cancel=None,
) -> Codeplug:
    # Один короткий таймаут на всю сессию вместо переконфигурации порта
    # на каждом чанке.
    with _read_timeout_session(pipe):
        return _read_codeplug_chunks(
            radio,
            pipe,
            part_list,
            total,
            progress,
            cancel,
        )


def _read_codeplug_chunks(
    radio,
    pipe,
    part_list,
    total: int,
    progress=None,
    cancel=None,
) -> Codeplug:
    cp = Codeplug()

    try:
        done = 0
        last_report = -1
        report_step = max(1024, total // 100) if total else 1024
        read_start = time.perf_counter()

        for index, p in enumerate(part_list):
            _check_cancel(cancel)

            part_start = time.perf_counter()
            size = int(p.size)
            file_addr = int(p.file_addr)
            radio_addr = int(p.radio_addr)
            memtype = int(p.memtype)

            _log(
                f"read_part {index + 1}/{len(part_list)} "
                f"radio=0x{radio_addr:05X} file=0x{file_addr:05X} "
                f"size=0x{size:05X} memtype={memtype}"
            )

            buf = bytearray()

            while len(buf) < size:
                _check_cancel(cancel)

                n = min(32, size - len(buf))
                data = _read_chunk_safe(
                    pipe,
                    memtype,
                    radio_addr + len(buf),
                    n,
                    cancel=cancel,
                    timing=(not buf),
                )

                if not data:
                    raise RuntimeError(
                        f"пустой ответ @0x{radio_addr + len(buf):05X}"
                    )

                # Не даём одному ответу выйти за границы части.
                buf.extend(data[:n])
                done += len(data[:n])

                # Прогресс в UI обновляем каждый чанк, чтобы полоса
                # двигалась плавно, а ETA успевала пересчитываться.
                if progress is not None:
                    try:
                        progress(done, total)
                    except Exception as e:
                        _log(f"read_progress_error={e!r}")

                # В журнал пишем реже, чтобы не раздувать лог.
                if done - last_report >= report_step or done >= total:
                    last_report = done

                    _log(
                        f"read_progress part={index + 1}/{len(part_list)} "
                        f"done={done}/{total} "
                        f"addr=0x{radio_addr + len(buf):05X}"
                    )

            if len(buf) != size:
                buf = buf[:size].ljust(size, b"\xff")

            end = file_addr + size

            if end > len(cp.data):
                raise RuntimeError(
                    f"часть выходит за кодплаг: file=0x{file_addr:05X} "
                    f"size=0x{size:05X}"
                )

            cp.data[file_addr:end] = buf

            part_secs = time.perf_counter() - part_start
            chunks = max(1, (size + 31) // 32)

            _log(
                f"read_part_done {index + 1}/{len(part_list)} "
                f"file=0x{file_addr:05X} size=0x{size:05X} "
                f"done={done}/{total} secs={part_secs:.1f} "
                f"per_chunk_ms={part_secs * 1000 / chunks:.1f}"
            )

        _log(
            f"read_codeplug done bytes={done} "
            f"secs={time.perf_counter() - read_start:.1f}"
        )

        if progress is not None:
            try:
                progress(total, total)
            except Exception:
                pass

        return cp

    finally:
        try:
            radio._C_scr_close()
        except Exception as e:
            _log(f"read_screen_close_error={e!r}")


def read_codeplug_safe(
    radio,
    progress=None,
    parts=None,
    baudrate: int | None = None,
    cancel=None,
) -> Codeplug:
    """
    Читает кодплаг кусками по 32 байта с повторами и прогрессом.

    В отличие от vendor read_codeplug():
    - порт переводится на 115200 (с откатом на 9600);
    - нулевая длина ответа не приводит к вечному циклу;
    - каждый кусок повторяется до 3 раз;
    - progress(done, total) вызывается по ходу чтения;
    - cancel() позволяет прервать чтение между чанками.
    """
    base = Codeplug()
    part_list = list(parts if parts is not None else base.parts)
    total = sum(int(p.size) for p in part_list)
    first = part_list[0] if part_list else None
    candidates = (baudrate,) if baudrate else (115200, 9600)
    errors = []

    for baud in candidates:
        _check_cancel(cancel)
        screen_open = False

        try:
            _close_radio_pipe(radio)
            _ensure_radio_open(radio, baud)
            pipe = _find_pipe(radio)

            if pipe is None:
                raise RuntimeError("не найден последовательный порт рации")

            # Сначала CPS-хендшейк: некоторым прошивкам нужно получить
            # команду C, прежде чем отвечать на R.
            _read_screen_begin(radio)
            screen_open = True

            if first is not None:
                # Пробное чтение: определяем, на какой скорости рация
                # реально отвечает, прежде чем читать 128 КБ.
                probe_len = min(32, int(first.size))
                probe = _read_chunk_safe(
                    pipe,
                    int(first.memtype),
                    int(first.radio_addr),
                    probe_len,
                    retries=2,
                    cancel=cancel,
                    timing=True,
                )

                if not probe:
                    raise RuntimeError("пустой ответ на пробное чтение")

            _log(
                f"read_codeplug start parts={len(part_list)} "
                f"total={total} baudrate={baud}"
            )

            # _read_codeplug_at сам закрывает экран CPS в своём finally,
            # поэтому сбрасываем флаг: снаружи закрываем только если
            # отмена или ошибка случились на пробном чтении.
            try:
                return _read_codeplug_at(
                    radio,
                    pipe,
                    part_list,
                    total,
                    progress,
                    cancel=cancel,
                )
            finally:
                screen_open = False

        except RadioCancelled:
            _log("read_codeplug_cancelled")
            raise

        except Exception as e:
            if _is_fatal(e):
                raise

            errors.append(f"{baud}: {e!r}")
            _log(f"read_baudrate_failed baudrate={baud} error={e!r}")

        finally:
            # Экран CPS закрываем в любом случае: и при успехе, и при
            # ошибке, и при отмене — иначе рация останется в режиме
            # программирования.
            if screen_open:
                try:
                    radio._C_scr_close()
                except Exception as e:
                    _log(f"read_screen_close_error={e!r}")

    raise RuntimeError(
        "не удалось прочитать кодплаг: " + "; ".join(errors)
    )


def _ensure_cps(radio) -> bool:
    _ensure_radio_open(radio)

    for name in (
        "show_cps",
        "enter_cps",
        "_show_cps",
        "cps_enter",
        "enter_programming_mode",
        "_enter_programming_mode",
    ):
        fn = getattr(radio, name, None)
        if callable(fn):
            try:
                fn()
                _log(f"ensure_cps={name}")
                time.sleep(0.2)
                return True
            except Exception as e:
                _log(f"ensure_cps={name} error={e!r}")

    try:
        pipe = _get_pipe(radio)

        _reset_io(pipe)
        pipe.write(bytes([ord("C"), 0]))
        pipe.flush()
        time.sleep(0.2)

        try:
            r = pipe.read(64)
            _log(f"ensure_cps_lowlevel_response={r!r}")
        except Exception as e:
            _log(f"ensure_cps_lowlevel_read_error={e!r}")

        return True

    except Exception as e:
        _log(f"ensure_cps_lowlevel_error={e!r}")
        return False


def _flash_prepare(pipe, sector: int, retries: int = 3) -> bool:
    req = bytes([
        ord("X"),
        1,
        (sector >> 16) & 0xFF,
        (sector >> 8) & 0xFF,
        sector & 0xFF,
    ])

    for attempt in range(retries):
        try:
            _reset_io(pipe)
            pipe.write(req)
            pipe.flush()
            time.sleep(0.15)

            r = _read_response(pipe, 1.0)

            if r and r[0] != ord("-"):
                return True

            _log(f"prepare sector=0x{sector:X} attempt={attempt + 1} bad={r!r}")

        except Exception as e:
            if _is_fatal(e):
                raise
            _log(f"prepare sector=0x{sector:X} attempt={attempt + 1} error={e!r}")

        time.sleep(0.3)

    return False


def _flash_send(pipe, addr: int, data: bytes, retries: int = 3) -> bool:
    off = 0

    while off < len(data):
        chunk = data[off:off + 1024]
        a = addr + off

        req = bytes([
            ord("X"),
            2,
            (a >> 24) & 0xFF,
            (a >> 16) & 0xFF,
            (a >> 8) & 0xFF,
            a & 0xFF,
            (len(chunk) >> 8) & 0xFF,
            len(chunk) & 0xFF,
        ]) + chunk

        ok = False

        for attempt in range(retries):
            try:
                _reset_io(pipe)
                pipe.write(req)
                pipe.flush()
                time.sleep(0.05)

                r = _read_response(pipe, 1.0)

                if r and r[0] != ord("-"):
                    ok = True
                    break

                _log(f"send addr=0x{a:X} attempt={attempt + 1} bad={r!r}")

            except Exception as e:
                if _is_fatal(e):
                    raise
                _log(f"send addr=0x{a:X} attempt={attempt + 1} error={e!r}")

            time.sleep(0.2)

        if not ok:
            return False

        off += len(chunk)

    return True


def _flash_commit(pipe, retries: int = 3) -> bool:
    for attempt in range(retries):
        try:
            _reset_io(pipe)
            pipe.write(bytes([ord("X"), 3]))
            pipe.flush()

            time.sleep(0.7)

            r = _read_response(pipe, 2.0)

            if r and r[0] != ord("-"):
                time.sleep(1.0)
                return True

            _log(f"commit attempt={attempt + 1} bad={r!r}")

        except Exception as e:
            if _is_fatal(e):
                raise
            _log(f"commit attempt={attempt + 1} error={e!r}")

        time.sleep(0.7)

    return False


def _report_write_progress(radio, step_bytes: int) -> None:
    """Прогресс записи: вызывается по секторам из _write_sector_aligned.

    _md9600_write_progress ставится обёрткой write_codeplug ниже;
    без неё (чтение, другие пути) — no-op.
    """
    progress = getattr(radio, "_md9600_write_progress", None)

    if progress is None:
        return

    total = getattr(radio, "_md9600_write_total", 0) or 0
    done = getattr(radio, "_md9600_write_done", 0) + step_bytes

    if total:
        done = min(done, total)

    radio._md9600_write_done = done

    try:
        progress(done, total)
    except Exception as e:
        _log(f"write_progress_error={e!r}")


def _write_sector_aligned(radio, addr: int, payload: bytes) -> bool:
    pipe = _get_pipe(radio)

    written_map = getattr(radio, "_md9600_written_sectors", None)
    if written_map is None:
        written_map = {}
        radio._md9600_written_sectors = written_map

    if not payload:
        return True

    end = addr + len(payload)
    first_sector = addr // SECTOR_SIZE
    last_sector = (end - 1) // SECTOR_SIZE

    written = 0
    skipped = 0
    written_since_pause = 0

    _log(
        f"write_diff addr=0x{addr:06X} len={len(payload)} "
        f"sectors={first_sector}..{last_sector}"
    )

    for sector in range(first_sector, last_sector + 1):
        s0 = sector * SECTOR_SIZE
        s1 = s0 + SECTOR_SIZE

        try:
            current = bytes(_read_flash_safe(radio, s0, SECTOR_SIZE))
        except Exception as e:
            if _is_fatal(e):
                raise
            _log(f"sector={sector} read_current_error={e!r}")
            return False

        if len(current) != SECTOR_SIZE:
            current = current[:SECTOR_SIZE].ljust(SECTOR_SIZE, b"\xff")

        target = bytearray(current)

        ov0 = max(addr, s0)
        ov1 = min(end, s1)

        if ov0 < ov1:
            target[ov0 - s0:ov1 - s0] = payload[ov0 - addr:ov1 - addr]

        target_bytes = bytes(target)

        if target_bytes == current:
            _log(f"sector={sector} skip_unchanged")
            skipped += 1
            _report_write_progress(radio, SECTOR_SIZE)
            continue

        sector_addr = s0

        if sector_addr in written_map:
            if written_map[sector_addr] != target_bytes:
                _log(
                    f"sector={sector} CONFLICT addr=0x{sector_addr:06X} "
                    f"already_written_different_data"
                )
                return False

            _log(f"sector={sector} already_written_same_skip")
            skipped += 1
            _report_write_progress(radio, SECTOR_SIZE)
            continue

        ok = False

        for attempt in range(2):
            if not _flash_prepare(pipe, sector):
                _log(f"sector={sector} prepare_failed attempt={attempt + 1}")
                continue

            if not _flash_send(pipe, s0, target_bytes):
                _log(f"sector={sector} send_failed attempt={attempt + 1}")
                continue

            if not _flash_commit(pipe):
                _log(f"sector={sector} commit_failed attempt={attempt + 1}")
                continue

            time.sleep(0.2)

            try:
                rb = bytes(_read_flash_safe(radio, s0, SECTOR_SIZE))
            except Exception as e:
                if _is_fatal(e):
                    raise
                _log(f"sector={sector} verify_read_error={e!r}")
                rb = b""

            if len(rb) != SECTOR_SIZE:
                rb = rb[:SECTOR_SIZE].ljust(SECTOR_SIZE, b"\xff")

            if rb == target_bytes:
                _log(f"sector={sector} verify_ok")
                ok = True
                break

            _log(f"sector={sector} verify_mismatch attempt={attempt + 1}")

        if not ok:
            _log(f"sector={sector} write_failed")
            return False

        written_map[sector_addr] = target_bytes
        written += 1
        written_since_pause += 1
        _report_write_progress(radio, SECTOR_SIZE)

        time.sleep(0.5)

        if written_since_pause >= 3:
            _log("throttle_pause_after_3_sectors")
            time.sleep(2.0)
            written_since_pause = 0

    _log(f"write_diff_done written={written} skipped={skipped}")
    return True


def _protocol_write_flash(self, *args, **kwargs):
    data, addr = _extract_payload(args, kwargs)

    if data is None:
        raise RuntimeError("_write_flash called without data")

    payload = bytes(data)

    _log(
        f"flash_enter addr=0x{addr:06X} len={len(payload)} "
        f"first={payload[:8].hex()}"
    )

    if not payload:
        return True

    ok = _write_sector_aligned(self, addr, payload)

    if not ok:
        raise RuntimeError("OpenGD77 flash prepare/send/commit failed")

    return True


def _md9600_write_memory(self, memtype, data, addr):
    return self._write_flash(data, addr)


_ORIG_WRITE_CODEPLUG = OpenGD77Radio.write_codeplug


def _md9600_write_codeplug(self, cp, progress=None) -> None:
    """write_codeplug с необязательным прогрессом (done, total) в байтах.

    Сумма частей известна заранее, а done складывается по секторам
    внутри _write_sector_aligned через _report_write_progress.
    """
    total = 0

    if progress is not None:
        try:
            total = sum(int(p.size) for p in cp.parts)
        except Exception as e:
            _log(f"write_total_error={e!r}")
            total = 0

    self._md9600_write_total = total
    self._md9600_write_done = 0
    self._md9600_write_progress = progress

    try:
        _ORIG_WRITE_CODEPLUG(self, cp)
    finally:
        self._md9600_write_progress = None
        self._md9600_write_total = 0
        self._md9600_write_done = 0


def install_md9600_flash_protocol() -> None:
    if getattr(OpenGD77Radio, "_md9600_flash_installed", False):
        return

    OpenGD77Radio._write_flash = _protocol_write_flash
    OpenGD77Radio._write_memory = _md9600_write_memory
    OpenGD77Radio.write_codeplug = _md9600_write_codeplug
    OpenGD77Radio._md9600_flash_installed = True

    _log("flash_protocol_installed")


install_md9600_flash_protocol()


def _part_tuple(part):
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
        vals = tuple(part)
        if len(vals) >= 4:
            return tuple(int(x) for x in vals[:4])
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

    try:
        items = list(vars(part).items())
        ints = []

        for _, v in items:
            try:
                ints.append(int(v))
            except Exception:
                try:
                    ints.append(int(getattr(v, "value")))
                except Exception:
                    pass

        if len(ints) >= 4:
            return tuple(ints[:4])
    except Exception:
        pass

    return None


def _part_fields(part):
    t = _part_tuple(part)

    if t is None or len(t) < 4:
        return None

    try:
        return (int(t[0]), int(t[1]), int(t[2]), int(t[3]))
    except Exception:
        return None


RADIO_USB_VID = 0x1FC9
RADIO_USB_PID = 0x0094

# OpenGD77 прошивает USB-CDC как NXP "MCU Virtual Com Demo" (1FC9:0094),
# но на части сборок и драйверов строка описания другая, поэтому она тоже
# в списке признаков.
RADIO_PORT_HINTS = (
    "1fc9:0094",
    "opengd77",
    "md-9600",
    "md9600",
    "gd-77",
    "virtual com",
)


def port_candidates() -> list[str]:
    """Все последовательные порты в системе — для подсказки пользователю."""
    return [
        str(getattr(info, "device", "") or "")
        for info in _serial_ports()
        if getattr(info, "device", None)
    ]


def _serial_ports():
    try:
        from serial.tools import list_ports
    except Exception:
        return []

    try:
        return list(list_ports.comports())
    except Exception as e:
        _log(f"list_ports_error={e!r}")
        return []


def _port_text(info) -> str:
    return " ".join(
        str(x or "")
        for x in (
            getattr(info, "device", ""),
            getattr(info, "description", ""),
            getattr(info, "manufacturer", ""),
            getattr(info, "product", ""),
            getattr(info, "hwid", ""),
            getattr(info, "serial_number", ""),
        )
    ).lower()


def detect_port(preferred: str | None = None) -> str | None:
    """Ищет порт рации: сначала USB VID:PID OpenGD77, потом типовые имена."""
    if preferred:
        return preferred

    ports = (
        glob.glob("/dev/ttyACM*") +
        glob.glob("/dev/ttyUSB*")
    )

    if ports:
        return ports[0]

    infos = _serial_ports()

    if not infos:
        return None

    exact = [
        info for info in infos
        if getattr(info, "vid", None) == RADIO_USB_VID
        and getattr(info, "pid", None) == RADIO_USB_PID
    ]

    if len(exact) == 1:
        _log(f"detect_port_by_vid_pid={exact[0].device}")
        return exact[0].device

    hinted = [
        info for info in infos
        if any(h in _port_text(info) for h in RADIO_PORT_HINTS)
    ]

    if len(hinted) == 1:
        _log(f"detect_port_by_hint={hinted[0].device}")
        return hinted[0].device

    usb = [
        info for info in infos
        if "usb" in _port_text(info)
    ]

    # Угадывать на несколько портах опасно: можно написать не туда.
    if len(usb) == 1:
        _log(f"detect_port_single_usb={usb[0].device}")
        return usb[0].device

    _log(
        "detect_port_ambiguous ports="
        + ",".join(str(getattr(i, "device", "?")) for i in infos)
    )

    return None
