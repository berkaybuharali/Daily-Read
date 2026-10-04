"""Report catalog, archive re-rendering and the pieces render_report embeds for navigation and auto-save."""
from __future__ import annotations

import html as htmllib
import json
import re
from dataclasses import replace
from pathlib import Path

from dailyread.catalog import build_catalog, report_files, report_stem, root_rel, write_catalog
from dailyread.config import load_config
from dailyread.library import EPOCH, clean_record, find_report_stems
from dailyread.pipeline import archive_previous, rerender_moved
from dailyread.render import render_report

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config()
EMPTY = {"version": 1, "updated_at": EPOCH, "items": {}}


def fixture_report(day: str, mode: str = "real", hhmm: str = "0912") -> dict:
    r = json.loads((ROOT / "fixtures" / "sample_report.json").read_text())
    r["generated_at"] = f"{day}T{hhmm[:2]}:{hhmm[2:]}:00+02:00"
    r["mode"] = mode
    return r


def tree(tmp_path: Path, days=("2026-10-02", "2026-10-03", "2026-10-04")):
    """reports/ (newest) + reports/archive/<year>/ (older) + data/ for each day, like a real project after a few runs."""
    reports, data = tmp_path / "reports", tmp_path / "data" / "reports"      # same layout as a real project
    cfg = replace(CFG, root=tmp_path)
    for k, day in enumerate(days):
        report = fixture_report(day)
        data.mkdir(parents=True, exist_ok=True)
        (data / f"{day}.json").write_text(json.dumps(report))
        newest = k == len(days) - 1
        f = reports / f"{day}.html" if newest else reports / "archive" / day[:4] / f"{day}.html"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(render_report(cfg, report, root_rel=root_rel(f, reports), library=EMPTY, helper=None))
    write_catalog(reports, data)
    return cfg, reports, data


def test_report_stem_is_the_file_name_the_pipeline_uses():
    assert report_stem(fixture_report("2026-10-04")) == "2026-10-04"
    assert report_stem(fixture_report("2026-10-04", mode="dry-run", hhmm="1149")) == "2026-10-04_1149"


def test_catalog_lists_every_report_newest_first_with_relative_links_and_counts(tmp_path):
    _, reports, data = tree(tmp_path)
    cat = build_catalog(reports, data)
    assert [e["id"] for e in cat] == ["2026-10-04", "2026-10-03", "2026-10-02"]
    assert [e["href"] for e in cat] == ["2026-10-04.html", "archive/2026/2026-10-03.html", "archive/2026/2026-10-02.html"]
    sample = json.loads((ROOT / "fixtures" / "sample_report.json").read_text())
    items = [i for s in sample["sections"] for i in s["items"]]
    assert cat[0]["items"] == len(items) and cat[0]["read"] == sum(1 for i in items if i.get("verdict") == "read")
    assert cat[0]["label"] == "Sun 4 Oct" and cat[0]["window"] == sample["window"]["label"]


def test_catalog_ignores_other_files_and_survives_missing_data(tmp_path):
    _, reports, data = tree(tmp_path)
    (reports / "index.html").write_text("x")
    (reports / "mockup.html").write_text("x")
    (reports / "dry-run").mkdir()
    (reports / "dry-run" / "2026-10-04_1149.html").write_text("x")
    (data / "2026-10-03.json").unlink()
    cat = {e["id"]: e for e in build_catalog(reports, data)}
    assert sorted(cat) == ["2026-10-02", "2026-10-03", "2026-10-04"]
    assert cat["2026-10-03"]["items"] is None and cat["2026-10-03"]["read"] is None      # unknown, not zero


def test_dry_run_catalog_uses_the_stamped_names(tmp_path):
    reports, data = tmp_path / "reports" / "dry-run", tmp_path / "data" / "dry-run"
    data.mkdir(parents=True)
    reports.mkdir(parents=True)
    for stem in ("2026-10-04_0900", "2026-10-04_1149"):
        (reports / f"{stem}.html").write_text("x")
        (data / f"{stem}.json").write_text(json.dumps(fixture_report("2026-10-04", "dry-run")))
    cat = build_catalog(reports, data)
    assert [e["id"] for e in cat] == ["2026-10-04_1149", "2026-10-04_0900"] and cat[0]["label"] == "Sun 4 Oct 11:49"


