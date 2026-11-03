import sys
from pathlib import Path

_VENDOR = Path(__file__).resolve().parent.parent / "vendor"

if str(_VENDOR) not in sys.path:
    sys.path.insert(0, str(_VENDOR))
