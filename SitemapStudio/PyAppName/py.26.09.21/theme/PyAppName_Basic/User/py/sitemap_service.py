from __future__ import annotations

import gzip
import io
import logging
import time
import xml.etree.ElementTree as ET
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.security import UnsafeURLError, validate_public_url

logger = logging.getLogger(__name__)
MAX_SITEMAP_XML_BYTES = 100 * 1024 * 1024


def build_http_session(user_agent: str) -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=5, connect=5, read=3, status=4, backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "HEAD"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": user_agent, "Accept": "application/xml,text/xml,*/*"})
    return session


def normalize_page_url(value: str) -> str | None:
    try:
        if len(value) > 8192:
            return None
        value = validate_public_url(value.strip())
        parts = urlsplit(value)
        host = parts.hostname.lower() if parts.hostname else ""
        if parts.port and (parts.scheme, parts.port) not in {("http", 80), ("https", 443)}:
            host = f"{host}:{parts.port}"
        return urlunsplit((parts.scheme.lower(), host, parts.path or "/", parts.query, ""))
    except (UnsafeURLError, ValueError):
        return None


def _xml_tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1].lower()


class SitemapCrawler:
    def __init__(self, session: requests.Session, cancel_event=None, max_sitemaps: int = 5000,
                 progress_callback=None):
        self.session = session
        self.cancel_event = cancel_event
        self.max_sitemaps = max_sitemaps
        self.progress_callback = progress_callback
        self.visited: set[str] = set()
        self.urls: dict[str, str] = {}
        self.sitemap_stats: list[dict] = []
        self.errors: list[str] = []
        self.raw_entries = 0
        self.xml_files_read = 0
        self.discovered_sitemaps = 0
        self.processed_sitemaps = 0

    def _notify_progress(self, current_sitemap: str = "", **extra) -> None:
        if not self.progress_callback:
            return
        payload = {
            "current_sitemap": current_sitemap,
            "processed_sitemaps": self.processed_sitemaps,
            "total_sitemaps": self.discovered_sitemaps,
            "raw_entries": self.raw_entries,
            "unique_urls": len(self.urls),
            **extra,
        }
        try:
            self.progress_callback(payload)
        except Exception:
            logger.exception("Sitemap progress callback failed; continuing crawl")

    def _fetch_bytes(self, url: str) -> bytes:
        current = validate_public_url(url)
        for _ in range(6):
            response = self.session.get(current, timeout=(15, 60), allow_redirects=False, stream=True)
            if response.is_redirect or response.is_permanent_redirect:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise RuntimeError("Redirect response did not include a Location header")
                from urllib.parse import urljoin
                current = validate_public_url(urljoin(current, location))
                continue
            response.raise_for_status()
            validate_public_url(response.url)
            raw = bytearray()
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    raw.extend(chunk)
                    if len(raw) > MAX_SITEMAP_XML_BYTES:
                        raise ValueError("Downloaded sitemap exceeds the 100 MB safety limit.")
            response.close()
            content = bytes(raw)
            if content[:2] == b"\x1f\x8b":
                # Bound gzip expansion to protect memory from unexpectedly large or malicious files.
                with gzip.GzipFile(fileobj=io.BytesIO(content), mode="rb") as zipped:
                    content = zipped.read(MAX_SITEMAP_XML_BYTES + 1)
                    if len(content) > MAX_SITEMAP_XML_BYTES or zipped.read(1):
                        raise ValueError("Decompressed sitemap exceeds the 100 MB safety limit.")
            if len(content) > MAX_SITEMAP_XML_BYTES:
                raise ValueError("Sitemap XML exceeds the 100 MB safety limit.")
            if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
                raise ValueError("DOCTYPE and entity declarations are not accepted in sitemap XML.")
            return content
        raise RuntimeError("Too many sitemap redirects")

    def crawl_one(self, sitemap_url: str) -> None:
        if self.cancel_event and self.cancel_event.is_set():
            return
        sitemap_url = validate_public_url(sitemap_url)
        if sitemap_url in self.visited:
            return
        if len(self.visited) >= self.max_sitemaps:
            raise RuntimeError(f"Sitemap limit reached ({self.max_sitemaps}); increase it only if expected.")
        self.visited.add(sitemap_url)
        self.discovered_sitemaps = max(self.discovered_sitemaps, len(self.visited))
        count = 0
        self._notify_progress(sitemap_url, phase="fetching")
        try:
            content = self._fetch_bytes(sitemap_url)
            self.xml_files_read += 1
            if not content or not content.strip():
                raise ValueError("Sitemap response is empty; no XML document was returned.")
            # iterparse is used so large sitemap files do not require a full XML tree in memory.
            root_tag = None
            for event, element in ET.iterparse(io.BytesIO(content), events=("start", "end")):
                if self.cancel_event and self.cancel_event.is_set():
                    break
                if event == "start" and root_tag is None:
                    root_tag = _xml_tag(element)
                    continue
                if event != "end":
                    continue
                current_tag = _xml_tag(element)
                if root_tag == "sitemapindex" and current_tag == "sitemap":
                    loc = next((node.text.strip() for node in element if _xml_tag(node) == "loc" and node.text), "")
                    if loc:
                        try:
                            child_url = validate_public_url(loc)
                            if child_url not in self.visited:
                                self.discovered_sitemaps += 1
                                self._notify_progress(sitemap_url, phase="index", next_sitemap=child_url)
                            self.crawl_one(child_url)
                        except Exception as exc:
                            self.errors.append(f"{loc}: {exc}")
                            self._notify_progress(sitemap_url, phase="warning", warning=str(exc)[:300])
                    element.clear()
                elif root_tag == "urlset" and current_tag == "url":
                    loc = next((node.text.strip() for node in element if _xml_tag(node) == "loc" and node.text), "")
                    if loc:
                        count += 1
                        self.raw_entries += 1
                        normalized = normalize_page_url(loc)
                        if normalized:
                            self.urls.setdefault(normalized, sitemap_url)
                        if count % 100 == 0:
                            self._notify_progress(sitemap_url, phase="parsing")
                    element.clear()
            if root_tag not in {"urlset", "sitemapindex"}:
                if root_tag is None:
                    detail = "empty or whitespace-only response"
                else:
                    detail = f"unexpected XML root <{root_tag}> (expected <urlset> or <sitemapindex>)"
                raise ValueError(f"Invalid sitemap document: {detail}. The endpoint may return an HTML block page or unsupported XML.")
            self.processed_sitemaps += 1
            self.sitemap_stats.append({"sitemap_url": sitemap_url, "entry_count": count, "status": "success", "error": None})
            self._notify_progress(sitemap_url, phase="completed")
            logger.info("Read sitemap %s (%s entries)", sitemap_url, count)
        except Exception as exc:
            message = str(exc)[:1000]
            self.errors.append(f"{sitemap_url}: {message}")
            self.processed_sitemaps += 1
            self.sitemap_stats.append({"sitemap_url": sitemap_url, "entry_count": count, "status": "failed", "error": message})
            self._notify_progress(sitemap_url, phase="failed", warning=message)
            logger.warning("Could not read sitemap %s: %s", sitemap_url, message)

    def crawl(self, sitemap_urls: list[str]) -> dict:
        self.discovered_sitemaps = len(set(sitemap_urls))
        self._notify_progress(phase="started")
        for sitemap_url in sitemap_urls:
            if self.cancel_event and self.cancel_event.is_set():
                break
            try:
                self.crawl_one(sitemap_url)
            except Exception as exc:
                self.errors.append(f"{sitemap_url}: {exc}")
                self._notify_progress(sitemap_url, phase="failed", warning=str(exc)[:300])
        self._notify_progress(phase="finished", complete=True)
        return {
            "unique_urls": len(self.urls),
            "raw_url_entries": self.raw_entries,
            "sitemap_files_read": self.xml_files_read,
            "sitemap_stats": self.sitemap_stats,
            "errors": self.errors,
            "urls": self.urls,
        }


class RobotsPolicy:
    def __init__(self, session: requests.Session, user_agent: str, minimum_delay: float = 1.0):
        self.session = session
        self.user_agent = user_agent
        self.minimum_delay = minimum_delay
        self.cache: dict[str, tuple[object, float]] = {}

    def _for_origin(self, url: str):
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self.cache:
            return self.cache[origin]
        robots_url = f"{origin}/robots.txt"
        parser = __import__("urllib.robotparser", fromlist=["RobotFileParser"]).RobotFileParser()
        parser.set_url(robots_url)
        try:
            response = self.session.get(robots_url, timeout=(10, 25), allow_redirects=False)
            if response.status_code in (404, 410):
                parser.parse([])
            elif response.is_redirect:
                # Do not follow robots redirects automatically; use a conservative empty rule set only for same-origin redirects.
                location = response.headers.get("Location", "")
                from urllib.parse import urljoin
                target = validate_public_url(urljoin(robots_url, location))
                if urlsplit(target).netloc != parts.netloc:
                    raise RuntimeError("robots.txt redirected to a different host; refusing to guess its policy")
                response2 = self.session.get(target, timeout=(10, 25), allow_redirects=False)
                response2.raise_for_status()
                parser.parse(response2.text.splitlines())
            else:
                response.raise_for_status()
                validate_public_url(response.url)
                parser.parse(response.text.splitlines())
        except requests.RequestException as exc:
            raise RuntimeError(f"Could not verify robots.txt for {origin}: {exc}") from exc
        except Exception as exc:
            raise RuntimeError(f"Could not verify robots.txt for {origin}: {exc}") from exc
        delay = parser.crawl_delay(self.user_agent) or parser.crawl_delay("*") or self.minimum_delay
        delay = max(self.minimum_delay, float(delay))
        self.cache[origin] = (parser, delay)
        return parser, delay

    def check(self, url: str) -> tuple[bool, float]:
        parser, delay = self._for_origin(url)
        return bool(parser.can_fetch(self.user_agent, url)), delay
