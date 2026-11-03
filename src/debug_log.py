from datetime import datetime
from pathlib import Path

DEBUG_LOG = Path("md9600_debug.log")


def _log(msg: str) -> None:
    try:
        with DEBUG_LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except Exception:
        pass
