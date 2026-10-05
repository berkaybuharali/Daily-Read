"""Unit tests for logic that silently loses or duplicates items if it breaks. No network, no Claude."""
from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from dailyread.config import load_config
from dailyread.http import Http, HttpError, truncate_words
from dailyread.models import Item, SourceStats, reading_minutes
from dailyread.pipeline import (RunOptions, clamp_summary, compute_window, counted_minutes, it_pick_count, read_limits,
                                review, sort_items, source_window, summary_problems)
from dailyread.sources.gcp_release_notes import split_notes
from dailyread.state import LockedError, StateStore
from dailyread.timeutil import Window

AMS = ZoneInfo("Europe/Amsterdam")
CFG = load_config()


def dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=AMS)


# ---- windows ---------------------------------------------------------------------------------------------------
def test_first_real_window_starts_at_config(tmp_path):
    w, rerun = compute_window(CFG, StateStore(tmp_path), RunOptions(dry_run=False), dt("2026-10-02T09:00"))
    assert w.start == dt("2026-10-01T00:00") and not rerun and w.day_lookback == 0


def test_next_window_starts_where_previous_ended_and_covers_gaps(tmp_path):
    store = StateStore(tmp_path)
    store.save_state({"last_report_date": "2026-10-02", "last_window_start": dt("2026-10-01T00:00").isoformat(),
                      "last_window_end": dt("2026-10-02T09:00").isoformat()})
    w, rerun = compute_window(CFG, store, RunOptions(dry_run=False), dt("2026-10-05T08:30"))   # skipped a weekend
    assert w.start == dt("2026-10-02T09:00") and w.end == dt("2026-10-05T08:30") and not rerun


def test_same_day_rerun_rebuilds_same_window(tmp_path):
    store = StateStore(tmp_path)
    store.save_state({"last_report_date": "2026-10-02", "last_window_start": dt("2026-10-01T00:00").isoformat(),
                      "last_window_end": dt("2026-10-02T09:00").isoformat()})
    w, rerun = compute_window(CFG, store, RunOptions(dry_run=False), dt("2026-10-02T15:00"))
    assert rerun and w.start == dt("2026-10-01T00:00")


def test_failed_source_catches_up_but_bounded():
    w = Window(dt("2026-10-20T09:00"), dt("2026-10-21T09:00"), AMS)
    assert source_window(w, {"covered_until": dt("2026-10-18T09:00").isoformat()}, False).start == dt("2026-10-18T09:00")
    long_ago = source_window(w, {"covered_until": dt("2026-09-01T09:00").isoformat()}, False)
    assert long_ago.start == w.start - timedelta(days=14)
    # a long global gap is NOT shortened for healthy sources
    gap = Window(dt("2026-09-01T09:00"), dt("2026-09-22T09:00"), AMS)
    assert source_window(gap, {"covered_until": gap.start.isoformat()}, False).start == gap.start


def test_date_labels_late_us_publication_not_lost():
    # previous run ended Sat 07:30 Amsterdam; Google adds a note labelled Friday at Fri 23:00 PT
    w = Window(dt("2026-10-03T07:30"), dt("2026-10-04T07:30"), AMS, day_lookback=2)
    assert w.contains_day(date(2026, 10, 2))
    assert not Window(w.start, w.end, AMS).contains_day(date(2026, 10, 2))


# ---- ids / parsing -----------------------------------------------------------------------------------------------
def test_release_note_ids_survive_a_prepended_note():
    from dailyread.models import make_id
    a = "<h3>Feature</h3><p>Alpha GA.</p><h3>Changed</h3><p>Beta updated.</p>"
    b = "<h3>Fixed</h3><p>New gamma fix.</p>" + a
    def ids(html):
        from dailyread.http import html_to_text
        return {make_id(t, " ".join(html_to_text(f).split())) for t, _, f in split_notes(html)}
    assert ids(a) <= ids(b)


def test_truncate_keeps_lines_and_marks():
    text = "- one two three\n- four five six\n- seven eight"
    out = truncate_words(text, 5)
    assert out.splitlines()[0] == "- one two three" and out.endswith("[…truncated: 11 words in full]")
    assert truncate_words(text, 50) == text


