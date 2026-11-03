"""Общее состояние редактора: буфер кодплага, undo/redo, журнал.

Один пользователь, несколько потоков Django — все мутации под self.lock.
Логика повторяет MD9600App из src/md9600_tui.py поверх тех же модулей src/.
"""
from __future__ import annotations

import sys
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT / "src", ROOT / "vendor"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from opengd77.codeplug import Codeplug

import gd77_patch
from cp_utils import _channel_valid
from debug_log import _log
from gd77_zones import build_channel_zone_map, get_zones, zone_id, zone_name

EVENT_LIMIT = 200
UNDO_LIMIT = 30
ALL_FILTER = "__all__"
APP_TITLE = f"MD-9600 Codeplug Editor {gd77_patch.CODE_VERSION}"


class EditorState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.port: str | None = None
        self.data: bytes | None = None
        self.saved_bytes: bytes | None = None
        self.backup_path: str | None = None
        self.zone_map: dict[int, list[tuple[int, str]]] = {}

        self._undo: list[bytes] = []
        self._redo: list[bytes] = []

        self._events: deque[str] = deque(maxlen=EVENT_LIMIT)
        self.pending_message: tuple[str, bool] | None = None
        self.pending_import: dict | None = None

        # спутники: кэш скачанных TLE по имени
        self.sat_tle: dict[str, tuple[str, str]] = {}

    # ---------- буфер ----------

    def codeplug(self) -> Codeplug | None:
        if self.data is None:
            return None

        return Codeplug(self.data)

    @property
    def dirty(self) -> bool:
        if self.data is None:
            return False

        if self.saved_bytes is None:
            return True

        return self.data != self.saved_bytes

    def total_channels(self) -> int:
        cp = self.codeplug()

        if cp is None:
            return 0

        return sum(1 for ch in cp.channels if _channel_valid(ch))

    def zone_labels(self) -> list[tuple[int, str]]:
        cp = self.codeplug()

        if cp is None:
            return []

        out = []

        for i, z in enumerate(get_zones(cp)):
            zid = zone_id(z, i)
            out.append((zid, zone_name(z, zid)))

        return out

    def changed_against(self, other: Codeplug) -> int:
        cp = self.codeplug()

        if cp is None:
            return 0

        count = 0

        for i in range(1024):
            try:
                if bytes(cp.channels[i].data) != bytes(
                    other.channels[i].data
                ):
                    count += 1
            except Exception:
                pass

        return count

    def changed_channel_count(self) -> int:
        if self.data is None or self.saved_bytes is None:
            return 0

        return self.changed_against(Codeplug(self.saved_bytes))

    # ---------- undo / commit ----------

    def snapshot(self) -> None:
        if self.data is None:
            return

        self._undo.append(self.data)

        if len(self._undo) > UNDO_LIMIT:
            self._undo.pop(0)

        self._redo.clear()

    def undo(self) -> str | None:
        if not self._undo or self.data is None:
            return None

        self._redo.append(self.data)
        self._apply(self._undo.pop())
        return "Изменение отменено"

    def redo(self) -> str | None:
        if not self._redo or self.data is None:
            return None

        self._undo.append(self.data)
        self._apply(self._redo.pop())
        return "Изменение возвращено"

    def _apply(self, new_bytes: bytes) -> None:
        self.data = bytes(new_bytes)
        self.zone_map = build_channel_zone_map(Codeplug(self.data))

    def commit(self, new_bytes: bytes, message: str) -> None:
        self._apply(new_bytes)
        self.notify(message)

    def load_from_read(self, cp: Codeplug, backup: str) -> None:
        from gd77_zones import log_zone_diagnostics

        self.data = bytes(cp)
        self.saved_bytes = self.data
        self._undo.clear()
        self._redo.clear()
        self.backup_path = backup
        log_zone_diagnostics(cp)
        self._apply(self.data)

    def mark_saved(self) -> None:
        self.saved_bytes = self.data

    # ---------- журнал и уведомления ----------

    def log_event(self, text: str, error: bool = False) -> None:
        prefix = "ОШИБКА" if error else "OK"
        self._events.append(f"{datetime.now():%H:%M:%S} [{prefix}] {text}")
        _log(text)

    def notify(self, text: str, error: bool = False) -> None:
        self.log_event(text, error)
        self.pending_message = (text, error)

    def events(self) -> list[str]:
        return list(self._events)

    def take_message(self) -> tuple[str, bool] | None:
        msg = self.pending_message
        self.pending_message = None
        return msg


state = EditorState()
