from __future__ import annotations

import csv
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from core.config import DB_PATH, ensure_runtime_dirs


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path=DB_PATH):
        ensure_runtime_dirs()
        self.path = str(path)
        self._thread_local = threading.local()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    @contextmanager
    def session(self):
        # Reuse one SQLite connection per calling thread to avoid opening a connection
        # for every page transition during very large scans.
        connection = getattr(self._thread_local, "connection", None)
        if connection is None:
            connection = self.connect()
            self._thread_local.connection = connection
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def close_current_thread(self) -> None:
        connection = getattr(self._thread_local, "connection", None)
        if connection is not None:
            connection.close()
            self._thread_local.connection = None

    def initialize(self) -> None:
        with self.session() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    total_urls INTEGER NOT NULL DEFAULT 0,
                    uploaded INTEGER NOT NULL DEFAULT 0,
                    skipped INTEGER NOT NULL DEFAULT 0,
                    failed INTEGER NOT NULL DEFAULT 0,
                    current_url TEXT,
                    message TEXT
                );
                CREATE TABLE IF NOT EXISTS pages (
                    url TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    branch TEXT NOT NULL,
                    source_sitemap TEXT,
                    status TEXT NOT NULL DEFAULT 'discovered',
                    screenshot_path TEXT,
                    github_path TEXT,
                    title TEXT,
                    capture_mode TEXT,
                    error TEXT,
                    job_id TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (url, repository, branch),
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_pages_status ON pages(status);
                CREATE INDEX IF NOT EXISTS idx_pages_job ON pages(job_id);
                CREATE TABLE IF NOT EXISTS sitemap_sources (
                    job_id TEXT NOT NULL,
                    sitemap_url TEXT NOT NULL,
                    entry_count INTEGER NOT NULL DEFAULT 0,
                    fetched_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error TEXT,
                    PRIMARY KEY(job_id, sitemap_url),
                    FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS app_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )

    def create_job(self, job_id: str, config: dict[str, Any]) -> None:
        safe_config = {key: value for key, value in config.items()
                       if key.lower() not in {"token", "github_token", "access_token", "password", "secret"}}
        now = utc_now()
        with self.session() as db:
            db.execute(
                "INSERT INTO jobs(id,created_at,updated_at,status,config_json,message) VALUES(?,?,?,?,?,?)",
                (job_id, now, now, "starting", json.dumps(safe_config), "Starting job"),
            )

    def update_job(self, job_id: str, **values: Any) -> None:
        permitted = {"status", "total_urls", "uploaded", "skipped", "failed", "current_url", "message"}
        updates = {key: value for key, value in values.items() if key in permitted}
        if not updates:
            return
        updates["updated_at"] = utc_now()
        sql = ",".join(f"{key}=?" for key in updates)
        with self.session() as db:
            db.execute(f"UPDATE jobs SET {sql} WHERE id=?", (*updates.values(), job_id))

    def get_job(self, job_id: str | None = None) -> dict[str, Any] | None:
        with self.session() as db:
            if job_id:
                row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            else:
                row = db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 1").fetchone()
            if not row:
                return None
            result = dict(row)
            result["config"] = json.loads(result.pop("config_json"))
            return result

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.session() as db:
            rows = db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                item["config"] = json.loads(item.pop("config_json"))
                results.append(item)
            return results

    def upsert_pages(self, job_id: str, repository: str, branch: str, urls: dict[str, str]) -> None:
        now = utc_now()
        with self.session() as db:
            for url, source in urls.items():
                db.execute(
                    """INSERT INTO pages(url,repository,branch,source_sitemap,status,job_id,updated_at)
                       VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(url,repository,branch) DO UPDATE SET
                         source_sitemap=excluded.source_sitemap,
                         job_id=excluded.job_id,
                         updated_at=excluded.updated_at,
                         status=CASE WHEN pages.status='uploaded' THEN 'uploaded' WHEN pages.status='captured' THEN 'captured' ELSE 'discovered' END""",
                    (url, repository, branch, source, "discovered", job_id, now),
                )

    def save_sitemap_result(self, job_id: str, sitemap_url: str, count: int, status: str, error: str | None = None) -> None:
        with self.session() as db:
            db.execute(
                """INSERT INTO sitemap_sources(job_id,sitemap_url,entry_count,fetched_at,status,error)
                   VALUES(?,?,?,?,?,?) ON CONFLICT(job_id,sitemap_url) DO UPDATE SET
                   entry_count=excluded.entry_count,fetched_at=excluded.fetched_at,status=excluded.status,error=excluded.error""",
                (job_id, sitemap_url, count, utc_now(), status, error),
            )

    def iter_pending_pages(self, job_id: str, repository: str, branch: str, retry_failed: bool = False, batch_size: int = 500):
        """Yield pending rows in short keyset-paginated reads to avoid long WAL snapshots."""
        statuses = ("discovered", "failed", "captured") if retry_failed else ("discovered", "captured")
        marks = ",".join("?" for _ in statuses)
        last_url = ""
        safe_batch = max(1, min(batch_size, 5000))
        while True:
            with self.session() as db:
                rows = db.execute(
                    f"SELECT * FROM pages WHERE job_id=? AND repository=? AND branch=? AND status IN ({marks}) AND url>? ORDER BY url LIMIT ?",
                    (job_id, repository, branch, *statuses, last_url, safe_batch),
                ).fetchall()
            if not rows:
                break
            last_url = rows[-1]["url"]
            for row in rows:
                yield dict(row)

    def count_pending_pages(self, job_id: str, repository: str, branch: str, retry_failed: bool = False) -> int:
        statuses = ("discovered", "failed", "captured") if retry_failed else ("discovered", "captured")
        marks = ",".join("?" for _ in statuses)
        with self.session() as db:
            return db.execute(
                f"SELECT COUNT(*) AS n FROM pages WHERE job_id=? AND repository=? AND branch=? AND status IN ({marks})",
                (job_id, repository, branch, *statuses),
            ).fetchone()["n"]

    def pending_pages(self, job_id: str, repository: str, branch: str, retry_failed: bool = False) -> list[dict[str, Any]]:
        statuses = ("discovered", "failed", "captured") if retry_failed else ("discovered", "captured")
        marks = ",".join("?" for _ in statuses)
        with self.session() as db:
            rows = db.execute(
                f"SELECT * FROM pages WHERE job_id=? AND repository=? AND branch=? AND status IN ({marks}) ORDER BY url",
                (job_id, repository, branch, *statuses),
            ).fetchall()
            return [dict(row) for row in rows]

    def update_page(self, url: str, repository: str, branch: str, **values: Any) -> None:
        permitted = {"status", "screenshot_path", "github_path", "title", "capture_mode", "error", "job_id"}
        updates = {key: value for key, value in values.items() if key in permitted}
        if not updates:
            return
        updates["updated_at"] = utc_now()
        assignments = ",".join(f"{key}=?" for key in updates)
        with self.session() as db:
            db.execute(
                f"UPDATE pages SET {assignments} WHERE url=? AND repository=? AND branch=?",
                (*updates.values(), url, repository, branch),
            )

    def page_counts(self, repository: str | None = None, branch: str | None = None, job_id: str | None = None) -> dict[str, int]:
        query = "SELECT status,COUNT(*) AS total FROM pages"
        params: list[Any] = []
        conditions = []
        if repository:
            conditions.append("repository=?")
            params.append(repository)
        if branch:
            conditions.append("branch=?")
            params.append(branch)
        if job_id:
            conditions.append("job_id=?")
            params.append(job_id)
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " GROUP BY status"
        with self.session() as db:
            rows = db.execute(query, params).fetchall()
            return {row["status"]: row["total"] for row in rows}

    def sitemap_rows(self, job_id: str) -> list[dict[str, Any]]:
        with self.session() as db:
            rows = db.execute("SELECT * FROM sitemap_sources WHERE job_id=? ORDER BY sitemap_url", (job_id,)).fetchall()
            return [dict(row) for row in rows]

    def inventory_rows(self, job_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.session() as db:
            rows = db.execute(
                "SELECT url,source_sitemap,status,github_path,title,capture_mode,error FROM pages WHERE job_id=? ORDER BY url LIMIT ?",
                (job_id, limit),
            ).fetchall()
            return [dict(row) for row in rows]

    def export_inventory_csv(self, job_id: str, path: str) -> tuple[int, dict[str, int]]:
        fields = ["url", "source_sitemap", "status", "github_path", "title", "capture_mode", "error"]
        status_counts: dict[str, int] = {}
        total = 0
        with self.session() as db:
            cursor = db.execute(
                "SELECT url,source_sitemap,status,github_path,title,capture_mode,error FROM pages WHERE job_id=? ORDER BY url",
                (job_id,),
            )
            with open(path, "w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                while True:
                    rows = cursor.fetchmany(2000)
                    if not rows:
                        break
                    for row in rows:
                        item = dict(row)
                        writer.writerow(item)
                        total += 1
                        status = item["status"]
                        status_counts[status] = status_counts.get(status, 0) + 1
        return total, status_counts

    def metrics(self) -> dict[str, int]:
        with self.session() as db:
            jobs = db.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"]
            pages = db.execute("SELECT COUNT(*) AS n FROM pages").fetchone()["n"]
            uploaded = db.execute("SELECT COUNT(*) AS n FROM pages WHERE status='uploaded'").fetchone()["n"]
            return {"jobs": jobs, "tracked_pages": pages, "uploaded_screenshots": uploaded}
