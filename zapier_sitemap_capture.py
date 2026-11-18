#!/usr/bin/env python3
"""Count public Zapier sitemap URLs, screenshot permitted pages, and upload images to GitHub."""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.robotparser
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

import requests
from playwright.sync_api import Browser, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

ROOT = Path(__file__).resolve().parent
DATA_DIR, REPORT_DIR, TEMP_DIR = ROOT / "data", ROOT / "reports", ROOT / "screenshots_tmp"
DB_PATH = DATA_DIR / "progress.sqlite3"
REPOSITORY, BRANCH = "Dineshkumarbarupal/zapier-analysis", "main"
API = f"https://api.github.com/repos/{REPOSITORY}"
USER_AGENT = "ZapierPublicSitemapResearch/1.0 (+https://github.com/Dineshkumarbarupal/zapier-analysis)"
ALLOWED_HOSTS = {"zapier.com"}

SITEMAPS = [
    "https://zapier.com/uag6x9zaeydnbfy75xsggcsb37h8cgzf-two-service-sitemap.xml",
    "https://zapier.com/uag6x9zaeydnbfy75xsggcsb37h8cgzf-one-service-sitemap.xml",
    "https://zapier.com/sitemaps/zap-templates/LZB2XiWzbJFggRYdnRGwcoAgEkTnHHEh",
    "https://zapier.com/blog/sitemap.xml",
    "https://zapier.com/uag6x9zaeydnbfy75xsggcsb37h8cgzf-sitemap.xml",
    "https://zapier.com/uag6x9zaeydnbfy75xsggcsb37h8cgzf-static-sitemap.xml",
    "https://zapier.com/agents/sitemap.xml",
    "https://zapier.com/automation/sitemap.xml",
    "https://zapier.com/marketing/sitemap.xml",
    "https://zapier.com/automations/sitemap-automations.xml",
]


@dataclass
class Capture:
    url: str
    image: Path
    remote_path: str


def allowed_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return any(host == item or host.endswith("." + item) for item in ALLOWED_HOSTS)


def normalize_url(url: str) -> str | None:
    try:
        parts = urlsplit(url.strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or not allowed_host(url):
            return None
        port = parts.port
        netloc = parts.hostname.lower()
        if port and (parts.scheme.lower(), port) not in {("http", 80), ("https", 443)}:
            netloc += f":{port}"
        return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))
    except (ValueError, UnicodeError):
        return None


def tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower()


class SitemapCrawler:
    def __init__(self, session: requests.Session):
        self.session = session
        self.visited: set[str] = set()
        self.urls: dict[str, str] = {}
        self.per_sitemap: dict[str, int] = {}
        self.errors: list[str] = []
        self.raw_entries = 0
        self.files_read = 0

    def visit(self, sitemap_url: str) -> None:
        if sitemap_url in self.visited:
            return
        self.visited.add(sitemap_url)
        try:
            response = self.session.get(sitemap_url, timeout=40)
            response.raise_for_status()
            if not allowed_host(response.url):
                raise ValueError(f"Sitemap redirected outside zapier.com: {response.url}")
            root = ET.fromstring(response.content)
            self.files_read += 1
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            self.errors.append(f"{sitemap_url}: {exc}")
            logging.warning("Sitemap unavailable: %s | %s", sitemap_url, exc)
            return

        kind = tag(root)
        if kind == "sitemapindex":
            for child in root:
                if tag(child) == "sitemap":
                    loc = next((n.text.strip() for n in child if tag(n) == "loc" and n.text), "")
                    if loc and allowed_host(loc):
                        self.visit(loc)
            return
        if kind != "urlset":
            self.errors.append(f"Unknown XML root <{kind}>: {sitemap_url}")
            return

        count = 0
        for entry in root:
            if tag(entry) != "url":
                continue
            loc = next((n.text.strip() for n in entry if tag(n) == "loc" and n.text), "")
            if not loc:
                continue
            count += 1
            self.raw_entries += 1
            normalized = normalize_url(loc)
            if normalized:
                self.urls.setdefault(normalized, sitemap_url)
        self.per_sitemap[sitemap_url] = count
        logging.info("Parsed %s (%s entries)", sitemap_url, count)

    def crawl(self) -> None:
        for sitemap in SITEMAPS:
            self.visit(sitemap)


