from __future__ import annotations

import logging
import re

import eel

from core.config import APP_NAME, APP_VERSION, DEFAULTS, DB_PATH
from core.security import validate_public_url
from py.database import Database
from py.github_service import parse_repository_url
from py.job_manager import JobManager

logger = logging.getLogger(__name__)
database = Database()
manager = JobManager(database)


def _normalize_sitemaps(raw) -> list[str]:
    if isinstance(raw, str):
        candidates = re.split(r"[\r\n,;]+", raw)
    elif isinstance(raw, list):
        candidates = raw
    else:
        raise ValueError("Enter one or more sitemap URLs.")
    result = []
    seen = set()
    for item in candidates:
        if not str(item).strip():
            continue
        url = validate_public_url(str(item).strip())
        if url not in seen:
            seen.add(url)
            result.append(url)
    if not result:
        raise ValueError("Enter at least one valid public sitemap URL.")
    if len(result) > 100:
        raise ValueError("You can submit up to 100 sitemap URLs in one job.")
    return result


def _build_config(payload: dict) -> tuple[dict, str]:
    if not isinstance(payload, dict):
        raise ValueError("Invalid start request.")
    sitemaps = _normalize_sitemaps(payload.get("sitemap_urls", ""))
    repository_url = str(payload.get("repository_url", "")).strip()
    repository = parse_repository_url(repository_url)
    token = str(payload.get("github_token", "")).strip()
    if not token:
        raise ValueError("Enter a GitHub personal access token.")
    if "\r" in token or "\n" in token:
        raise ValueError("GitHub token contains an invalid line break.")

    branch = str(payload.get("branch", DEFAULTS["branch"])).strip() or DEFAULTS["branch"]
    batch_size = int(payload.get("batch_size", DEFAULTS["batch_size"]))
    delay = float(payload.get("delay_seconds", DEFAULTS["delay_seconds"]))
    settle_ms = int(payload.get("settle_ms", DEFAULTS["settle_ms"]))
    quality = int(payload.get("screenshot_quality", DEFAULTS["screenshot_quality"]))
    max_height = int(payload.get("max_full_page_height", DEFAULTS["max_full_page_height"]))
    max_urls = int(payload.get("max_urls", DEFAULTS["max_urls"]))
    if not 1 <= batch_size <= 50:
        raise ValueError("Batch size must be between 1 and 50.")
    if not 0.5 <= delay <= 120:
        raise ValueError("Delay must be between 0.5 and 120 seconds.")
    if not 0 <= settle_ms <= 30000:
        raise ValueError("Page settle time must be between 0 and 30000 milliseconds.")
    if not 40 <= quality <= 90:
        raise ValueError("Screenshot quality must be between 40 and 90.")
    if not 2000 <= max_height <= 50000:
        raise ValueError("Maximum full-page height must be between 2000 and 50000 pixels.")
    if not 0 <= max_urls <= 10_000_000:
        raise ValueError("URL limit must be between 0 (unlimited) and 10,000,000.")

    config = {
        "sitemap_urls": sitemaps,
        "repository_url": repository_url,
        "repository": repository,
        "branch": branch,
        "batch_size": batch_size,
        "delay_seconds": delay,
        "settle_ms": settle_ms,
        "screenshot_quality": quality,
        "max_full_page_height": max_height,
        "max_urls": max_urls,
        "retry_failed": bool(payload.get("retry_failed", False)),
        "user_agent": DEFAULTS["user_agent"],
    }
    return config, token


def register_api() -> None:
    @eel.expose
    def get_app_info():
        return {
            "name": APP_NAME,
            "version": APP_VERSION,
            "defaults": DEFAULTS,
            "database_path": str(DB_PATH),
            "metrics": database.metrics(),
            "status": manager.status(),
        }

    @eel.expose
    def validate_connection(payload):
        try:
            config, token = _build_config(payload)
            from py.github_service import GitHubUploader
            uploader = GitHubUploader(config["repository_url"], token, config["branch"])
            result = {
                "ok": True,
                "repository": uploader.repository,
                "branch": uploader.branch,
                "message": f"Repository and branch are reachable: {uploader.repository} ({uploader.branch}). GitHub write permission will be confirmed on the first screenshot commit.",
            }
            uploader.close()
            token = ""
            return result
        except Exception as exc:
            logger.warning("Connection validation failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    @eel.expose
    def start_job(payload):
        try:
            config, token = _build_config(payload)
            result = manager.start(config, token)
            token = ""
            return {"ok": True, **result}
        except Exception as exc:
            logger.warning("Could not start job: %s", exc)
            return {"ok": False, "error": str(exc)}

    @eel.expose
    def stop_job():
        try:
            return {"ok": True, **manager.stop()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    @eel.expose
    def get_status():
        return manager.status()

    @eel.expose
    def poll_events():
        return manager.poll_events()

    @eel.expose
    def get_jobs():
        return database.list_jobs(30)

    @eel.expose
    def get_latest_inventory(limit=100):
        job = database.get_job()
        if not job:
            return {"job": None, "pages": []}
        safe_limit = max(1, min(int(limit), 500))
        return {"job": job, "pages": database.inventory_rows(job["id"], safe_limit)}

    @eel.expose
    def get_page_counts():
        return database.metrics()
