from __future__ import annotations

import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

from core.config import REPORT_DIR
from py.database import Database


def create_reports(db: Database, job_id: str, summary: dict) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    output = dict(summary)
    output["generated_at_utc"] = datetime.now(timezone.utc).isoformat()
    csv_path = REPORT_DIR / "latest_pages.csv"
    total, status_counts = db.export_inventory_csv(job_id, str(csv_path))
    output["unique_urls_in_inventory"] = total
    output["status_counts"] = status_counts
    output["sitemap_results"] = db.sitemap_rows(job_id)
    compressed_csv = REPORT_DIR / "latest_pages.csv.gz"
    with csv_path.open("rb") as source, gzip.open(compressed_csv, "wb", compresslevel=6) as destination:
        while chunk := source.read(1024 * 1024):
            destination.write(chunk)
    summary_path = REPORT_DIR / "latest_summary.json"
    summary_path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary_path, compressed_csv
