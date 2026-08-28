"""Runtime paths shared by source and the packaged Windows application."""

from __future__ import annotations

import sys
from pathlib import Path


if getattr(sys, "frozen", False):
    APP_ROOT = Path(sys.executable).resolve().parent
else:
    APP_ROOT = Path(__file__).resolve().parent.parent

BACKEND_DIR = APP_ROOT / "backend"
MODEL_DIR = APP_ROOT / "models" if (APP_ROOT / "models").is_dir() else APP_ROOT

