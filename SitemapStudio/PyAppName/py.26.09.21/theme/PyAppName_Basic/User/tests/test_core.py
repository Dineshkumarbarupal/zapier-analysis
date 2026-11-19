from __future__ import annotations

import csv
import gzip
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from py.database import Database
from py.github_service import GitHubUploader, parse_repository_url
from requests.exceptions import SSLError
from py.sitemap_service import SitemapCrawler, normalize_page_url
from py import report_service


class RepositoryUrlTests(unittest.TestCase):
    def test_https_git_url(self):
        self.assertEqual(
            parse_repository_url("https://github.com/example/project.git"),
            "example/project",
        )

    def test_owner_repo_shorthand(self):
        self.assertEqual(parse_repository_url("example/project"), "example/project")

    def test_reject_non_github_host(self):
        with self.assertRaises(ValueError):
            parse_repository_url("https://notgithub.example/a/b")


class GitHubRetryTests(unittest.TestCase):
    def test_retries_transient_ssl_failure_for_write_request(self):
        class FakeResponse:
            status_code = 201
            text = ""
            content = b"{\"sha\":\"blob-sha\"}"
            ok = True
            def json(self):
                return {"sha": "blob-sha"}

        class FakeSession:
            def __init__(self):
                self.calls = 0
            def request(self, *args, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    raise SSLError("temporary TLS EOF")
                return FakeResponse()

        uploader = GitHubUploader.__new__(GitHubUploader)
        uploader.session = FakeSession()
        uploader.cancel_event = None
        with patch("py.github_service.time.sleep"):
            result = uploader.request("POST", "https://api.github.com/test", retry_writes=True, json={})
        self.assertEqual(result["sha"], "blob-sha")
        self.assertEqual(uploader.session.calls, 2)

    def test_accepts_slash_in_branch_name(self):
        class FakeSession:
            def __init__(self):
                self.headers = {}
        with patch.object(GitHubUploader, "preflight", return_value={}):
            uploader = GitHubUploader("example/project", "fake-token", "feature/screenshots")
        self.assertEqual(uploader.branch, "feature/screenshots")


class StopDuringReportUploadTests(unittest.TestCase):
    def test_final_reports_ignore_scan_cancel_event(self):
        from threading import Event
        from unittest.mock import Mock

        uploader = GitHubUploader.__new__(GitHubUploader)
        uploader.cancel_event = Event()
        uploader.cancel_event.set()
        uploader.upload_files = Mock(return_value="report-commit-sha")

        result = uploader.upload_reports(Path("summary.json"), Path("pages.csv.gz"))

        self.assertEqual(result, "report-commit-sha")
        uploader.upload_files.assert_called_once()
        self.assertFalse(uploader.upload_files.call_args.kwargs["respect_cancel"])

    def test_screenshot_requests_still_stop_immediately(self):
        from threading import Event

        class FakeSession:
            def __init__(self):
                self.calls = 0
            def request(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError("Cancelled request must not reach the network")

        uploader = GitHubUploader.__new__(GitHubUploader)
        uploader.session = FakeSession()
        uploader.cancel_event = Event()
        uploader.cancel_event.set()

        with self.assertRaisesRegex(RuntimeError, "GitHub request cancelled"):
            uploader.request("POST", "https://api.github.com/test", retry_writes=True)

        self.assertEqual(uploader.session.calls, 0)


class UrlNormalizationTests(unittest.TestCase):
    @patch("py.sitemap_service.validate_public_url", side_effect=lambda value: value)
    def test_removes_fragment_and_normalizes_host(self, _validate):
        self.assertEqual(
            normalize_page_url("https://EXAMPLE.com/docs/page#section"),
            "https://example.com/docs/page",
        )


class DatabaseTests(unittest.TestCase):
    def test_job_and_page_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "test.sqlite3")
            db.create_job("job-1", {"repository": "example/project", "branch": "main", "github_token": "must-not-persist"})
            job = db.get_job("job-1")
            self.assertNotIn("github_token", job["config"])
            db.upsert_pages("job-1", "example/project", "main", {"https://example.com/": "https://example.com/sitemap.xml"})
            counts = db.page_counts("example/project", "main", "job-1")
            self.assertEqual(counts.get("discovered"), 1)
            db.update_page("https://example.com/", "example/project", "main", status="uploaded", github_path="screenshots/home.jpg")
            counts = db.page_counts("example/project", "main", "job-1")
            self.assertEqual(counts.get("uploaded"), 1)
            db.close_current_thread()

    def test_local_screenshots_are_counted_and_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "local.sqlite3")
            config = {
                "output_mode": "local",
                "local_output_dir": str(Path(tmp) / "screenshots"),
                "repository": "local:folder-hash",
                "branch": "local",
            }
            db.create_job("local-job-1", config)
            db.upsert_pages("local-job-1", "local:folder-hash", "local", {
                "https://example.com/home": "https://example.com/sitemap.xml"
            })
            saved_path = str(Path(tmp) / "screenshots" / "home.jpg")
            db.update_page(
                "https://example.com/home", "local:folder-hash", "local",
                status="saved_local", screenshot_path=saved_path,
                github_path=saved_path, title="Home"
            )
            self.assertEqual(db.metrics()["uploaded_screenshots"], 1)

            db.create_job("local-job-2", config)
            db.upsert_pages("local-job-2", "local:folder-hash", "local", {
                "https://example.com/home": "https://example.com/sitemap.xml"
            })
            counts = db.page_counts("local:folder-hash", "local", "local-job-2")
            self.assertEqual(counts.get("saved_local"), 1)
            self.assertEqual(db.count_pending_pages("local-job-2", "local:folder-hash", "local"), 0)
            inventory = db.inventory_rows("local-job-2", 10)
            self.assertEqual(inventory[0]["output_path"], saved_path)
            db.close_current_thread()


