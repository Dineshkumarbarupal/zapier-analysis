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
        result = crawler.crawl(["https://example.com/sitemap.xml"])
        self.assertEqual(result["raw_url_entries"], 2)
        self.assertEqual(result["unique_urls"], 2)
        self.assertEqual(result["sitemap_files_read"], 1)


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
