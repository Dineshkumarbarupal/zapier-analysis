from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import Browser, Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError, sync_playwright

from core.config import DEFAULT_VIEWPORT, TEMP_SCREENSHOT_DIR
from core.security import is_safe_browser_request

logger = logging.getLogger(__name__)


def image_name(url: str) -> str:
    parts = urlsplit(url)
    raw = (parts.hostname or "page") + parts.path
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", raw).strip("-").lower()[:64] or "page"
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"{slug}-{digest}.jpg"


class BrowserCapture:
    def __init__(self, viewport: dict | None = None, output_dir: Path | str | None = None):
        self.viewport = viewport or DEFAULT_VIEWPORT
        self.output_dir = Path(output_dir).expanduser().resolve() if output_dir else TEMP_SCREENSHOT_DIR
        self._playwright = None
        self.browser: Browser | None = None

    def start(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(headless=True)
        return self

    def close(self):
        if self.browser:
            self.browser.close()
            self.browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None

    def capture(self, url: str, settle_ms: int = 1200, quality: int = 75,
                max_full_page_height: int = 14000, full_page: bool = True) -> tuple[Path, str, str]:
        if not self.browser:
            raise RuntimeError("BrowserCapture has not been started")
        self.output_dir.mkdir(parents=True, exist_ok=True)
        target = self.output_dir / image_name(url)
        context = self.browser.new_context(
            viewport=self.viewport,
            device_scale_factor=1,
            locale="en-US",
            ignore_https_errors=False,
        )
        page = context.new_page()
        page.set_default_navigation_timeout(45000)
        page.set_default_timeout(15000)

        def guard(route):
            if is_safe_browser_request(route.request.url):
                route.continue_()
            else:
                route.abort("blockedbyclient")

        page.route("**/*", guard)
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(settle_ms)
            final_url = page.url
            if not is_safe_browser_request(final_url):
                raise RuntimeError("Page redirected to a private or unsafe destination; navigation was blocked.")

            height = int(page.evaluate("() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"))
            if full_page:
                bounded_height = min(height, max_full_page_height)
                for y in range(0, bounded_height, 1000):
                    page.evaluate("y => window.scrollTo(0, y)", y)
                    page.wait_for_timeout(80)
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(150)
            title = page.title()[:300]
            mode = "full_page" if full_page and height <= max_full_page_height else "viewport_only_height_limit"
            try:
                page.screenshot(path=str(target), type="jpeg", quality=quality,
                                full_page=(mode == "full_page"), animations="disabled", timeout=60000)
            except PlaywrightError:
                if mode != "full_page":
                    raise
                mode = "viewport_only_capture_fallback"
                page.screenshot(path=str(target), type="jpeg", quality=quality,
                                full_page=False, animations="disabled", timeout=60000)
            if response and response.status >= 400:
                logger.warning("Page returned HTTP %s: %s", response.status, url)
            return target, title, mode
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            target.unlink(missing_ok=True)
            raise RuntimeError(f"Browser capture failed: {exc}") from exc
        finally:
            context.close()