def test_full_length_survives_truncation_for_the_reading_time():
    long = " ".join(["word"] * 2650)
    cut = Item(id="a", source="x", url="https://x.test", published=None, day=date(2026, 10, 1),
               content=truncate_words(long, 1200), content_origin="article")
    assert cut.truncated and cut.word_count < 1210 and cut.full_word_count == 2650
    assert reading_minutes(cut.full_word_count) == 10 and reading_minutes(40) == 1
    assert reading_minutes(10 ** 9) == 999                         # a fake marker in feed text cannot blow it up
    old = replace(cut, content="some text […truncated]")              # marker from before the count was kept
    assert old.truncated and old.full_word_count is None and counted_minutes(old) is None     # unknown: the model estimates


def test_summary_lint_and_clamp():
    assert summary_problems("Google announces a thing.", 300)
    assert summary_problems("x" * 301, 300)
    assert not summary_problems("`AI.KEY_DRIVERS` is GA in BigQuery.", 300)
    s = clamp_summary("First sentence is here. " + "word " * 80, 120)
    assert len(s) <= 120


# ---- HTTP ------------------------------------------------------------------------------------------------------
def _http(handler, monkeypatch, curl=None):
    h = Http("UA", 5, 4, retries=5, backoff_base=0)
    h.client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("time.sleep", lambda s: None)
    monkeypatch.setattr(h, "_curl", lambda url: curl)
    return h


def test_http_retries_429_then_succeeds(monkeypatch):
    calls = []
    def handler(req):
        calls.append(1)
        return httpx.Response(429 if len(calls) < 4 else 200, text="ok")
    assert _http(handler, monkeypatch).get_text("https://x.test/a") == "ok" and len(calls) == 4


def test_http_404_fails_fast(monkeypatch):
    calls = []
    def handler(req):
        calls.append(1)
        return httpx.Response(404)
    with pytest.raises(HttpError):
        _http(handler, monkeypatch).get_text("https://x.test/a")
    assert len(calls) == 1


def test_http_403_uses_curl_fallback(monkeypatch):
    assert _http(lambda r: httpx.Response(403), monkeypatch, curl="via curl").get_text("https://x.test/") == "via curl"


def test_http_rejects_non_http_urls(monkeypatch):
    h = _http(lambda r: httpx.Response(200), monkeypatch)
    for bad in ("javascript:alert(1)", "/relative", "mailto:a@b.c", ""):
        with pytest.raises(HttpError):
            h.get_text(bad)


# ---- review enforcement ------------------------------------------------------------------------------------------
class FakeClaude:
    def __init__(self, out): self.out = out
    def prompt(self, *a): return ""
    def schema(self, n): return json.loads((CFG.schemas_dir / f"{n}.json").read_text())
    with_enum = staticmethod(lambda s, p, v: s)
    def call(self, *a, **k): return self.out


def _item(n, prescreen="candidate"):
    return Item(id=f"i{n}", source="x", url="https://x.test", published=None, day=date(2026, 10, 1),
                title=f"T{n}", summary="s", prescreen=prescreen)


def test_review_caps_reads_and_aligns_stars():
    items = [_item(n) for n in range(10)]
    out = {"items": [{"id": f"i{n}", "stars": 5, "verdict": "read", "reason": ""} for n in range(10)], "highlights": []}
    w = Window(dt("2026-10-01T09:00"), dt("2026-10-02T09:00"), AMS)
    review(FakeClaude(out), CFG, [{"key": "x", "title": "X", "items": items}], w)
    budget, cap = read_limits(w)
    reads = [i for i in items if i.verdict == "read"]
    assert len(reads) == cap and all(i.stars >= 4 for i in reads)
    assert all(i.stars <= 3 for i in items if i.verdict == "skip")
    assert all(i.reason.startswith("You should read because") for i in reads)   # empty reason didn't crash


def test_review_missing_items_default_to_skip():
    items = [_item(1), _item(2, "skip")]
    review(FakeClaude({"items": [], "highlights": []}), CFG, [{"key": "x", "title": "X", "items": items}],
           Window(dt("2026-10-01T09:00"), dt("2026-10-02T09:00"), AMS))
    assert all(i.verdict == "skip" and i.stars in (2, 3) for i in items)


