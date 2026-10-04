"""HTML rendering (Jinja2, autoescaped). The model never emits HTML: summaries are escaped and only
`backtick` spans are turned into <code>."""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape
from markupsafe import Markup, escape

from .config import Config
from .catalog import report_stem
from .library import (counts, embed_json, find_report_stems, helper_config, item_snapshots, library_path, load_library,
                      seed_doc)

PRIO_LABELS = {"gcp": "Google Cloud", "anthropic": "Anthropic", "netherlands": "Netherlands", "europe": "Europe",
               "turkiye": "Türkiye", "big": "Big story"}


def codeify(text: str | None) -> Markup:
    escaped = str(escape(text or ""))
    return Markup(re.sub(r"`([^`]{1,120})`", r"<code>\1</code>", escaped))


def safe_url(url: str | None) -> str:
    """Only http(s) links reach an href (feeds could carry javascript:/data: or relative URLs)."""
    u = (url or "").strip()
    return u if urlsplit(u).scheme in ("http", "https") else "#"


def stars(n: int | None) -> Markup:
    n = max(0, min(5, int(n or 0)))
    label = f"{n} of 5 stars"
    return Markup(f'<span class="stars s{n}" role="img" aria-label="{label}" title="{label}">'
                  f'{"★" * n}<span class="off">{"★" * (5 - n)}</span></span>')


def dayfmt(value) -> str:
    d = date.fromisoformat(value) if isinstance(value, str) else value
    return f"{d:%a %-d %b}"


def thousands(n) -> str:
    return f"{int(n or 0):,}".replace(",", " ")


@lru_cache(maxsize=4)
def _env(templates_dir: str) -> Environment:
    env = Environment(loader=FileSystemLoader(templates_dir), autoescape=select_autoescape(["html", "j2"]),
                      trim_blocks=True, lstrip_blocks=True)
    env.filters.update(codeify=codeify, dayfmt=dayfmt, thousands=thousands, safe_url=safe_url)
    return env


def _assets(cfg: Config) -> dict:
    t = cfg.templates_dir
    return {"fonts_css": (t / "vendor" / "fonts.css").read_text(), "app_css": (t / "report.css").read_text(),
            # "</" would end the inline <script> early
            "app_js": "\n".join((t / n).read_text() for n in ("report.js", "library.js", "nav.js")).replace("</", "<\\/")}


def empty_message(section: dict) -> str:
    kind = "releases" if section.get("is_release_notes") else "stories" if section.get("is_it_news") else "posts"
    st = section["stats"]
    if st.get("status") not in ("ok", "empty"):
        return f"Could not fetch this source ({st.get('error') or st.get('status')}). Will retry next run."
    last = st.get("latest_item_date") or st.get("newest_item_in_feed")
    return f"No new {kind} in this window" + (f" · last one: {dayfmt(last)}" if last else "") + "."


def order_sections(sections: list[dict]) -> list[dict]:
    """Config order, except sections with no items that day move to the end (stable)."""
    return sorted(sections, key=lambda s: not s["items"])


_AUTO = object()


