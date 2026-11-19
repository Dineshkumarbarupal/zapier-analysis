# Sitemap Studio — Eel Desktop Application

A modular Windows desktop app built with HTML, CSS, JavaScript, Python, Eel, SQLite and Playwright.

## Windows setup

1. Install Python 3.10+.
2. Open PowerShell in this directory.
3. Create an environment and install dependencies:

    py -3 -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install --upgrade pip
    pip install -r requirements.txt
    python -m playwright install chromium
    python main.py

Alternatively, double-click run_windows.bat.

## Capture workflow

1. Paste one or more public sitemap URLs, one per line.
2. Choose Full sitemap to capture every unique URL discovered across nested sitemap indexes, or choose Limit URL count for a smaller run.
3. Choose a screenshot destination:
   - This PC: browse to a folder. Screenshots are saved directly there. No GitHub repository URL, account or token is required.
   - GitHub repository: provide a repository URL, branch and fine-grained token with Contents: Read and write scoped to that repository.
4. Set the page delay and optional advanced screenshot settings, then start capture.

For a full sitemap run, the app displays a confirmation because some sitemaps contain hundreds of thousands of URLs. Large local runs require sufficient disk space. Large GitHub runs also consume API quota and repository storage.

## Features

- Recursive sitemap-index parsing, XML gzip support and URL de-duplication.
- Full sitemap capture or an explicit URL-count limit.
- Local screenshot output to a user-selected folder without GitHub credentials.
- Optional GitHub batch commits with retry and branch-update recovery.
- Playwright Chromium screenshot capture with bounded full-page height.
- robots.txt compliance and public-address / SSRF protections.
- SQLite job history, page inventory and recovery state.
- Local CSV/GZIP inventory and JSON summary reports.
- Stop control; saved local screenshots remain in place and uncommitted GitHub screenshots are retained for recovery.

## Output details

Local screenshots are JPEG files named using a normalized page URL and a short URL hash. The app's JSON summary and compressed CSV inventory are written under the local app data directory (data/reports). GitHub mode also publishes those reports to reports/ in the selected repository when the API commit succeeds.

## Limitations and responsible use

Only public sitemap URLs are discovered. Sitemap entries are not guaranteed to map one-to-one to unique application screens. Private dashboards, editor states, dialogs and authenticated pages absent from sitemaps are not discovered. The app does not bypass authentication, CAPTCHAs, bot challenges, robots rules, rate limits or other access controls.

Eel is used because this app explicitly requires it, but its upstream repository is archived. Review this dependency and plan a maintained migration path before commercial enterprise distribution.

## Stop button and final reports

Pressing Stop capture stops sitemap/page processing at the next safe point. Local screenshots already written to the chosen folder remain available. In GitHub mode, confirmed commits remain on GitHub and uncommitted local screenshots are retained for recovery. Once page processing stops, the worker still attempts to publish the partial JSON summary and compressed CSV inventory in GitHub mode; local report files are retained if that publish fails.