class RobotsPolicy:
    def __init__(self, session: requests.Session):
        url = "https://zapier.com/robots.txt"
        response = session.get(url, timeout=30)
        response.raise_for_status()
        if not allowed_host(response.url):
            raise RuntimeError("robots.txt redirected off zapier.com; refusing to crawl")
        self.parser = urllib.robotparser.RobotFileParser()
        self.parser.set_url(url)
        self.parser.parse(response.text.splitlines())
        self.delay = self.parser.crawl_delay(USER_AGENT) or self.parser.crawl_delay("*")
        logging.info("robots.txt loaded; crawl-delay=%s", self.delay or "unspecified")

    def permits(self, url: str) -> bool:
        return self.parser.can_fetch(USER_AGENT, url)


def db_open() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    db.execute("""CREATE TABLE IF NOT EXISTS pages(
        url TEXT PRIMARY KEY, source TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'discovered',
        image_path TEXT, github_path TEXT, title TEXT, mode TEXT, error TEXT, updated TEXT NOT NULL
    )""")
    db.commit()
    return db


def discovered(db: sqlite3.Connection, crawler: SitemapCrawler) -> None:
    now = datetime.now(timezone.utc).isoformat()
    db.executemany(
        """INSERT INTO pages(url,source,status,updated) VALUES(?,?,'discovered',?)
        ON CONFLICT(url) DO UPDATE SET source=excluded.source, updated=excluded.updated""",
        [(url, source, now) for url, source in crawler.urls.items()],
    )
    db.commit()


def set_page(db: sqlite3.Connection, url: str, **values: str | None) -> None:
    allowed = {"status", "image_path", "github_path", "title", "mode", "error"}
    if not values or not set(values).issubset(allowed):
        raise ValueError("Invalid database update")
    assignments = [f"{name}=?" for name in values]
    params = list(values.values()) + [datetime.now(timezone.utc).isoformat(), url]
    assignments.append("updated=?")
    db.execute(f"UPDATE pages SET {','.join(assignments)} WHERE url=?", params)
    db.commit()


def filename(url: str) -> str:
    parts = urlsplit(url)
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (parts.netloc + parts.path).strip("/")).strip("-").lower()[:65] or "page"
    digest = hashlib.sha256(url.encode()).hexdigest()[:12]
    return f"{slug}-{digest}.jpg"


def github_path(url: str) -> str:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    return f"screenshots/{month}/{filename(url)}"


