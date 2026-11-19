from __future__ import annotations

import hashlib
import logging
import os
import re
import uuid
from pathlib import Path

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
    output_mode = str(payload.get("output_mode", "github")).strip().lower()
    if output_mode not in {"local", "github"}:
        raise ValueError("Choose either this PC or GitHub as the screenshot destination.")

    token = ""
    repository_url = ""
    if output_mode == "github":
        repository_url = str(payload.get("repository_url", "")).strip()
        repository = parse_repository_url(repository_url)
        token = str(payload.get("github_token", "")).strip()
        if not token:
            raise ValueError("Enter a GitHub personal access token.")
        if "\r" in token or "\n" in token:
            raise ValueError("GitHub token contains an invalid line break.")
        branch = str(payload.get("branch", DEFAULTS["branch"])).strip() or DEFAULTS["branch"]
        local_output_dir = ""
    else:
        selected_dir = str(payload.get("local_output_dir", "")).strip()
        if not selected_dir:
            raise ValueError("Choose a folder on this PC for your screenshots.")
        target = Path(selected_dir).expanduser()
        try:
            target.mkdir(parents=True, exist_ok=True)
            target = target.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"Could not access the selected screenshot folder: {exc}") from exc
        if not target.is_dir():
            raise ValueError("The selected screenshot destination is not a folder.")
        probe = target / f".sitemap-studio-{uuid.uuid4().hex}.tmp"
        try:
            probe.write_text("write-check", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            raise ValueError(f"The selected folder is not writable: {exc}") from exc
        local_output_dir = str(target)
        identity = hashlib.sha256(os.path.normcase(str(target)).casefold().encode("utf-8")).hexdigest()[:20]
        repository = f"local:{identity}"
        branch = "local"

    batch_size = int(payload.get("batch_size", DEFAULTS["batch_size"]))
    delay = float(payload.get("delay_seconds", DEFAULTS["delay_seconds"]))
    settle_ms = int(payload.get("settle_ms", DEFAULTS["settle_ms"]))
    quality = int(payload.get("screenshot_quality", DEFAULTS["screenshot_quality"]))
    max_height = int(payload.get("max_full_page_height", DEFAULTS["max_full_page_height"]))
    capture_all_urls = bool(payload.get("capture_all_urls", int(payload.get("max_urls", 0) or 0) == 0))
    max_urls = 0 if capture_all_urls else int(payload.get("max_urls", 100))

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
    if not capture_all_urls and not 1 <= max_urls <= 10_000_000:
        raise ValueError("The URL limit must be between 1 and 10,000,000, or select full sitemap capture.")

    config = {
        "output_mode": output_mode,
        "local_output_dir": local_output_dir,
        "sitemap_urls": sitemaps,
        "repository_url": repository_url,
        "repository": repository,
        "branch": branch,
        "batch_size": batch_size,
        "delay_seconds": delay,
        "settle_ms": settle_ms,
        "screenshot_quality": quality,
        "max_full_page_height": max_height,
        "capture_all_urls": capture_all_urls,
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
    def choose_local_folder():
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected = filedialog.askdirectory(
                parent=root,
                title="Choose where Sitemap Studio should save screenshots",
                mustexist=True,
            )
            root.destroy()
            return {"ok": bool(selected), "path": selected or "", "cancelled": not bool(selected)}
        except Exception as exc:
            logger.warning("Local folder picker failed: %s", exc)
            return {"ok": False, "error": f"Could not open the folder picker: {exc}"}

    @eel.expose
    def validate_connection(payload):
        try:
            config, token = _build_config(payload)
            if config["output_mode"] != "github":
                return {"ok": False, "error": "A GitHub connection test is only needed for the GitHub destination."}
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
