from __future__ import annotations

from pathlib import Path

APP_NAME = "Sitemap Studio"
APP_VERSION = "1.1.0"
USER_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = USER_DIR / "data"
DB_DIR = USER_DIR / "db"
TEMP_SCREENSHOT_DIR = DATA_DIR / "screenshots_tmp"
REPORT_DIR = DATA_DIR / "reports"
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DB_DIR / "sitemap_studio.sqlite3"
LOG_PATH = LOG_DIR / "app.log"

DEFAULT_USER_AGENT = "SitemapStudio/1.0 (+https://github.com/Dineshkumarbarupal/zapier-analysis)"
DEFAULT_VIEWPORT = {"width": 1440, "height": 960}
DEFAULTS = {
    "branch": "main",
    "batch_size": 10,
    "delay_seconds": 1.0,
    "settle_ms": 1200,
    "screenshot_quality": 75,
    "max_full_page_height": 14000,
    "capture_all_urls": True,
    "max_urls": 0,
    "user_agent": DEFAULT_USER_AGENT,
}


def ensure_runtime_dirs() -> None:
    for directory in (DATA_DIR, DB_DIR, TEMP_SCREENSHOT_DIR, REPORT_DIR, LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)


ensure_runtime_dirs()
