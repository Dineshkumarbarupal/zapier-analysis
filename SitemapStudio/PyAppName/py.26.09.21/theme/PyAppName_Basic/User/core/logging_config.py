from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler

from core.config import LOG_PATH


def configure_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if root.handlers:
        return
    formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=5_000_000, backupCount=4, encoding="utf-8")
    file_handler.setFormatter(formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
