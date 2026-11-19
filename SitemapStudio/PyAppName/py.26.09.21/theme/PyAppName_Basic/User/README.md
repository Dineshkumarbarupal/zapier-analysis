# Sitemap Studio — Eel Desktop Application

A modular desktop application built with HTML, CSS, JavaScript, Python, Eel, SQLite, Playwright, and GitHub REST APIs.

## Windows setup
1. Install Python 3.10+.
2. Open PowerShell in this directory.
3. Run:
\`\`\`powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
python main.py
\`\`\`
Alternatively, double-click \`run_windows.bat\`.

## Use
Enter one or more sitemap URLs (one per line), a GitHub repository URL, branch, and a fine-grained token with Contents: Read and write scoped to that repository. The token remains in memory only and is not persisted in SQLite or logs. Test the connection, set a safe maximum URL count for a first run, then start scanning.

## Features
- Recursive sitemap index parsing, XML gzip support and URL de-duplication.
- robots.txt compliance and public-address/SSRF protections.
- Playwright Chromium screenshot capture.
- GitHub batch commits with retries and branch-update recovery.
- SQLite job history, URL state persistence and resume support.
- Delete temporary screenshots only after confirmed GitHub commits.
- CSV/GZIP inventory and JSON summary reports.

## Limitations
Only public sitemap URLs are discovered. These are not guaranteed to correspond to unique in-app screens; private dashboards, editor states, dialogs and authenticated pages absent from sitemaps are not discovered. The app does not bypass authentication, CAPTCHAs, bot challenges, robots rules or access controls. Large URL inventories can generate many screenshots and substantial GitHub storage usage.

Eel is used because it is the requested bridge. Its upstream repository is archived; review dependencies and plan a maintained migration path before commercial enterprise distribution.


## Stop button and final reports

Pressing **Stop scan** cancels sitemap/page processing and screenshot batch uploads. Any screenshot that has not received a confirmed GitHub branch commit stays on disk for recovery. Once the worker has stopped processing pages, it still attempts to publish the partial JSON summary and compressed CSV inventory. Final report publication deliberately does not inherit the scan cancellation flag; a temporary report upload failure is shown as a warning and local report files are retained. This avoids treating a normal user stop as a failed GitHub request.