def test_review_reading_time_is_counted_when_possible_and_estimated_otherwise():
    article = replace(_item(1), content=" ".join(["w"] * 1325), content_origin="article")
    excerpt = replace(_item(2), content="A short feed excerpt.", content_origin="feed")
    unrated = replace(_item(3), content="Curator note.", content_origin="curator_note")
    note = replace(_item(4), content="Fixed a bug.", content_origin="release_note")
    out = {"items": [{"id": "i1", "stars": 4, "verdict": "read", "reason": "x", "read_minutes": 30},
                     {"id": "i2", "stars": 2, "verdict": "skip", "reason": "x", "read_minutes": 8},
                     {"id": "i3", "stars": 2, "verdict": "skip", "reason": "x", "read_minutes": 0},
                     {"id": "i4", "stars": 2, "verdict": "skip", "reason": "x", "read_minutes": 2}], "highlights": []}
    review(FakeClaude(out), CFG, [{"key": "x", "title": "X", "items": [article, excerpt, unrated]},
                                  {"key": "bigquery_rn", "title": "BQ", "items": [note]}],
           Window(dt("2026-10-01T09:00"), dt("2026-10-02T09:00"), AMS))
    assert (article.read_minutes, article.minutes_estimated) == (5, False)       # counted words beat the model
    assert (excerpt.read_minutes, excerpt.minutes_estimated) == (8, True)
    assert unrated.read_minutes is None                                          # no estimate: nothing shown
    assert note.read_minutes is None                                             # release notes are read in place


def test_sort_puts_stars_first():
    a, b, c = _item(1), _item(2), _item(3)
    a.stars, b.stars, c.stars = 2, 5, 4
    assert [i.id for i in sort_items([a, b, c])] == ["i2", "i3", "i1"]


def test_it_pick_count_scales():
    lim = CFG.limits
    assert it_pick_count(1.0, lim) == lim["it_picks_min"]
    assert it_pick_count(3.5, lim) > lim["it_picks_min"]
    assert it_pick_count(30, lim) == lim["it_picks_max"]


# ---- state / lock ------------------------------------------------------------------------------------------------
def test_lock_blocks_live_owner_and_breaks_dead_one(tmp_path):
    store = StateStore(tmp_path)
    with store.lock():
        with pytest.raises(LockedError):
            with StateStore(tmp_path).lock():
                pass
    (tmp_path / "run.lock").mkdir()
    (tmp_path / "run.lock" / "pid").write_text("999999")          # no such process
    with store.lock():
        assert (tmp_path / "run.lock" / "pid").read_text() == str(os.getpid())
    assert not (tmp_path / "run.lock").exists()


def test_failed_source_keeps_old_covered_until(tmp_path):
    store = StateStore(tmp_path)
    w1 = Window(dt("2026-10-01T00:00"), dt("2026-10-02T09:00"), AMS)
    store.update_sources({"a": SourceStats(status="ok"), "b": SourceStats(status="http_error", error="x")}, w1.end, w1)
    w2 = Window(w1.end, dt("2026-10-03T09:00"), AMS)
    store.update_sources({"a": SourceStats(status="partial", error="y"), "b": SourceStats(status="ok")}, w2.end, w2)
    src = store.sources()
    assert src["a"]["covered_until"] == w1.end.isoformat()          # partial: don't advance
    assert src["b"]["covered_until"] == w2.end.isoformat()
    assert src["a"]["consecutive_failures"] == 1


def test_disabled_source_not_loaded(tmp_path):
    raw = (CFG.root / "config.yaml").read_text().replace("  - key: gcloud_blog\n    enabled: true",
                                                          "  - key: gcloud_blog\n    enabled: false")
    p = tmp_path / "c.yaml"
    p.write_text(raw)
    assert "gcloud_blog" not in [s.key for s in load_config(p).sections]


def test_real_window_capped_after_long_break(tmp_path):
    store = StateStore(tmp_path)
    store.save_state({"last_report_date": "2026-09-01", "last_window_start": dt("2026-08-31T09:00").isoformat(),
                      "last_window_end": dt("2026-09-01T09:00").isoformat()})
    w, _ = compute_window(CFG, store, RunOptions(dry_run=False), dt("2026-10-01T09:00"))
    assert w.start == dt("2026-10-01T09:00") - timedelta(days=14)


def test_rerun_carries_over_items_of_failed_source(tmp_path):
    from dailyread.models import SourceResult
    from dailyread.pipeline import carry_over_failed_sections
    data = tmp_path / "2026-10-02.json"
    data.write_text(json.dumps({"sections": [{"key": "blog_a", "items": [_item(1).to_dict()]}]}))
    results = {"blog_a": SourceResult("blog_a", stats=SourceStats(status="http_error"))}
    carry_over_failed_sections(results, data)
    assert [i.id for i in results["blog_a"].items] == ["i1"]


