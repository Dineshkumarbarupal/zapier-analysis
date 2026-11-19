from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

import eel

USER_DIR = Path(__file__).resolve().parent
WEB_DIR = USER_DIR / "web"
if str(USER_DIR) not in sys.path:
    sys.path.insert(0, str(USER_DIR))

from core.config import APP_NAME, ensure_runtime_dirs
from core.logging_config import configure_logging
from py.api import database, manager, register_api


def prepare_web_assets() -> None:
    """Serve only web assets; never expose Python modules or local data files."""
    WEB_DIR.mkdir(parents=True, exist_ok=True)
    for folder in ("asset", "component", "css", "js", "template"):
        source = USER_DIR / folder
        destination = WEB_DIR / folder
        if destination.exists():
            shutil.rmtree(destination)
        if source.exists():
            shutil.copytree(source, destination)


def _on_window_close(route, websockets) -> None:
    logging.getLogger(__name__).info("Eel window closed: %s; remaining=%s", route, len(websockets))
    if not websockets:
        manager.stop()


def main() -> None:
    os.chdir(USER_DIR)
    ensure_runtime_dirs()
    configure_logging()
    prepare_web_assets()
    eel.init(str(WEB_DIR), allowed_extensions=[".html", ".js", ".htm", ".xhtml"])
    register_api()
    logging.getLogger(__name__).info("Starting %s", APP_NAME)
    try:
        eel.start(
            "index.html", mode="edge", host="127.0.0.1",
            size=(1500, 980), position=(40, 30), port=0,
            block=True, close_callback=_on_window_close,
        )
    finally:
        database.close_current_thread()


if __name__ == "__main__":
    main()