def capture_page(browser: Browser, url: str, target: Path, settle_ms: int, max_height: int, lazy_scroll: bool) -> tuple[str, str]:
    context = browser.new_context(
        viewport={"width": 1440, "height": 960}, device_scale_factor=1,
        locale="en-US", user_agent=USER_AGENT,
    )
    page = context.new_page()
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(settle_ms)
        if not allowed_host(page.url):
            raise RuntimeError(f"Page redirected outside zapier.com: {page.url}")
        title = page.title()[:300]

        if lazy_scroll:
            height = page.evaluate("() => Math.min(document.documentElement.scrollHeight || document.body.scrollHeight || 0, 16000)")
            for y in range(0, int(height), 900):
                page.evaluate("y => window.scrollTo(0, y)", y)
                page.wait_for_timeout(100)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(200)

        page_height = page.evaluate("() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
        mode = "full_page" if int(page_height) <= max_height else "viewport_only_height_limit"
        page.screenshot(path=str(target), type="jpeg", quality=78, full_page=(mode == "full_page"), animations="disabled", timeout=60000)
        if response and response.status >= 400:
            logging.warning("HTTP %s for %s", response.status, url)
        return title, mode
    finally:
        context.close()


class GitHubUploader:
    """Create image blobs and one Git commit per batch using the Git Data API."""
    def __init__(self, token: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": USER_AGENT,
        })
        self.head, self.tree = self.read_head()

    def request(self, method: str, url: str, **kwargs):
        response = self.session.request(method, url, timeout=120, **kwargs)
        if response.status_code in (403, 429) and response.headers.get("X-RateLimit-Remaining") == "0":
            reset = int(response.headers.get("X-RateLimit-Reset", str(int(time.time()) + 60)))
            time.sleep(max(1, reset - int(time.time()) + 2))
            response = self.session.request(method, url, timeout=120, **kwargs)
        if not response.ok:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text[:1000]
            raise RuntimeError(f"GitHub API {method} failed ({response.status_code}): {detail}")
        return response.json() if response.content else {}

    def read_head(self) -> tuple[str, str]:
        ref = self.request("GET", f"{API}/git/ref/heads/{BRANCH}")
        head = ref["object"]["sha"]
        commit = self.request("GET", f"{API}/git/commits/{head}")
        return head, commit["tree"]["sha"]

    def upload_batch(self, batch: list[Capture]) -> None:
        elements = []
        for item in batch:
            raw = base64.b64encode(item.image.read_bytes()).decode("ascii")
            blob = self.request("POST", f"{API}/git/blobs", json={"content": raw, "encoding": "base64"})
            elements.append({"path": item.remote_path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
            logging.info("Uploaded blob %s (%s bytes)", item.remote_path, item.image.stat().st_size)

        tree = self.request("POST", f"{API}/git/trees", json={"base_tree": self.tree, "tree": elements})
        commit = self.request("POST", f"{API}/git/commits", json={
            "message": f"Add {len(batch)} Zapier page screenshots",
            "tree": tree["sha"], "parents": [self.head],
        })
        self.request("PATCH", f"{API}/git/refs/heads/{BRANCH}", json={"sha": commit["sha"], "force": False})
        self.head, self.tree = commit["sha"], tree["sha"]


def reports(db: sqlite3.Connection, crawler: SitemapCrawler) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    rows = db.execute("SELECT * FROM pages ORDER BY url").fetchall()
    states: dict[str, int] = {}
    for row in rows:
        states[row["status"]] = states.get(row["status"], 0) + 1
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository": f"https://github.com/{REPOSITORY}",
        "sitemap_files_read": crawler.files_read,
        "raw_url_entries": crawler.raw_entries,
        "unique_urls": len(rows),
        "status_counts": states,
        "per_sitemap_entries": crawler.per_sitemap,
        "sitemap_errors": crawler.errors,
        "note": "Sitemap URLs are not necessarily unique logged-in product screens. robots.txt-disallowed URLs are not captured.",
    }
    (REPORT_DIR / "latest_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with (REPORT_DIR / "pages.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.writer(stream)
        writer.writerow(["url", "source", "status", "github_path", "title", "mode", "error"])
        writer.writerows([[r["url"], r["source"], r["status"], r["github_path"], r["title"], r["mode"], r["error"]] for r in rows])


def main() -> int:
    parser = argparse.ArgumentParser(description="Count Zapier sitemap URLs and capture permitted public pages.")
    parser.add_argument("--max-pages", type=int, default=0, help="Limit pages for a test; 0 means all pending pages.")
    parser.add_argument("--batch-size", type=int, default=10, help="Screenshots per GitHub commit.")
    parser.add_argument("--delay", type=float, default=1.0, help="Minimum delay between pages in seconds.")
    parser.add_argument("--settle-ms", type=int, default=1200, help="Wait after DOM content loaded.")
    parser.add_argument("--max-page-height", type=int, default=16000, help="Tall pages above this height are viewport-only.")
    parser.add_argument("--no-lazy-scroll", action="store_true", help="Disable scroll-based lazy loading.")
    parser.add_argument("--retry-failed", action="store_true", help="Retry previously failed page captures.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

    if args.max_pages < 0 or args.batch_size < 1 or args.delay < 0:
        parser.error("max-pages and delay must be non-negative; batch-size must be at least 1")
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        logging.error("Set GITHUB_TOKEN (fine-grained token: Contents read/write for %s).", REPOSITORY)
        return 2

    for folder in (DATA_DIR, REPORT_DIR, TEMP_DIR):
        folder.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml,*/*"})

    crawler = SitemapCrawler(session)
    logging.info("Reading ten sitemap endpoints and any nested sitemap indexes...")
    crawler.crawl()
    logging.info("Sitemap files=%s | raw entries=%s | unique Zapier URLs=%s",
                 crawler.files_read, crawler.raw_entries, len(crawler.urls))

    db = db_open()
    discovered(db, crawler)
    try:
        robots = RobotsPolicy(session)
    except Exception:
        logging.exception("Unable to verify robots.txt; refusing to capture pages.")
        reports(db, crawler)
        db.close()
        return 3
    try:
        uploader = GitHubUploader(token)
    except Exception:
        logging.exception("Could not access GitHub branch %s.", BRANCH)
        reports(db, crawler)
        db.close()
        return 4

    # Recover captured files from an interrupted run first.
    pending = db.execute("SELECT url,status,image_path,github_path FROM pages WHERE status!='uploaded' ORDER BY url").fetchall()
    recovered: list[Capture] = []
    for row in pending:
        path = Path(row["image_path"]) if row["image_path"] else None
        if row["status"] == "captured" and path and path.exists():
            recovered.append(Capture(row["url"], path, row["github_path"] or github_path(row["url"])))
            if len(recovered) >= args.batch_size:
                try:
                    uploader.upload_batch(recovered)
                    for item in recovered:
                        set_page(db, item.url, status="uploaded", image_path=None, github_path=item.remote_path, error=None)
                        item.image.unlink(missing_ok=True)
                    logging.info("Recovered batch committed; local images removed.")
                    recovered.clear()
                except Exception:
                    logging.exception("Recovered batch upload failed; local files retained.")
                    reports(db, crawler)
                    db.close()
                    return 5

    # Carry a smaller recovery batch into normal capture; it will be uploaded at the end.
    batch = recovered
    rows = db.execute("SELECT url,status,image_path,github_path FROM pages ORDER BY url").fetchall()
    work = [r for r in rows if r["status"] != "uploaded"]
    if args.max_pages:
        work = work[:args.max_pages]

    done, skipped, failed = 0, 0, 0
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                for row in work:
                    url, status = row["url"], row["status"]
                    existing = Path(row["image_path"]) if row["image_path"] else None
                    if status == "captured" and existing and existing.exists():
                        continue
                    if status in {"failed", "robots_disallowed"} and not args.retry_failed:
                        continue
                    if not robots.permits(url):
                        set_page(db, url, status="robots_disallowed", error="Disallowed by robots.txt")
                        skipped += 1
                        continue

                    image = TEMP_DIR / filename(url)
                    try:
                        title, mode = capture_page(browser, url, image, args.settle_ms,
                                                   args.max_page_height, not args.no_lazy_scroll)
                        destination = github_path(url)
                        set_page(db, url, status="captured", image_path=str(image),
                                 github_path=destination, title=title, mode=mode, error=None)
                        batch.append(Capture(url, image, destination))
                        done += 1
                        logging.info("Captured %s | %s | %s", url, mode, title)
                    except (PlaywrightError, PlaywrightTimeoutError, RuntimeError, OSError) as exc:
                        image.unlink(missing_ok=True)
                        set_page(db, url, status="failed", image_path=None, error=str(exc)[:1200])
                        failed += 1
                        logging.warning("Capture failed: %s | %s", url, exc)

                    if len(batch) >= args.batch_size:
                        try:
                            uploader.upload_batch(batch)
                            for item in batch:
                                set_page(db, item.url, status="uploaded", image_path=None,
                                         github_path=item.remote_path, error=None)
                                item.image.unlink(missing_ok=True)
                            logging.info("GitHub commit confirmed; deleted %s temporary screenshot(s).", len(batch))
                            batch.clear()
                        except Exception:
                            logging.exception("Upload failed. Local screenshot files retained for retry.")
                            reports(db, crawler)
                            db.close()
                            return 5

                    time.sleep(max(args.delay, float(robots.delay or 0)))
            finally:
                browser.close()

        if batch:
            uploader.upload_batch(batch)
            for item in batch:
                set_page(db, item.url, status="uploaded", image_path=None,
                         github_path=item.remote_path, error=None)
                item.image.unlink(missing_ok=True)
            logging.info("Final commit confirmed; deleted %s temporary screenshot(s).", len(batch))
            batch.clear()
    except KeyboardInterrupt:
        logging.warning("Interrupted. Images not uploaded yet are retained for the next run.")
    except Exception:
        logging.exception("Run failed unexpectedly.")
        reports(db, crawler)
        db.close()
        return 6

    reports(db, crawler)
    counts = {r["status"]: r["n"] for r in db.execute(
        "SELECT status,COUNT(*) AS n FROM pages GROUP BY status")}
    logging.info("UNIQUE URLS=%s | UPLOADED=%s | ROBOTS-DISALLOWED=%s | FAILED=%s",
                 len(crawler.urls), counts.get("uploaded", 0),
                 counts.get("robots_disallowed", 0), counts.get("failed", 0))
    logging.info("Reports: reports/latest_summary.json and reports/pages.csv")
    logging.info("Screenshots: https://github.com/%s/tree/%s/screenshots", REPOSITORY, BRANCH)
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
