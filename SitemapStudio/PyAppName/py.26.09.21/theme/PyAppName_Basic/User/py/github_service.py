from __future__ import annotations

import base64
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

import requests
from requests.exceptions import ConnectionError, Timeout, SSLError

logger = logging.getLogger(__name__)
API_ROOT = "https://api.github.com"
TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


def parse_repository_url(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValueError("Enter a GitHub repository URL.")
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        owner, repo = value.split("/", 1)
        return f"{owner}/{repo.removesuffix('.git')}"
    parsed = urlparse(value)
    if parsed.scheme not in {"https"} or parsed.hostname not in {"github.com", "www.github.com"}:
        raise ValueError("Repository must be an https://github.com/owner/repository URL.")
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise ValueError("Repository URL must point to /owner/repository.")
    owner, repo = parts
    repo = repo.removesuffix(".git")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", owner) or not re.fullmatch(r"[A-Za-z0-9_.-]+", repo):
        raise ValueError("Repository URL contains invalid owner or repository characters.")
    return f"{owner}/{repo}"


class GitHubUploader:
    def __init__(self, repository_url: str, token: str, branch: str = "main", cancel_event=None):
        self.repository = parse_repository_url(repository_url)
        if not token:
            raise ValueError("GitHub token is empty or invalid.")
        self.token = token.strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in self.token):
            raise ValueError("GitHub token contains invalid control characters. Copy it again from GitHub.")
        self.branch = branch.strip() or "main"
        self.cancel_event = cancel_event
        invalid_ref = (
            not self.branch
            or self.branch.startswith("/")
            or self.branch.endswith(("/", "."))
            or ".." in self.branch
            or "//" in self.branch
            or "@{" in self.branch
            or any(char in self.branch for char in " ~^:?*[\\")
            or any(ord(char) < 32 or ord(char) == 127 for char in self.branch)
            or any(part.startswith(".") or part.endswith(".lock") for part in self.branch.split("/"))
        )
        if invalid_ref:
            raise ValueError("Branch name is invalid. Use a valid Git branch such as main or feature/screenshots.")
        self.api = f"{API_ROOT}/repos/{self.repository}"
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "Sitemap-Studio/1.0",
        })
        self.head_sha = ""
        self.base_tree_sha = ""
        self.preflight()

    def request(self, method: str, url: str, *, expected_status=None, retry_writes: bool = False, **kwargs):
        last_error = None
        attempts = 5 if retry_writes or method.upper() == "GET" else 1
        for attempt in range(attempts):
            if self.cancel_event and self.cancel_event.is_set():
                raise RuntimeError("GitHub request cancelled. Uncommitted local screenshots have been retained.")
            try:
                response = self.session.request(method, url, timeout=(20, 120), **kwargs)
            except (ConnectionError, Timeout, SSLError) as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    time.sleep(min(2 ** attempt, 20))
                    continue
                raise RuntimeError(f"GitHub connection failed after {attempts} attempt(s): {exc}") from exc
            rate_limit_message = "rate limit" in response.text.lower()
            primary_rate_limited = response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0"
            secondary_rate_limited = response.status_code in (403, 429) and rate_limit_message
            if (response.status_code in TRANSIENT_STATUS or primary_rate_limited or secondary_rate_limited) and attempt + 1 < attempts:
                retry_after = response.headers.get("Retry-After")
                reset_at = response.headers.get("X-RateLimit-Reset")
                if retry_after and retry_after.isdigit():
                    wait = int(retry_after)
                elif primary_rate_limited and reset_at and reset_at.isdigit():
                    wait = max(1, int(reset_at) - int(time.time()) + 2)
                else:
                    wait = min(2 ** (attempt + 4), 60) if secondary_rate_limited else min(2 ** attempt, 20)
                logger.warning("GitHub rate/temporary limit (%s). Retrying in %s seconds.", response.status_code, wait)
                if self.cancel_event:
                    if self.cancel_event.wait(wait):
                        raise RuntimeError("GitHub retry cancelled. Local screenshots are retained.")
                else:
                    time.sleep(wait)
                continue
            if expected_status and response.status_code not in expected_status:
                self._raise_api_error(response)
            if not response.ok:
                self._raise_api_error(response)
            return response.json() if response.content else {}
        raise RuntimeError(f"GitHub request failed: {last_error}")

    @staticmethod
    def _raise_api_error(response: requests.Response):
        try:
            detail = response.json()
        except ValueError:
            detail = response.text[:1000]
        status = response.status_code
        hint = ""
        if status == 401:
            hint = " Check that the token is current and copied correctly."
        elif status == 403:
            hint = " Ensure the token has Contents: Read and write and access to this repository."
        elif status == 404:
            hint = " Verify repository, branch, and token repository access."
        raise RuntimeError(f"GitHub API error {status}: {detail}.{hint}")

    def preflight(self) -> dict:
        repo_info = self.request("GET", self.api)
        if repo_info.get("archived"):
            raise RuntimeError("The target GitHub repository is archived and cannot accept uploads.")
        try:
            ref = self.request("GET", f"{self.api}/git/ref/heads/{quote(self.branch, safe='')}")
        except RuntimeError as exc:
            raise RuntimeError(f"Cannot read branch '{self.branch}' or token lacks repository access: {exc}") from exc
        self.head_sha = ref["object"]["sha"]
        commit = self.request("GET", f"{self.api}/git/commits/{self.head_sha}")
        self.base_tree_sha = commit["tree"]["sha"]
        return repo_info

    def _publish_tree(self, entries: list[dict], message: str) -> str:
        tree = self.request("POST", f"{self.api}/git/trees", retry_writes=True,
                            json={"base_tree": self.base_tree_sha, "tree": entries})
        commit = self.request("POST", f"{self.api}/git/commits", retry_writes=True,
                              json={"message": message, "tree": tree["sha"], "parents": [self.head_sha]})
        new_sha = commit["sha"]
        try:
            self.request("PATCH", f"{self.api}/git/refs/heads/{quote(self.branch, safe='')}",
                         json={"sha": new_sha, "force": False})
        except Exception as exc:
            # A timed-out PATCH may have succeeded server-side. Read the ref before retrying.
            current = self.request("GET", f"{self.api}/git/ref/heads/{quote(self.branch, safe='')}")
            if current.get("object", {}).get("sha") != new_sha:
                raise RuntimeError(f"Could not confirm GitHub branch update: {exc}") from exc
        self.head_sha = new_sha
        self.base_tree_sha = tree["sha"]
        return new_sha

    def upload_files(self, files: list[tuple[Path, str]], message: str) -> str:
        entries = []
        for local_path, remote_path in files:
            content = local_path.read_bytes()
            if len(content) > 20 * 1024 * 1024:
                raise RuntimeError(f"File exceeds safe upload limit (20 MB): {local_path.name}")
            encoded = base64.b64encode(content).decode("ascii")
            blob = self.request("POST", f"{self.api}/git/blobs", retry_writes=True,
                                json={"content": encoded, "encoding": "base64"})
            entries.append({"path": remote_path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        if not entries:
            return self.head_sha
        return self._publish_tree(entries, message)

    def screenshot_path(self, page_url: str, filename: str) -> str:
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        return f"screenshots/{month}/{filename}"

    def upload_screenshot_batch(self, files: list[tuple[Path, str]]) -> str:
        return self.upload_files(files, f"Add {len(files)} sitemap page screenshot(s)")

    def upload_reports(self, summary_path: Path, csv_path: Path) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return self.upload_files(
            [(summary_path, "reports/latest_summary.json"), (csv_path, "reports/latest_pages.csv.gz")],
            f"Publish sitemap scan report ({stamp})",
        )

    def close(self) -> None:
        self.session.headers.pop("Authorization", None)
        self.session.close()
        self.token = ""