class SitemapParserTests(unittest.TestCase):
    class FakeResponse:
        status_code = 200
        url = "https://example.com/sitemap.xml"
        headers = {}
        is_redirect = False
        is_permanent_redirect = False
        content = b"""<?xml version='1.0' encoding='UTF-8'?>
        <urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <url><loc>https://example.com/a#one</loc></url>
          <url><loc>https://example.com/b</loc></url>
        </urlset>"""
        def raise_for_status(self):
            return None
        def iter_content(self, chunk_size=1024 * 1024):
            yield self.content
        def close(self):
            return None

    class FakeSession:
        def get(self, *args, **kwargs):
            return SitemapParserTests.FakeResponse()

    @patch("py.sitemap_service.validate_public_url", side_effect=lambda value: value)
    @patch("py.sitemap_service.normalize_page_url", side_effect=lambda value: value.split("#", 1)[0])
    def test_counts_unique_sitemap_urls(self, _normalize, _validate):
        crawler = SitemapCrawler(self.FakeSession())
        self.assertIsInstance(crawler.urls, dict)
        result = crawler.crawl(["https://example.com/sitemap.xml"])
        self.assertEqual(result["raw_url_entries"], 2)
        self.assertEqual(result["unique_urls"], 2)
        self.assertEqual(result["sitemap_files_read"], 1)


class SitemapResilienceTests(unittest.TestCase):
    class Response:
        status_code = 200
        headers = {}
        is_redirect = False
        is_permanent_redirect = False

        def __init__(self, url, content):
            self.url = url
            self.content = content

        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size=1024 * 1024):
            yield self.content

        def close(self):
            return None

    class Session:
        def __init__(self, documents):
            self.documents = documents

        def get(self, url, **kwargs):
            return SitemapResilienceTests.Response(url, self.documents[url])

    def test_empty_and_unexpected_xml_do_not_abort_remaining_sitemaps(self):
        valid = b"""<?xml version='1.0'?>
        <urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <url><loc>https://example.com/working</loc></url>
        </urlset>"""
        documents = {
            "https://example.com/empty.xml": b"  \n ",
            "https://example.com/html.xml": b"<html><body>Access denied</body></html>",
            "https://example.com/good.xml": valid,
        }
        with patch("py.sitemap_service.validate_public_url", side_effect=lambda value: value), \
             patch("py.sitemap_service.normalize_page_url", side_effect=lambda value: value):
            crawler = SitemapCrawler(self.Session(documents))
            result = crawler.crawl(list(documents))

        self.assertEqual(result["unique_urls"], 1)
        self.assertEqual(result["urls"]["https://example.com/working"], "https://example.com/good.xml")
        self.assertEqual(len(result["errors"]), 2)
        self.assertEqual([row["status"] for row in result["sitemap_stats"]], ["failed", "failed", "success"])
        self.assertIn("empty", result["sitemap_stats"][0]["error"].lower())
        self.assertIn("unexpected xml root", result["sitemap_stats"][1]["error"].lower())

    def test_discovery_progress_callback_reports_url_counts(self):
        valid = b"""<?xml version='1.0'?>
        <urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
          <url><loc>https://example.com/a</loc></url>
        </urlset>"""
        events = []
        with patch("py.sitemap_service.validate_public_url", side_effect=lambda value: value), \
             patch("py.sitemap_service.normalize_page_url", side_effect=lambda value: value):
            crawler = SitemapCrawler(
                self.Session({"https://example.com/sitemap.xml": valid}),
                progress_callback=events.append,
            )
            result = crawler.crawl(["https://example.com/sitemap.xml"])

        self.assertEqual(result["unique_urls"], 1)
        self.assertTrue(any(event.get("phase") == "finished" and event.get("complete") for event in events))
        self.assertTrue(any(event.get("unique_urls") == 1 for event in events))


class ReportExportTests(unittest.TestCase):
    def test_exports_full_csv_and_gzip_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = Database(root / "test.sqlite3")
            db.create_job("job-report", {"repository": "example/project", "branch": "main"})
            db.upsert_pages("job-report", "example/project", "main", {
                "https://example.com/a": "https://example.com/sitemap.xml",
                "https://example.com/b": "https://example.com/sitemap.xml",
            })
            report_dir = root / "reports"
            with patch.object(report_service, "REPORT_DIR", report_dir):
                summary_path, compressed_path = report_service.create_reports(
                    db, "job-report", {"unique_urls_discovered": 2}
                )
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(summary["unique_urls_in_inventory"], 2)
            with gzip.open(compressed_path, "rt", encoding="utf-8-sig") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 2)
            self.assertTrue((report_dir / "latest_pages.csv").exists())
            db.close_current_thread()


if __name__ == "__main__":
    unittest.main(verbosity=2)
