import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
ROOT_DIR = BASE_DIR.parent

for p in (ROOT_DIR / "src", ROOT_DIR / "vendor", BASE_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()
