from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from py.capture_service import BrowserCapture
from py.database import Database
from py.github_service import GitHubUploader
from py.report_service import create_reports
from py.sitemap_service import RobotsPolicy, SitemapCrawler, build_http_session

logger = logging.getLogger(__name__)


class JobManager:
    def __init__(self, db: Database):
        self.db = db
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._events: queue.Queue[dict] = queue.Queue(maxsize=5000)
        self._active_job_id: str | None = None
        self._runtime: dict[str, Any] = {"stage": "idle", "message": "Ready", "current_url": ""}
        # A desktop process cannot continue a worker after it has been closed. Mark
        # unfinished jobs clearly; their uploaded/captured URL states remain resumable.
        last_job = self.db.get_job()
        if last_job and last_job.get("status") in {"starting", "validating", "discovering", "capturing", "running", "stopping"}:
            self.db.update_job(
                last_job["id"], status="interrupted",
                message="App closed before the scan completed. Start the same sitemap/repository again to resume pending URLs.",
                current_url="",
            )

    def _emit(self, event_type: str, message: str, **data: Any) -> None:
        event = {"type": event_type, "message": message, "timestamp": time.time(), **data}
        try:
            self._events.put_nowait(event)
        except queue.Full:
            try:
                self._events.get_nowait()
                self._events.put_nowait(event)
            except queue.Empty:
                pass
        with self._lock:
            self._runtime.update({"stage": event_type, "message": message})
            if "current_url" in data:
                self._runtime["current_url"] = data["current_url"]

    def poll_events(self, limit: int = 250) -> list[dict]:
        output = []
        for _ in range(max(1, min(limit, 1000))):
            try:
                output.append(self._events.get_nowait())
            except queue.Empty:
                break
        return output

    def status(self) -> dict:
        with self._lock:
            job_id = self._active_job_id
            runtime = dict(self._runtime)
            alive = bool(self._thread and self._thread.is_alive())
        job = self.db.get_job(job_id) if job_id else self.db.get_job()
        if not job:
            return {"running": alive, "runtime": runtime, "job": None, "metrics": self.db.metrics()}
        counts = self.db.page_counts(job["config"].get("repository"), job["config"].get("branch"), job["id"])
        return {"running": alive, "runtime": runtime, "job": job, "counts": counts, "metrics": self.db.metrics()}

    def start(self, config: dict, token: str) -> dict:
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError("A scan is already running. Stop it before starting another scan.")
            self._cancel.clear()
            job_id = str(uuid.uuid4())
            self.db.create_job(job_id, config)
            self._active_job_id = job_id
            self._runtime = {"stage": "starting", "message": "Starting scan", "current_url": ""}
            self._thread = threading.Thread(
                target=self._run_job,
                args=(job_id, dict(config), token),
                name=f"SitemapStudio-{job_id[:8]}",
                daemon=False,
            )
            self._thread.start()
            return {"job_id": job_id, "status": "starting"}

    def stop(self) -> dict:
        with self._lock:
            if not self._thread or not self._thread.is_alive():
                return {"stopped": False, "message": "No active scan."}
            self._cancel.set()
        self._emit("stopping", "Stop requested. Already committed screenshots remain on GitHub; uncommitted images will be retained locally for resume.")
        return {"stopped": True, "message": "Stop requested."}

    def _run_job(self, job_id: str, config: dict, token: str) -> None:
        uploader: GitHubUploader | None = None
        browser: BrowserCapture | None = None
        http = None
        repository = config["repository"]
        branch = config["branch"]
        batch: list[tuple[str, Path, str]] = []
        uploaded_this_run = 0
        skipped_this_run = 0
        failed_this_run = 0
        discovered_summary: dict = {}
        try:
            self.db.update_job(job_id, status="validating", message="Validating GitHub access")
            self._emit("validating", "Checking GitHub repository access and branch…")
            uploader = GitHubUploader(config["repository_url"], token, branch, cancel_event=self._cancel)
            token = ""
            if uploader.repository.lower() != repository.lower():
                raise RuntimeError("Internal repository validation mismatch.")
            self._emit("connected", f"GitHub connected: {repository} / {branch}", repository=repository, branch=branch)

            http = build_http_session(config["user_agent"])
            self.db.update_job(job_id, status="discovering", message="Reading sitemap files")
            self._emit("discovering", f"Reading {len(config['sitemap_urls'])} sitemap endpoint(s), including nested indexes…")
            crawler = SitemapCrawler(http, self._cancel)
            discovered_summary = crawler.crawl(config["sitemap_urls"])
            for row in crawler.sitemap_stats:
                self.db.save_sitemap_result(job_id, row["sitemap_url"], row["entry_count"], row["status"], row["error"])
            urls = discovered_summary["urls"]
            max_urls = int(config.get("max_urls", 0))
            if max_urls > 0:
                urls = dict(list(urls.items())[:max_urls])
            self.db.upsert_pages(job_id, repository, branch, urls)
            self.db.update_job(job_id, total_urls=len(urls), status="capturing", message="Sitemap discovery complete")
            self._emit(
                "discovered",
                f"Discovery complete: {discovered_summary['raw_url_entries']:,} raw entries, {discovered_summary['unique_urls']:,} unique URLs.",
                total_urls=len(urls), raw_entries=discovered_summary["raw_url_entries"],
                unique_urls=discovered_summary["unique_urls"], sitemaps_read=discovered_summary["sitemap_files_read"],
            )
            if crawler.errors:
                for error in crawler.errors[:20]:
                    self._emit("warning", f"Sitemap warning: {error[:350]}")
                if len(crawler.errors) > 20:
                    self._emit("warning", f"{len(crawler.errors) - 20} additional sitemap warning(s) are in the local report.")

            if self._cancel.is_set():
                raise InterruptedError("Scan stopped during sitemap discovery.")

            robots = RobotsPolicy(http, config["user_agent"], float(config["delay_seconds"]))
            browser = BrowserCapture().start()
            pending_count = self.db.count_pending_pages(job_id, repository, branch, retry_failed=bool(config.get("retry_failed")))
            pending = self.db.iter_pending_pages(job_id, repository, branch, retry_failed=bool(config.get("retry_failed")))
            self._emit("capturing", f"Preparing to capture {pending_count:,} pending page(s).", pending=pending_count)
            self.db.update_job(job_id, total_urls=len(urls), message=f"Capturing {pending_count} pending pages")

            for index, row in enumerate(pending, start=1):
                if self._cancel.is_set():
                    break
                url = row["url"]
                self.db.update_job(job_id, current_url=url, message=f"Processing {index:,} of {pending_count:,}")
                self._emit("page", f"Processing {index:,}/{pending_count:,}: {url[:180]}", current_url=url,
                           current=index, pending=pending_count)

                robots_delay = float(config["delay_seconds"])
                existing_path = Path(row["screenshot_path"]) if row.get("screenshot_path") else None
                if row["status"] == "captured" and existing_path and existing_path.exists():
                    remote = row["github_path"] or uploader.screenshot_path(url, existing_path.name)
                    batch.append((url, existing_path, remote))
                    self._emit("recovered", f"Recovered an uncommitted screenshot: {url[:150]}")
                else:
                    try:
                        permitted, robots_delay = robots.check(url)
                    except Exception as exc:
                        failed_this_run += 1
                        self.db.update_page(url, repository, branch, status="failed", error=f"robots.txt check failed: {exc}")
                        self._emit("error", f"Could not verify robots policy for {url[:150]}: {exc}", failed=failed_this_run)
                        self._update_counters(job_id, uploaded_this_run, skipped_this_run, failed_this_run)
                        continue
                    if not permitted:
                        skipped_this_run += 1
                        self.db.update_page(url, repository, branch, status="skipped_robots", error="Disallowed by robots.txt")
                        self._emit("skipped", f"Skipped (robots.txt disallows): {url[:160]}", skipped=skipped_this_run)
                        self._update_counters(job_id, uploaded_this_run, skipped_this_run, failed_this_run)
                        continue
                    try:
                        path, title, mode = browser.capture(
                            url,
                            settle_ms=int(config["settle_ms"]),
                            quality=int(config["screenshot_quality"]),
                            max_full_page_height=int(config["max_full_page_height"]),
                        )
                        remote = uploader.screenshot_path(url, path.name)
                        self.db.update_page(url, repository, branch, status="captured", screenshot_path=str(path),
                                            github_path=remote, title=title, capture_mode=mode, error=None)
                        batch.append((url, path, remote))
                        self._emit("captured", f"Screenshot captured: {title or url[:120]}", current_url=url, mode=mode)
                    except Exception as exc:
                        failed_this_run += 1
                        self.db.update_page(url, repository, branch, status="failed", screenshot_path=None,
                                            error=str(exc)[:1500])
                        self._emit("error", f"Capture failed for {url[:140]}: {str(exc)[:250]}", failed=failed_this_run)

                if len(batch) >= int(config["batch_size"]) and not self._cancel.is_set():
                    uploaded_this_run += self._commit_batch(uploader, batch, repository, branch, job_id)
                    batch.clear()
                self._update_counters(job_id, uploaded_this_run, skipped_this_run, failed_this_run)
                self._cancel.wait(max(float(config["delay_seconds"]), robots_delay))

            if batch:
                if self._cancel.is_set():
                    self._emit("warning", f"Stopped with {len(batch)} uncommitted screenshot(s); local files are retained for the next run.")
                else:
                    uploaded_this_run += self._commit_batch(uploader, batch, repository, branch, job_id)
                    batch.clear()
            if browser:
                browser.close()
                browser = None

            if self._cancel.is_set():
                final_status = "stopped"
                message = "Stopped safely. Uploaded batches are committed; uncommitted local captures are retained for resume."
                self._emit("stopped", message)
            else:
                final_status = "completed" if failed_this_run == 0 else "completed_with_errors"
                message = "Scan completed." if failed_this_run == 0 else "Scan completed with some failed pages."
                self._emit("completed", message)

            counts = self.db.page_counts(repository, branch, job_id)
            summary = {
                "job_id": job_id,
                "repository": repository,
                "branch": branch,
                "sitemap_urls_submitted": config["sitemap_urls"],
                "raw_url_entries": discovered_summary.get("raw_url_entries", 0),
                "unique_urls_discovered": discovered_summary.get("unique_urls", 0),
                "unique_urls_queued": len(urls),
                "xml_files_read": discovered_summary.get("sitemap_files_read", 0),
                "sitemap_errors": discovered_summary.get("errors", []),
                "database_status_counts": counts,
            }
            summary_path, csv_path = create_reports(self.db, job_id, summary)
            try:
                self._emit("reports", "Uploading count summary and page inventory to GitHub…")
                uploader.upload_reports(summary_path, csv_path)
                self._emit("reports_uploaded", "Reports successfully published to GitHub: reports/latest_summary.json and reports/latest_pages.csv.gz")
            except Exception as exc:
                logger.warning("Reports upload failed; local reports are retained: %s", exc)
                self._emit("warning", f"Could not publish reports to GitHub; local files are safe: {exc}")
                if final_status == "completed":
                    final_status = "completed_with_warnings"
                    message = "Scan completed, but report publishing failed. The local reports are retained."
            self.db.update_job(job_id, status=final_status, uploaded=uploaded_this_run,
                               skipped=skipped_this_run, failed=failed_this_run,
                               current_url="", message=message)
        except InterruptedError as exc:
            self.db.update_job(job_id, status="stopped", message=str(exc))
            self._emit("stopped", str(exc))
        except Exception as exc:
            if self._cancel.is_set():
                message = "Stopped. Any uncommitted local screenshots have been retained for the next run."
                self.db.update_job(job_id, status="stopped", uploaded=uploaded_this_run,
                                   skipped=skipped_this_run, failed=failed_this_run,
                                   current_url="", message=message)
                self._emit("stopped", message)
            else:
                logger.exception("Job %s failed", job_id)
                self.db.update_job(job_id, status="failed", uploaded=uploaded_this_run,
                                   skipped=skipped_this_run, failed=failed_this_run,
                                   current_url="", message=str(exc)[:1200])
                self._emit("fatal", f"Job failed: {str(exc)[:500]}")
            # Captures not committed on the target branch are intentionally kept for recovery.
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    logger.exception("Could not close browser cleanly")
            if http:
                http.close()
            if uploader:
                uploader.close()
            self.db.close_current_thread()
            with self._lock:
                self._runtime["current_url"] = ""

    def _commit_batch(self, uploader: GitHubUploader, batch: list[tuple[str, Path, str]],
                      repository: str, branch: str, job_id: str) -> int:
        files = [(path, remote) for _, path, remote in batch]
        self._emit("uploading", f"Uploading {len(files)} screenshot(s) to GitHub…", batch_size=len(files))
        commit_sha = uploader.upload_screenshot_batch(files)
        # Only delete after the branch update has been confirmed by GitHub.
        for url, path, remote in batch:
            self.db.update_page(url, repository, branch, status="uploaded", screenshot_path=None,
                                github_path=remote, error=None)
            path.unlink(missing_ok=True)
        self._emit("uploaded", f"GitHub commit confirmed ({commit_sha[:10]}). Deleted {len(files)} local screenshot(s).",
                   uploaded=len(files), commit=commit_sha)
        return len(files)

    def _update_counters(self, job_id: str, uploaded: int, skipped: int, failed: int) -> None:
        self.db.update_job(job_id, uploaded=uploaded, skipped=skipped, failed=failed)