def test_catalog_js_is_a_script_that_cannot_break_out(tmp_path):
    _, reports, data = tree(tmp_path)
    text = (reports / "catalog.js").read_text()
    assert text.startswith("window.DR_CATALOG = [") and "<" not in text and ">" not in text
    assert list(reports.glob("*.tmp")) == [] and list(reports.glob(".catalog.js.*")) == []


def test_root_rel_matches_the_depth_of_the_file(tmp_path):
    r = tmp_path / "reports"
    assert root_rel(r / "x.html", r) == "" and root_rel(r / "archive" / "2026" / "x.html", r) == "../../"


def test_report_files_finds_current_and_archived(tmp_path):
    _, reports, _ = tree(tmp_path)
    assert [f.stem for f in report_files(reports)] == ["2026-10-04", "2026-10-03", "2026-10-02"]


# ---- archiving moves a report: its link to catalog.js must be fixed -----------------------------------------------------
def test_archiving_moves_old_reports_and_rerendering_fixes_their_catalog_path(tmp_path):
    cfg, reports, data = tree(tmp_path, days=("2026-10-02", "2026-10-03"))
    assert 'src="catalog.js"' in (reports / "2026-10-03.html").read_text()
    # a new day arrives: yesterday's report is archived (moved), its relative path to catalog.js is now wrong
    report = fixture_report("2026-10-04")
    (data / "2026-10-04.json").write_text(json.dumps(report))
    moved = archive_previous(reports, reports / "archive", keep="2026-10-04.html")
    assert [m.name for m in moved] == ["2026-10-03.html"] and (reports / "archive" / "2026" / "2026-10-03.html").exists()
    assert 'src="catalog.js"' in moved[0].read_text()                       # stale until re-rendered
    p = {"data": data, "reports": reports, "archive": reports / "archive"}
    rerender_moved(cfg, p, moved)
    html = moved[0].read_text()
    assert 'src="../../catalog.js"' in html and '"root":"../../"' in html.replace(" ", "")


def test_rerender_skips_reports_without_saved_data(tmp_path, caplog):
    cfg, reports, data = tree(tmp_path, days=("2026-10-02", "2026-10-03"))
    f = reports / "archive" / "2026" / "2026-10-02.html"
    before = f.read_text()
    (data / "2026-10-02.json").unlink()
    rerender_moved(cfg, {"data": data, "reports": reports, "archive": reports / "archive"}, [f])
    assert f.read_text() == before and "no saved data" in caplog.text


# ---- what the page embeds -------------------------------------------------------------------------------------------
def test_render_loads_catalog_only_when_told_where_it_is():
    r = fixture_report("2026-10-04")
    cfg = replace(CFG, root=Path("/nonexistent"))
    assert 'src="catalog.js"' in render_report(cfg, r, library=EMPTY, root_rel="", helper=None)
    assert 'src="../../catalog.js"' in render_report(cfg, r, library=EMPTY, root_rel="../../", helper=None)
    assert "<script src=" not in render_report(cfg, r, library=EMPTY, helper=None)       # mockup: no navigation


def test_render_embeds_report_meta_and_saved_items_remember_their_report():
    r = fixture_report("2026-10-04")
    html = render_report(replace(CFG, root=Path("/nonexistent")), r, library=EMPTY, root_rel="../../", helper=None)
    meta = json.loads(re.search(r'id="report-meta">(.*?)</script>', html, re.S).group(1))
    assert meta == {"stem": "2026-10-04", "root": "../../"}
    snaps = json.loads(re.search(r'id="report-items">(.*?)</script>', html, re.S).group(1))
    assert {s["report"] for s in snaps.values()} == {"2026-10-04"}


