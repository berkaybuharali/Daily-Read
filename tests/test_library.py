"""Read Later / Favorites: file loading, snapshots, and what ends up in the rendered page (no browser, no LLM)."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from dailyread.config import load_config
from dailyread.library import EPOCH, backup_library, clean_record, counts, embed_json, item_snapshots, load_library, seed_doc
from dailyread.render import render_report

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config()
EMPTY = {"version": 1, "updated_at": EPOCH, "items": {}}


def rec(**kw):
    base = {"id": "a1", "title": "A title", "url": "https://example.com/a", "source": "Blog", "published": "2026-10-02",
            "stars": 4, "verdict": "read", "summary": "Short.", "read_later": True}
    return {**base, **kw}


def sample_report():
    return json.loads((ROOT / "fixtures" / "sample_report.json").read_text())


# ---- clean_record: the file is untrusted input ------------------------------------------------------------------
def test_clean_record_keeps_a_valid_record():
    r = clean_record(rec(favorite=True, read=True, added_at="2026-10-03T08:00:00.000Z", updated_at="2026-10-03T09:00:00Z"))
    assert r["id"] == "a1" and r["read_later"] and r["favorite"] and r["read"]
    assert r["updated_at"] == "2026-10-03T09:00:00Z" and r["stars"] == 4 and r["verdict"] == "read"


@pytest.mark.parametrize("bad", [None, "x", [], {}, {"id": "a"}, {"title": "t"}, {"id": "", "title": "t"}, {"id": 5, "title": "t"}])
def test_clean_record_rejects_unusable_records(bad):
    assert clean_record(bad) is None


def test_clean_record_neutralises_hostile_values():
    r = clean_record(rec(url="javascript:alert(1)", stars=99, verdict="<b>", published="yesterday", read_later="yes",
                         added_at="not a date", title="  <img src=x onerror=alert(1)>  \n x "))
    assert r["url"] == "" and r["stars"] == 0 and r["verdict"] == "" and r["published"] == ""
    assert r["read_later"] is False                      # only a real `true` counts
    assert r["added_at"] == EPOCH
    assert r["title"] == "<img src=x onerror=alert(1)> x"   # kept as text; rendering never treats it as HTML


def test_clean_record_limits_lengths_and_rejects_bool_stars():
    r = clean_record(rec(title="t" * 1000, summary="s" * 5000, stars=True))
    assert len(r["title"]) == 300 and len(r["summary"]) == 800 and r["stars"] == 0


# ---- load_library -----------------------------------------------------------------------------------------------
def test_missing_file_is_an_empty_library(tmp_path):
    assert load_library(tmp_path / "nope.json") == EMPTY


@pytest.mark.parametrize("content", ["", "{not json", "[]", '"x"', '{"items": []}', '{"items": 5}', "null"])
def test_damaged_file_is_an_empty_library(tmp_path, content):
    f = tmp_path / "library.json"
    f.write_text(content)
    assert load_library(f)["items"] == {}


def test_load_library_drops_bad_records_and_keeps_good_ones(tmp_path):
    f = tmp_path / "library.json"
    f.write_text(json.dumps({"version": 1, "items": {"a1": rec(), "bad": {"id": "bad"}, "b2": rec(id="b2", favorite=True)}}))
    doc = load_library(f)
    assert sorted(doc["items"]) == ["a1", "b2"]
    assert counts(doc) == {"later": 2, "favorites": 1}


def test_tombstones_do_not_count(tmp_path):
    f = tmp_path / "library.json"
    f.write_text(json.dumps({"items": {"a1": rec(read_later=False, favorite=False)}}))
    assert counts(load_library(f)) == {"later": 0, "favorites": 0}


# ---- snapshots of the report rows -------------------------------------------------------------------------------
def test_item_snapshots_use_writer_name_then_short_name_then_title():
    sections = [
        {"key": "independent_writers", "title": "Independent writers", "items": [
            {"id": "w1", "title": "Post", "url": "https://x.dev/p", "day": "2026-10-01", "stars": 5, "verdict": "read",
             "summary": "Uses `code` here.", "reason": "why", "extra": {"writer": "Marc Brooker"}}]},
        {"key": "bigquery_rn", "title": "BigQuery release notes", "items": [
            {"id": "b1", "title": None, "gen_title": "Generated title", "url": "https://x.dev/b", "day": "2026-10-02",
             "stars": None, "verdict": "skip", "summary": "s", "extra": {}}]},
        {"key": "other", "title": "Other section", "items": [
            {"id": "o1", "title": "T", "url": "javascript:alert(1)", "day": "2026-10-02", "extra": {}}]},
    ]
    snaps = item_snapshots(sections, {"bigquery_rn": "BigQuery"})
    assert snaps["w1"]["source"] == "Marc Brooker" and snaps["w1"]["summary"] == "Uses code here."
    assert snaps["b1"]["source"] == "BigQuery" and snaps["b1"]["title"] == "Generated title" and snaps["b1"]["stars"] == 2
    assert snaps["o1"]["source"] == "Other section" and snaps["o1"]["url"] == ""


def test_embed_json_cannot_break_out_of_a_script_tag():
    out = embed_json({"t": "</script><script>alert(1)</script> <!-- &  "})
    assert "<" not in out and ">" not in out and "&" not in out and " " not in out
    assert json.loads(out)["t"].startswith("</script>")      # still the same data


# ---- rendered page ----------------------------------------------------------------------------------------------
def test_render_has_tabs_buttons_and_snapshots_for_every_row():
    report = sample_report()
    html = render_report(CFG, report, library=EMPTY)
    rows = sum(len(s["items"]) for s in report["sections"])
    assert 'role="tablist"' in html and 'id="panel-later"' in html and 'id="panel-favorites"' in html
    assert len(re.findall(r'<button class="act"[^>]*data-act="later"', html)) == rows
    assert len(re.findall(r'<button class="act"[^>]*data-act="fav"', html)) == rows
    snaps = json.loads(re.search(r'id="report-items">(.*?)</script>', html, re.S).group(1))
    assert len(snaps) == rows
    first = next(iter(snaps.values()))
    assert {"id", "title", "url", "source", "published", "stars", "summary"} <= set(first)


def test_render_bakes_in_saved_lists_and_counts():
    doc = {"version": 1, "updated_at": EPOCH, "items": {
        "a1": clean_record(rec()), "b2": clean_record(rec(id="b2", read_later=False, favorite=True))}}
    html = render_report(CFG, sample_report(), library=doc)
    assert re.search(r'id="n-later">1<', html) and re.search(r'id="n-favorites">1<', html)
    seed = json.loads(re.search(r'id="library-seed">(.*?)</script>', html, re.S).group(1))
    assert sorted(seed["items"]) == ["a1", "b2"]


def test_hostile_library_cannot_inject_markup():
    evil = clean_record(rec(title="</script><script>window.pwned=1</script>", summary="<img src=x onerror=window.pwned=1>"))
    doc = {"version": 1, "updated_at": EPOCH, "items": {"a1": evil}}
    html = render_report(CFG, sample_report(), library=doc)
    assert "<script>window.pwned" not in html and "<img src=x" not in html
    blob = re.search(r'id="library-seed">(.*?)</script>', html, re.S).group(1)
    assert json.loads(blob)["items"]["a1"]["title"].startswith("</script>")


def test_render_reads_state_library_file_by_default(tmp_path, monkeypatch):
    f = tmp_path / "library.json"
    f.write_text(json.dumps({"items": {"a1": rec()}}))
    monkeypatch.setattr("dailyread.render.library_path", lambda cfg: f)
    assert re.search(r'id="n-later">1<', render_report(CFG, sample_report()))


def test_library_file_is_git_ignored():
    if subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=ROOT, capture_output=True).returncode != 0:
        pytest.skip("not a git checkout (a downloaded ZIP)")
    out = subprocess.run(["git", "check-ignore", "state/library.json"], cwd=ROOT, capture_output=True, text=True)
    assert out.returncode == 0, "state/library.json must never be committed"


# ---- the browser logic (templates/library.js) is plain JS; run its pure functions with node -------------------------
@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_library_js_logic_with_node():
    out = subprocess.run(["node", "--test", str(ROOT / "tests" / "library.test.js")], capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr


# ---- daily backup next to the state files ---------------------------------------------------------------------------
def test_backup_copies_the_library_and_keeps_the_newest_copies(tmp_path):
    f = tmp_path / "state" / "library.json"
    f.parent.mkdir()
    f.write_text(json.dumps({"items": {"a1": rec()}}))
    for day in range(1, 21):
        assert backup_library(f, f"2026-09-{day:02d}", keep=14).name == f"library-2026-09-{day:02d}.json"
    kept = sorted(p.name for p in (f.parent / "library-backups").glob("*.json"))
    assert len(kept) == 14 and kept[0] == "library-2026-09-07.json" and kept[-1] == "library-2026-09-20.json"
    assert json.loads((f.parent / "library-backups" / kept[-1]).read_text())["items"]["a1"]["title"] == "A title"


def test_backup_skips_missing_or_damaged_files(tmp_path):
    f = tmp_path / "library.json"
    assert backup_library(f, "2026-10-04") is None
    f.write_text("{broken")
    assert backup_library(f, "2026-10-04") is None
    assert not (tmp_path / "library-backups").exists()


def test_reports_only_bake_in_saved_items_not_removal_records():
    doc = {"version": 1, "updated_at": EPOCH, "items": {
        "kept": clean_record(rec(id="kept")),
        "gone": clean_record(rec(id="gone", read_later=False, favorite=False))}}
    assert sorted(seed_doc(doc)["items"]) == ["kept"] and sorted(doc["items"]) == ["gone", "kept"]


def test_clean_record_matches_the_browser_on_the_edge_cases_found_in_review():
    assert clean_record(rec(stars=3.0))["stars"] == 3 and clean_record(rec(stars=3.5))["stars"] == 0
    assert clean_record(rec(updated_at="0000-00-00T99:99:99Z"))["updated_at"] == EPOCH
    assert clean_record(rec(title="ab\ud83d"))["title"] == "ab"            # a cut emoji cannot reach the file
    assert clean_record(rec(updated_at="9999-12-31T23:59:59Z"))["updated_at"] < "2100"


def test_the_manual_flag_matches_the_browser_loader():
    assert clean_record(rec(manual=True))["manual"] is True
    assert clean_record(rec(manual=False))["manual"] is False
    assert clean_record(rec(manual="yes"))["manual"] is False and clean_record(rec())["manual"] is False


def test_control_and_bidi_override_characters_cannot_disguise_saved_text():
    r = clean_record(rec(title="Safe\u202etxt.exe\x07 title\u2066x", source="Goo\x00gle\u061c", summary="a\tb\nc"))
    assert r["title"] == "Safetxt.exe titlex" and r["source"] == "Google" and r["summary"] == "a b c"
    assert clean_record(rec(title="family \U0001F468\u200d\U0001F469"))["title"].endswith("\u200d\U0001F469")     # emoji joiners are kept
