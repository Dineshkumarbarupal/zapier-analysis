# Zapier Sitemap Screenshot Research

A Python crawler that reads the 10 supplied Zapier sitemap endpoints (including nested sitemap indexes), counts unique public page URLs, checks `robots.txt`, captures screenshots with Playwright, and uploads screenshots to the GitHub repository in small batches.

Repository: <https://github.com/Dineshkumarbarupal/zapier-analysis>

## What it does

1. Recursively reads sitemap indexes and `urlset` files from the supplied Zapier sitemap URLs.
2. Counts raw URL entries, deduplicated public Zapier URLs, and entries per sitemap.
3. Reads `https://zapier.com/robots.txt` and skips URLs disallowed for this crawler. It does not bypass login walls, bot challenges, or robots restrictions.
4. Opens each permitted URL in a headless Chromium browser and captures a JPEG screenshot. It scrolls to trigger lazy-loaded content unless `--no-lazy-scroll` is supplied.
5. Uploads screenshots to `screenshots/YYYY-MM/` in GitHub in batches. It uses the Git Data API to make one commit per batch instead of one commit per image.
6. Deletes local screenshot files only after the GitHub branch update succeeds. If the upload fails or the run is interrupted, unuploaded images remain under `screenshots_tmp/` for recovery.
7. Saves resumable progress in SQLite and creates `reports/latest_summary.json` and `reports/pages.csv` locally.

> **Important:** A sitemap URL is a public website URL, not necessarily a distinct logged-in app screen. This tool measures sitemap pages and screenshots publicly accessible pages only. It cannot discover private dashboard pages, editor states, dialogs, or account-specific screens that are absent from the sitemaps.

## Requirements

- Windows 10/11, macOS, or Linux
- Python 3.10+
- A GitHub fine-grained personal access token with **Contents: Read and write** permission, scoped to `Dineshkumarbarupal/zapier-analysis`
- Internet access to Zapier and GitHub

## Windows PowerShell setup

```powershell
cd zapier-analysis
py -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
```

Set the token for the current PowerShell session. Do not paste it into the source code or commit it:

```powershell
$env:GITHUB_TOKEN = "github_pat_your_token_here"
```

## Run

First, test a small batch:

```powershell
python zapier_sitemap_capture.py --max-pages 5 --batch-size 2
```

Then process all discovered URLs:

```powershell
python zapier_sitemap_capture.py
```

Useful options:

```powershell
python zapier_sitemap_capture.py --max-pages 20
python zapier_sitemap_capture.py --batch-size 1
python zapier_sitemap_capture.py --delay 2.0
python zapier_sitemap_capture.py --retry-failed
python zapier_sitemap_capture.py --no-lazy-scroll
```

`--max-pages 0` (the default) means all pending discovered URLs. The default batch size is 10 and the default delay is at least one second between page captures, or the larger crawl delay declared in `robots.txt`.

## Output

- GitHub screenshots: `screenshots/YYYY-MM/<page-slug>-<url-hash>.jpg`
- Local count summary: `reports/latest_summary.json`
- Local page inventory: `reports/pages.csv`
- Local resume database: `data/progress.sqlite3`
- Temporary images waiting for upload: `screenshots_tmp/`

`data/`, `reports/`, and temporary screenshots are excluded from Git by `.gitignore`, so local run state and credentials are not committed. GitHub screenshot files remain in the remote repository after local deletion; the remote repository will therefore grow as more pages are captured.

## Resume and storage behaviour

Run the same command again after an interruption. The SQLite database skips pages already uploaded. A screenshot captured but not yet uploaded is re-used if its temporary file still exists. If an upload fails, local files are deliberately retained rather than deleted. Use `--retry-failed` to retry pages that previously failed during browser capture.

For the smallest local disk footprint, use `--batch-size 1`. This leaves at most one normal pending screenshot on disk at a time, though a failed upload is retained for recovery.

## GitHub token safety

Never commit a real token. `.env` files, local database files, reports, and temporary screenshots are ignored by Git. Set `GITHUB_TOKEN` as an environment variable. Revoke the token in GitHub settings if it is ever exposed.

## Limitations

- Public sitemap pages only; no login, private pages, or authenticated app states.
- Screenshots are subject to the site's current robots rules and access controls.
- Very tall pages above `--max-page-height` are captured as viewport-only screenshots to avoid oversized images.
- GitHub API rate limits and repository storage limits apply. A large sitemap may take multiple runs; the local SQLite database supports resuming.
- This project does not claim the sitemap count equals the total number of Zapier product screens.