def render_report(cfg: Config, report: dict, index_href: str = "index.html", library: dict | None = None,
                  root_rel: str | None = None, helper: dict | None | object = _AUTO) -> str:
    """`library`: the saved Read Later / Favorites document (default: state/library.json).
    `root_rel`: relative path from this file to the reports directory ("" or "../../"); when given, the page loads
    catalog.js for the prev/next buttons and the Reports menu. `helper`: how the page reaches the auto-save helper
    (default: from the token in state/, which only `schedule install` creates; pass None for "no helper")."""
    library = library if library is not None else load_library(library_path(cfg))
    helper = helper_config(cfg) if helper is _AUTO else helper
    stem = report_stem(report)
    missing = {rid for rid, r in library["items"].items() if not r["report"]}
    if missing:      # saved before items remembered their report: find it in the saved report data
        found = find_report_stems(missing, cfg.root / "data" / "reports")
        library = {**library, "items": {rid: ({**r, "report": found[rid]} if rid in found and not r["report"] else r)
                                        for rid, r in library["items"].items()}}
    shorts = {s.key: s.short for s in cfg.sections}
    sections = order_sections(report["sections"])
    report = {**report, "sections": sections}
    total_items = sum(len(s["items"]) for s in sections)
    total_read = sum(1 for s in sections for i in s["items"] if i.get("verdict") == "read")
    days = {i["day"] for s in sections for i in s["items"]}
    by_model = report.get("usage", {}).get("by_model", {})
    usage_line = " · ".join(
        f"{m}: {u['calls']} calls, {thousands(u['input_tokens'] + u['cache_read_tokens'] + u['cache_creation_tokens'])} in / {thousands(u['output_tokens'])} out"
        for m, u in by_model.items()) or "no model calls"
    return _env(str(cfg.templates_dir)).get_template("report.html.j2").render(
        report=report, gen=datetime.fromisoformat(report["generated_at"]),
        total_items=total_items, total_read=total_read,
        active_sections=sum(1 for s in sections if s["items"]),
        icons={s.key: s.icon for s in cfg.sections},
        shorts=shorts, lib_counts=counts(library),
        items_json=embed_json(item_snapshots(sections, shorts, stem)), seed_json=embed_json(seed_doc(library)),
        report_stem=stem, root_rel=root_rel, helper_json=embed_json(helper) if helper else None,
        csp_connect=helper["url"] if helper else None,
        item_index={i["id"]: {"title": i.get("title") or i.get("gen_title") or "item",
                              "icon": next((c.icon for c in cfg.sections if c.key == s["key"]), "•")}
                    for s in sections for i in s["items"]},
        index_href=index_href, stars=stars,
        show_dates=len(days) > 1, prio_labels=PRIO_LABELS, empty_message=empty_message, usage_line=usage_line,
        **_assets(cfg),
    )


def _model_bucket(by_model: dict, name: str) -> dict:
    u = by_model.get(name, {})
    return {"calls": u.get("calls", 0),
            "inp": u.get("input_tokens", 0) + u.get("cache_read_tokens", 0) + u.get("cache_creation_tokens", 0),
            "out": u.get("output_tokens", 0)}


def render_index(cfg: Config, reports_dir: Path, store, dry_run: bool) -> str:
    runs = list(reversed(store.runs()))
    by_date = {}
    for r in runs:
        if r.get("ok"):
            by_date.setdefault(r["report_date"], r)
    reports = []
    files = sorted(reports_dir.glob("*.html"), reverse=True) + sorted((reports_dir / "archive").glob("*/*.html"), reverse=True)
    for f in files:
        if f.name in ("index.html", "mockup.html"):
            continue
        info = by_date.get(f.stem[:10]) if not dry_run else None
        data = None
        if dry_run:
            data_file = cfg.root / "data" / "dry-run" / f"{f.stem}.json"
            if data_file.exists():
                d = json.loads(data_file.read_text())
                data = {"window": d["window"]["label"], "items": sum(len(s["items"]) for s in d["sections"]),
                        "read": sum(1 for s in d["sections"] for i in s["items"] if i.get("verdict") == "read")}
        reports.append({
            "href": str(f.relative_to(reports_dir)), "name": f.stem,
            "window": (data or {}).get("window") or (
                f"{info['window_start'][:16].replace('T', ' ')} → {info['window_end'][:16].replace('T', ' ')}" if info else None),
            "items": (data or {}).get("items", info["totals"]["items"] if info else None),
            "read": (data or {}).get("read", info["totals"]["read"] if info else None),
        })
    run_rows = []
    for r in runs[:120]:
        bm = r.get("usage", {}).get("by_model", {})
        run_rows.append({
            "started": r.get("started_at", "")[:16].replace("T", " "), "ok": r.get("ok"),
            "window_days": r.get("window_days"), "items": r.get("totals", {}).get("items", 0),
            "read": r.get("totals", {}).get("read", 0),
            "models": {m: _model_bucket(bm, m) for m in bm},
            "cost": sum(u.get("cost_usd", 0) for u in bm.values()), "duration": r.get("duration_s", 0),
        })
    models = sorted({m for r in run_rows for m in r["models"]})
    src = store.sources()
    sources = [{"title": s.title, **src.get(s.key, {})} for s in cfg.sections]
    return _env(str(cfg.templates_dir)).get_template("index.html.j2").render(
        reports=reports, runs=run_rows, models=models, sources=sources, dry_run=dry_run, **_assets(cfg))
