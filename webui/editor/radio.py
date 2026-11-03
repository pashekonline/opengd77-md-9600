"""Фоновые операции с рацией: очередь задач, один рабочий поток.

Порт у нас один, поэтому read/write/verify сериализуются через очередь.
Прогресс читается из /api/job (JS опрашивает каждые ~0.6 с).
"""
from __future__ import annotations

import hashlib
import queue
import threading
from datetime import datetime
from pathlib import Path
from time import monotonic

from opengd77.codeplug import Codeplug
from opengd77.comm import OpenGD77Radio

from debug_log import _log
from gd77_flash import (
    RadioCancelled,
    configure_radio_port,
    detect_port,
    friendly_serial_error,
    port_candidates,
    read_codeplug_safe,
)

from . import sats
from .state import state

MESSAGES = {
    "read": "Чтение кодплага из рации…",
    "write": "Запись кодплага в рацию…",
    "verify": "Проверка записи: чтение из рации…",
    "tle_fetch": "Поиск TLE на Celestrak…",
    "sat_write": "Запись спутников в рацию…",
}

HINTS = {
    "read": "Чтение ~15 с. Не отключайте рацию и кабель.",
    "verify": "Сравнение ~15 с. Не отключайте рацию.",
    "write": "Не отключайте кабель и не закрывайте страницу.",
    "tle_fetch": "Каталог satgs.ru/satonline.txt, нужен интернет.",
    "sat_write": "Не отключайте кабель и не закрывайте страницу.",
}


def _safe_close(radio) -> None:
    try:
        radio.close()
        return
    except Exception:
        pass

    for attr in ("serial", "ser", "s", "port"):
        try:
            obj = getattr(radio, attr, None)

            if obj is not None and hasattr(obj, "close"):
                obj.close()
                return
        except Exception:
            pass


class RadioManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._job: dict | None = None

    @property
    def job(self) -> dict | None:
        with self._lock:
            return self._job

    def busy(self) -> bool:
        with self._lock:
            return (
                self._job is not None
                and self._job["status"] == "running"
            ) or not self._queue.empty()

    def submit(self, kind: str, snapshot: bytes | None = None) -> str | None:
        """Ставит задачу в очередь. Возвращает текст ошибки или None."""
        with self._lock:
            if self._job is not None and self._job["status"] == "running":
                return "Операция с рацией уже выполняется"

            self._job = {
                "id": f"{kind}-{monotonic():.3f}",
                "kind": kind,
                "status": "running",
                "message": MESSAGES.get(kind, "Работа с рацией…"),
                "hint": HINTS.get(kind, ""),
                "started": monotonic(),
                "done": 0,
                "total": 0,
                "rate": None,
                "last_done": 0,
                "last_time": None,
                "cancel": threading.Event(),
                "cancellable": kind in ("read", "verify"),
                "snapshot": snapshot,
                "result": None,
                "result_error": False,
            }
            self._queue.put(self._job)

            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._worker, daemon=True
                )
                self._thread.start()

        return None

    def request_cancel(self) -> bool:
        job = self.job

        if job is None or not job["cancellable"]:
            return False

        job["cancel"].set()
        _log("radio_cancel_requested")
        return True

    # ------------------------------------------------------------------

    def _set_progress(self, job: dict, done: int, total: int) -> None:
        now = monotonic()
        job["done"] = done
        job["total"] = total

        if job["last_time"] is not None and done > job["last_done"]:
            dt = now - job["last_time"]

            if dt > 0.05:
                instant = (done - job["last_done"]) / dt

                if job["rate"] is None:
                    job["rate"] = instant
                else:
                    job["rate"] = job["rate"] * 0.8 + instant * 0.2

        job["last_done"] = done
        job["last_time"] = now

    def _finish(self, job: dict, status: str, result: str, error: bool) -> None:
        job["status"] = status
        job["result"] = result
        job["result_error"] = error

        with state.lock:
            state.notify(result, error)

    def _worker(self) -> None:
        while True:
            job = self._queue.get()

            try:
                self._run_job(job)
            except Exception as e:  # noqa: BLE001
                _log(f"worker error {e!r}")
                self._finish(job, "error", f"Ошибка операции: {e}", True)
            finally:
                self._queue.task_done()

    def _run_job(self, job: dict) -> None:
        kind = job["kind"]

        def report(done: int, total: int) -> None:
            self._set_progress(job, done, total)

        if kind == "tle_fetch":
            with state.lock:
                cache = state.sat_tle

            try:
                ok, total = sats.fetch_all(cache, progress=report)
            except Exception as e:
                self._finish(
                    job, "error", f"Ошибка поиска TLE: {e}", True
                )
                return

            if ok == 0:
                self._finish(
                    job,
                    "error",
                    f"TLE не получены ({total} спутников): "
                    "проверьте интернет и norad_id",
                    True,
                )
            else:
                self._finish(
                    job, "done", f"TLE обновлены: {ok} из {total}", False
                )
            return

        port = state.port

        if not port:
            try:
                port = detect_port()
            except Exception as e:
                _log(f"detect_port error {e!r}")
                port = None

        if not port:
            candidates = ", ".join(port_candidates()) or "нет доступных портов"
            job["status"] = "error"
            msg = (
                "Рация не найдена. Проверьте USB-кабель и драйвер. "
                f"Порты в системе: {candidates}. "
                "Укажите нужный: run.py --port COM14."
            )
            self._finish(job, "error", msg, True)
            return

        state.port = port

        if kind == "sat_write":
            try:
                with state.lock:
                    cache = state.sat_tle

                job["message"] = "Собираем блок и TLE…"
                block = sats.build_block(cache)
                job["message"] = MESSAGES["sat_write"]

                result = sats.write_to_radio(
                    port,
                    block,
                    progress=lambda n, t: self._set_progress(job, n, t),
                )

                msg = (
                    "Блок уже идентичен — запись не потребовалась"
                    if result == "same"
                    else "Спутниковый блок записан в рацию"
                )
                self._finish(job, "done", msg, False)
            except Exception as e:
                _log(f"sat job error {kind} {e!r}")
                self._finish(
                    job, "error", f"Ошибка: {friendly_serial_error(str(e))}",
                    True,
                )
            return

        try:
            radio = OpenGD77Radio(port)
        except Exception as e:
            job["status"] = "error"
            self._finish(
                job,
                "error",
                f"Не удалось открыть порт {port}: {e}. "
                "Проверьте кабель, драйвер и права доступа.",
                True,
            )
            return

        try:
            if kind == "read":
                cp = read_codeplug_safe(
                    radio,
                    progress=report,
                    cancel=job["cancel"].is_set,
                )
                backup = f"codeplug_backup_{datetime.now():%Y%m%d_%H%M%S}.g77"
                Path(backup).write_bytes(bytes(cp))

                with state.lock:
                    state.load_from_read(cp, backup)
                    total = state.total_channels()
                    zones = len(state.zone_labels())

                msg = (
                    f"Прочитано: {total} каналов, {zones} зон. "
                    f"Backup: {backup}"
                )
                self._finish(job, "done", msg, False)

            elif kind == "write":
                snapshot = job["snapshot"]

                if snapshot is None:
                    self._finish(job, "error", "Кодплаг не загружен", True)
                    return

                backup = (
                    f"codeplug_before_write_"
                    f"{datetime.now():%Y%m%d_%H%M%S}.g77"
                )
                Path(backup).write_bytes(snapshot)

                h = hashlib.sha256(snapshot).hexdigest()[:16]
                _log(f"write start len={len(snapshot)} sha={h}")

                configure_radio_port(radio)
                radio._md9600_written_sectors = {}
                radio.write_codeplug(Codeplug(snapshot), progress=report)

                with state.lock:
                    state.mark_saved()

                msg = (
                    f"Записано. Backup: {backup}. "
                    "Проверить запись — «Проверить»."
                )
                self._finish(job, "done", msg, False)

            elif kind == "verify":
                cp = read_codeplug_safe(
                    radio,
                    progress=report,
                    cancel=job["cancel"].is_set,
                )

                with state.lock:
                    same = bytes(cp) == state.data
                    changed = 0 if same else state.changed_against(cp)

                if same:
                    self._finish(
                        job, "done",
                        "Проверка пройдена: рация совпадает с буфером",
                        False,
                    )
                else:
                    self._finish(
                        job, "error",
                        f"Есть различия: изменено каналов — {changed}. "
                        "Можно записать заново.",
                        True,
                    )

        except RadioCancelled:
            self._finish(
                job,
                "cancelled",
                "Операция отменена. Рация осталась в обычном режиме.",
                False,
            )
        except Exception as e:
            _log(f"job error {kind} {e!r}")
            self._finish(
                job,
                "error",
                f"Ошибка операции: {friendly_serial_error(str(e))}. "
                "Подробности — в журнале.",
                True,
            )
        finally:
            _safe_close(radio)

    def api_status(self) -> dict:
        job = self.job

        if job is None:
            return {"active": False}

        elapsed = max(0.0, monotonic() - job["started"])
        running = job["status"] == "running"

        if running and job["rate"] and job["total"] > job["done"]:
            eta = (job["total"] - job["done"]) / job["rate"]
        else:
            eta = None

        pct = (
            int(job["done"] * 100 / job["total"]) if job["total"] else 0
        )

        return {
            "active": running,
            "id": job["id"],
            "kind": job["kind"],
            "status": job["status"],
            "message": job["message"],
            "hint": job["hint"],
            "cancellable": job["cancellable"] and running,
            "elapsed": round(elapsed, 1),
            "eta": round(eta, 1) if eta is not None else None,
            "pct": max(0, min(100, pct)),
            "done_raw": job["done"],
            "total_raw": job["total"],
            "done_kb": round(job["done"] / 1024, 1),
            "total_kb": round(max(0.0, job["total"] / 1024), 1),
            "result": job["result"],
            "result_error": job["result_error"],
        }


radio = RadioManager()