def test_seen_pruned(tmp_path):
    store = StateStore(tmp_path)
    store.add_seen(["old"], "2026-07-01")
    store.add_seen(["new"], "2026-10-02")
    assert set(store.seen()) == {"new"}


def test_missing_claude_binary_is_llm_error(tmp_path):
    from dailyread.llm import Claude, LlmError, Usage
    raw = {**CFG.raw, "claude": {**CFG.claude, "bin": str(tmp_path / "nope")}}
    with pytest.raises(LlmError):
        Claude(replace(CFG, raw=raw), Usage()).call("review", "x", {}, {"type": "object"}, retries=0)


def test_usage_limit_message_detected():
    from dailyread.llm import USAGE_LIMIT
    assert USAGE_LIMIT.search("You've hit your session limit · resets 5:30pm (Europe/Amsterdam)")
    assert not USAGE_LIMIT.search("invalid JSON in structured output")


def test_empty_sections_move_to_end_keeping_config_order():
    from dailyread.render import order_sections
    secs = [{"key": "a", "items": []}, {"key": "b", "items": [1]}, {"key": "c", "items": []}, {"key": "d", "items": [2]}]
    assert [s["key"] for s in order_sections(secs)] == ["b", "d", "a", "c"]


def test_local_sections_merge_and_position():
    from dailyread.config import merge_sections
    base = [{"key": "a", "title": "A"}, {"key": "b", "title": "B"}]
    local = [{"key": "p", "title": "P", "position": "first"}, {"key": "q", "title": "Q", "after": "a"},
             {"key": "b", "enabled": False}, {"key": "z", "title": "Z"}]
    out = merge_sections(base, local)
    assert [s["key"] for s in out] == ["p", "a", "q", "b", "z"]
    assert out[3]["enabled"] is False and out[3]["title"] == "B"
    assert "position" not in out[0] and "after" not in out[2]


def test_local_sources_plugin_loaded(tmp_path):
    from dailyread.sources import FETCHERS, load_local_sources
    (tmp_path / "mine.py").write_text("def fetch(ctx):\n    return None\nFETCHERS = {'mine_test': fetch}\n")
    (tmp_path / "example_x.py").write_text("FETCHERS = {'example_test': None}\n")
    assert load_local_sources(tmp_path) == ["mine_test"]
    assert "mine_test" in FETCHERS and "example_test" not in FETCHERS
    del FETCHERS["mine_test"]


def test_failed_run_records_health_without_advancing(tmp_path):
    from types import SimpleNamespace
    store = StateStore(tmp_path)
    w1 = SimpleNamespace(start=datetime(2026, 10, 1, tzinfo=AMS), end=datetime(2026, 10, 2, 7, tzinfo=AMS))
    store.update_sources({"a": SourceStats(status="ok")}, w1.end, w1)
    w2 = SimpleNamespace(start=w1.end, end=datetime(2026, 10, 3, 7, tzinfo=AMS))
    store.update_sources({"a": SourceStats(status="http_error", error="boom")}, w2.end, w2, advance=False)
    src = store.sources()["a"]
    assert src["covered_until"] == w1.end.isoformat(timespec="seconds")
    assert src["consecutive_failures"] == 1 and src["last_error"] == "boom"
    assert src["last_fetch_at"] == w2.end.isoformat(timespec="seconds")


def test_schedule_skip_reasons():
    from datetime import time as dtime
    from dailyread.schedule import skip_reason
    now = datetime(2026, 10, 3, 8, 0, tzinfo=AMS)
    nb = dtime(7, 0)
    assert skip_reason({"last_report_date": "2026-10-03"}, now, nb, 3) == "today's report already exists"
    assert skip_reason({"last_report_date": "2026-10-02"}, now.replace(hour=6), nb, 3) == "before 07:00"
    assert skip_reason({"last_report_date": "2026-10-02"}, now, nb, 3) is None
    gave_up = {"last_report_date": "2026-10-02", "last_failure_date": "2026-10-03", "failed_attempts_today": 3}
    assert "gave up" in skip_reason(gave_up, now, nb, 3)
    yesterday_fails = {**gave_up, "last_failure_date": "2026-10-02"}
    assert skip_reason(yesterday_fails, now, nb, 3) is None