def test_helper_settings_reach_the_page_only_when_the_helper_is_installed(tmp_path):
    cfg = replace(CFG, root=tmp_path)
    r = fixture_report("2026-10-04")
    assert 'id="library-helper"' not in render_report(cfg, r, library=EMPTY)           # no token yet: nothing embedded
    token = tmp_path / "state" / "library.token"
    token.parent.mkdir(parents=True)
    token.write_text("secret-abc")
    html = render_report(cfg, r, library=EMPTY)
    info = json.loads(re.search(r'id="library-helper">(.*?)</script>', html, re.S).group(1))
    assert info == {"url": "http://127.0.0.1:47821", "token": "secret-abc"}


def test_old_saved_items_find_their_report_in_the_saved_data(tmp_path):
    cfg, reports, data = tree(tmp_path)
    r = fixture_report("2026-10-04")
    some_id = r["sections"][0]["items"][0]["id"]
    old = clean_record({"id": some_id, "title": "Saved before reports were remembered", "read_later": True})
    assert old["report"] == ""
    doc = {"version": 1, "updated_at": EPOCH, "items": {some_id: old}}
    html = render_report(cfg, r, library=doc, helper=None)
    seed = json.loads(re.search(r'id="library-seed">(.*?)</script>', html, re.S).group(1))
    assert seed["items"][some_id]["report"] == "2026-10-02"          # the earliest report that contained it
    assert doc["items"][some_id]["report"] == ""                      # the caller's data is not modified


def test_find_report_stems_handles_missing_and_broken_files(tmp_path):
    (tmp_path / "2026-10-01.json").write_text("{broken")
    (tmp_path / "2026-10-02.json").write_text(json.dumps({"sections": [{"items": [{"id": "x"}]}]}))
    assert find_report_stems({"x", "y"}, tmp_path) == {"x": "2026-10-02"}
    assert find_report_stems({"x"}, tmp_path / "nope") == {}


def test_pages_carry_a_content_security_policy_that_only_allows_the_helper():
    r = fixture_report("2026-10-04")
    cfg = replace(CFG, root=Path("/nonexistent"))
    plain = render_report(cfg, r, library=EMPTY, helper=None)
    with_helper = render_report(cfg, r, library=EMPTY, helper={"url": "http://127.0.0.1:47821", "token": "t"})
    policy = lambda html: htmllib.unescape(re.search(r'Content-Security-Policy" content="([^"]+)"', html).group(1))   # as Chrome reads it
    assert "connect-src 'none'" in policy(plain)
    assert "connect-src http://127.0.0.1:47821;" in policy(with_helper)
    for html in (plain, with_helper):
        p = policy(html)
        assert "default-src 'none'" in p and "unsafe-eval" not in p and "base-uri 'none'" in p
        assert html.index("Content-Security-Policy") < html.index("<script")       # must precede every script


def test_one_corrupt_data_file_never_stops_the_other_reports_from_being_rerendered(tmp_path, caplog):
    cfg, reports, data = tree(tmp_path)
    good, bad = reports / "archive" / "2026" / "2026-10-03.html", reports / "archive" / "2026" / "2026-10-02.html"
    (data / "2026-10-02.json").write_text("{truncated")
    good.write_text("OLD-CONTENT-MARKER")
    rerender_moved(cfg, {"data": data, "reports": reports, "archive": reports / "archive"}, [bad, good])
    assert "OLD-CONTENT-MARKER" not in good.read_text()          # the good one was still rendered
    assert "could not re-render 2026-10-02.html" in caplog.text


def test_report_files_are_written_owner_only(tmp_path):
    from dailyread.fsutil import write_private
    f = tmp_path / "sub" / "report.html"
    write_private(f, "<p>token inside</p>")
    assert f.read_text() == "<p>token inside</p>" and (f.stat().st_mode & 0o777) == 0o600
    write_private(f, "second")                                   # replacing keeps it private and leaves no temp files
    assert (f.stat().st_mode & 0o777) == 0o600 and [p.name for p in f.parent.iterdir()] == ["report.html"]
