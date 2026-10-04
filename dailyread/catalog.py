"""Table of contents of every report (the prev/next buttons and the "Reports" menu in each report page).

`<reports dir>/catalog.js` is a tiny script, `window.DR_CATALOG = [...]`, newest first. Every report loads it with a
`<script src>` (works from disk, unlike fetch), so navigation always knows all reports without re-rendering them.
Only a report that MOVES (today's report is archived into archive/<year>/ the next day) is re-rendered, because its
relative path to catalog.js changes."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime
from pathlib import Path

from .fsutil import write_private
from .library import embed_json

log = logging.getLogger(__name__)
STEM = re.compile(r"^\d{4}-\d{2}-\d{2}(_\d{4})?$")


def report_stem(report: dict) -> str:
    """File stem of a report: 2026-10-04 for real runs, 2026-10-04_1149 for dry runs (as the pipeline names them)."""
    dt = datetime.fromisoformat(report["generated_at"])
    return f"{dt:%Y-%m-%d}" if report.get("mode") == "real" else f"{dt:%Y-%m-%d_%H%M}"


def report_files(reports_dir: Path) -> list[Path]:
    """Every report page: the newest ones in `reports_dir`, older ones in archive/<year>/ (real runs only)."""
    found = [f for f in reports_dir.glob("????-??-??*.html") if STEM.match(f.stem)]
    found += [f for f in (reports_dir / "archive").glob("*/????-??-??.html") if STEM.match(f.stem)]
    return sorted(found, key=lambda f: f.stem, reverse=True)


def root_rel(file: Path, reports_dir: Path) -> str:
    """Relative path from a report file back to the reports directory ("" or "../../")."""
    depth = len(file.parent.relative_to(reports_dir).parts)
    return "../" * depth


def _entry(f: Path, reports_dir: Path, data_dir: Path) -> dict:
    stem = f.stem
    day = date.fromisoformat(stem[:10])
    entry = {"id": stem, "date": stem[:10], "label": f"{day:%a %-d %b}" + (f" {stem[11:13]}:{stem[13:15]}" if "_" in stem else ""),
             "href": f.relative_to(reports_dir).as_posix(), "items": None, "read": None, "window": ""}
    data = data_dir / f"{stem}.json"
    try:
        report = json.loads(data.read_text())
        items = [i for s in report["sections"] for i in s["items"]]
        entry.update(items=len(items), read=sum(1 for i in items if i.get("verdict") == "read"),
                     window=report.get("window", {}).get("label", ""))
    except (OSError, ValueError, KeyError):
        log.info("no data file for report %s: counts unknown", stem)
    return entry


def build_catalog(reports_dir: Path, data_dir: Path) -> list[dict]:
    return [_entry(f, reports_dir, data_dir) for f in report_files(reports_dir)]


def write_catalog(reports_dir: Path, data_dir: Path) -> list[dict]:
    entries = build_catalog(reports_dir, data_dir)
    target = reports_dir / "catalog.js"
    write_private(target, "window.DR_CATALOG = " + embed_json(entries) + ";\n")
    return entries