def test_schedule_plist_uses_claude_config_dir():
    from dailyread.schedule import build_plist
    p = build_plist(CFG, env={"CLAUDE_CONFIG_DIR": "/x/.claude-work"})
    assert p["ProgramArguments"][-2:] == ["-m", "dailyread.agent_main"] and p["RunAtLoad"] is True
    # launchd hands its socket only to the job's own process: python must be started directly, never through uv
    assert not any("uv" in Path(a).name or a.endswith("bin/dailyread") for a in p["ProgramArguments"])
    assert p["StartInterval"] == 30 * 60
    assert p["EnvironmentVariables"]["CLAUDE_CONFIG_DIR"] == "/x/.claude-work"
    assert "CLAUDE_CONFIG_DIR" not in build_plist(CFG, env={})["EnvironmentVariables"]


def test_schedule_plist_also_holds_the_localhost_port_for_the_library_helper():
    from dailyread.schedule import build_plist
    p = build_plist(CFG, env={})
    listener = p["Sockets"]["Listener"]
    assert listener["SockNodeName"] == "127.0.0.1" and listener["SockServiceName"] == "47821"
    assert "KeepAlive" not in p                      # nothing may stay running between timer ticks and clicks
    assert p["ThrottleInterval"] == 5 and p["ProcessType"] == "Background"
    assert p["StartCalendarInterval"] == [{"Hour": 7, "Minute": 5}, {"Hour": 12, "Minute": 0}]   # wake safety net
    assert "Sockets" not in build_plist(CFG, env={}, helper=False)


def test_scheduler_usage_limit_does_not_burn_attempt(tmp_path, monkeypatch):
    from dailyread import schedule
    from dailyread.pipeline import RunFailed
    store = StateStore(tmp_path)
    store.save_state({"last_report_date": "2026-10-02"})
    monkeypatch.setattr(schedule, "paths", lambda cfg, dry_run: {"state": tmp_path})
    monkeypatch.setattr(schedule, "online", lambda: True)
    notes = []
    monkeypatch.setattr(schedule, "notify", lambda t, m: notes.append(m))

    def fail_with(msg):
        def _run(cfg, opts):
            store.record_failure("2026-10-03", msg)
            raise RunFailed(msg)
        return _run

    now = datetime(2026, 10, 3, 8, 0, tzinfo=AMS)
    monkeypatch.setattr(schedule, "run", fail_with("review: You've hit your session limit · resets 5:30pm"))
    schedule.tick(CFG, now)
    assert store.state()["failed_attempts_today"] == 0
    monkeypatch.setattr(schedule, "run", fail_with("review: boom"))
    for _ in range(3):
        schedule.tick(CFG, now)
    assert store.state()["failed_attempts_today"] == 3 and len(notes) == 1
    schedule.tick(CFG, now)                     # gave up: no 4th attempt
    assert store.state()["failed_attempts_today"] == 3


def test_sre_weekly_parse_issue_skips_sponsor_and_extracts_rows():
    from dailyread.sources.sre_weekly import parse_issue
    html = """
    <div class="sreweekly-sponsor-message"><p>A message from our sponsor <a href="https://x/ad">Ad</a></p></div>
    <div class="sreweekly-entry"><div class="sreweekly-title"><a href="https://a.example/p1">Post one</a></div>
      <div class="sreweekly-description"><blockquote><p>Why it broke.</p></blockquote>
      <p><small>Jane Doe — Example</small></p></div></div>
    <div class="sreweekly-entry"><div class="sreweekly-title"><a href="https://b.example/p2">Post two</a></div>
      <div class="sreweekly-description"><p>Notes.</p></div></div>
    """
    rows = parse_issue(html)
    assert [r["url"] for r in rows] == ["https://a.example/p1", "https://b.example/p2"]
    assert rows[0]["note"] == "Why it broke." and rows[0]["byline"] == "Jane Doe — Example"
    assert rows[1]["byline"] == ""


def test_claude_env_drops_api_key_and_effort_override(monkeypatch):
    import importlib
    from dailyread import llm
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_EFFORT_LEVEL"):
        monkeypatch.setenv(k, "x")
    llm = importlib.reload(llm)
    try:
        assert not {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_EFFORT_LEVEL"} & set(llm.CLAUDE_ENV)
    finally:
        monkeypatch.undo()
        importlib.reload(llm)
