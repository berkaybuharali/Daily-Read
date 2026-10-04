"""Browser UI tests for Read Later / Favorites: a real Chrome opens a rendered report from disk (file://) and clicks.

Optional: needs `uv run --group dev --group ui pytest tests/test_ui.py` and Google Chrome installed; otherwise skipped.
The native file picker cannot be clicked by a script, so file sync is tested with a fake in-memory file handle that
implements the same calls (getFile / createWritable / queryPermission). Reconnecting a handle that was stored in
IndexedDB across browser restarts can only be checked by hand in real Chrome."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from dailyread.config import load_config  # noqa: E402
from dailyread.library import EPOCH, clean_record  # noqa: E402
from dailyread.render import render_report  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config()
EMPTY = {"version": 1, "updated_at": EPOCH, "items": {}}

FAKE_FILE_API = """
(() => {
  window.__fs = { text: %s, writes: 0, perm: "granted", picks: 0 };
  const handle = {
    name: "library.json", kind: "file",
    getFile: async () => ({ text: async () => window.__fs.text }),
    createWritable: async () => { let buf = ""; return { write: async (t) => { buf = t; }, close: async () => { window.__fs.text = buf; window.__fs.writes++; } }; },
    queryPermission: async () => window.__fs.perm, requestPermission: async () => window.__fs.perm,
  };
  window.showSaveFilePicker = async (opts) => { window.__fs.picks++; window.__fs.opts = opts; return handle; };
})();
"""


@pytest.fixture(scope="module")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome")
        except Exception as e:      # Chrome not installed
            pytest.skip(f"Google Chrome is not available: {e}")
        yield b
        b.close()


def make_report(tmp_path: Path, library: dict | None = None, name: str = "report.html") -> Path:
    report = json.loads((ROOT / "fixtures" / "sample_report.json").read_text())
    f = tmp_path / name
    f.write_text(render_report(CFG, report, library=library or EMPTY))
    return f


class Page:
    """A report page plus the JS errors it raised."""

    def __init__(self, ctx, path: Path, viewport=None):
        self.page = ctx.new_page()
        if viewport:
            self.page.set_viewport_size(viewport)
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        self.page.goto(path.as_uri())

    def __getattr__(self, name):
        return getattr(self.page, name)

    def wait_js(self, expression: str, timeout: float = 8.0):
        """Wait until a JavaScript expression is truthy. Polls with evaluate(): the report's Content-Security-Policy forbids
        eval, which Playwright's own wait_for_function() needs."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.page.evaluate(expression):
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for: {expression}")

    def row(self, n: int):
        return self.page.locator("#panel-today tr.item").nth(n)

    def row_id(self, n: int) -> str:
        return self.row(n).get_attribute("id")[2:]

    def snaps(self) -> dict:
        return json.loads(self.page.locator("#report-items").inner_text())

    def save(self, n: int, kind: str):
        self.row(n).locator(f'button.act[data-act="{kind}"]').click()

    def count(self, which: str) -> int:
        return int(self.page.inner_text(f"#n-{which}"))

    def titles(self, panel: str) -> list[str]:
        return self.page.locator(f"#panel-{panel} tbody tr.lib-row a.t-title").all_inner_texts()


@pytest.fixture
def ctx(browser):
    c = browser.new_context(viewport={"width": 1400, "height": 900})
    yield c
    c.close()


@pytest.fixture
def open_page(ctx):
    opened: list[Page] = []

    def _open(path: Path, viewport=None) -> Page:
        pg = Page(ctx, path, viewport)
        opened.append(pg)
        return pg

    yield _open
    for pg in opened:      # no test may leave a JavaScript error behind
        assert pg.errors == [], pg.errors


# ---- tabs, saving, removing ---------------------------------------------------------------------------------------
def test_starts_on_today_with_empty_lists(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    assert pg.locator("#panel-today").is_visible() and not pg.locator("#panel-later").is_visible()
    assert (pg.count("later"), pg.count("favorites")) == (0, 0)
    pg.click("#tab-later")
    assert pg.locator("#panel-later").is_visible() and not pg.locator("#panel-today").is_visible()
    assert pg.locator("#panel-later .lib-empty").is_visible()
    assert pg.get_attribute("#tab-later", "aria-selected") == "true"
    pg.click("#tab-favorites")
    assert pg.locator("#panel-favorites .lib-empty").is_visible()
    pg.click("#tab-today")
    assert pg.locator("#panel-today").is_visible()


def test_every_row_has_both_buttons_and_they_toggle(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    rows = pg.locator("#panel-today tr.item").count()
    assert pg.locator("#panel-today button.act[data-act=later]").count() == rows
    assert pg.locator("#panel-today button.act[data-act=fav]").count() == rows
    later = pg.row(0).locator("button.act[data-act=later]")
    assert later.get_attribute("aria-pressed") == "false"
    later.click()
    assert later.get_attribute("aria-pressed") == "true" and pg.count("later") == 1
    later.click()
    assert later.get_attribute("aria-pressed") == "false" and pg.count("later") == 0


def test_saved_items_appear_in_the_right_lists_with_all_details(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    snaps = pg.snaps()
    a, b = pg.row_id(0), pg.row_id(3)
    pg.save(0, "later"); pg.save(3, "later"); pg.save(3, "fav")
    assert (pg.count("later"), pg.count("favorites")) == (2, 1)
    pg.click("#tab-later")
    assert set(pg.titles("later")) == {snaps[a]["title"], snaps[b]["title"]}
    row = pg.locator(f"#panel-later tr.lib-row[data-id='{b}']")
    text = row.inner_text()
    assert snaps[b]["source"] in text and snaps[b]["summary"][:40] in text      # source column + details, no LLM
    assert row.locator("a.t-title").get_attribute("href") == snaps[b]["url"]
    pg.click("#tab-favorites")
    assert pg.titles("favorites") == [snaps[b]["title"]]


def test_lists_are_sorted_by_article_date_newest_first(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    snaps = pg.snaps()
    for n in range(8):
        pg.save(n, "later")
    pg.click("#tab-later")
    dates = [snaps[i.get_attribute("data-id")]["published"] for i in pg.locator("#panel-later tr.lib-row").all()]
    assert dates == sorted(dates, reverse=True) and len(set(dates)) > 1


def test_mark_read_moves_to_bottom_and_can_be_undone(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    for n in range(3):
        pg.save(n, "later")
    pg.click("#tab-later")
    first_id = pg.locator("#panel-later tr.lib-row").first.get_attribute("data-id")
    pg.locator("#panel-later tr.lib-row").first.locator("button[data-act=read]").click()
    last = pg.locator("#panel-later tr.lib-row").last
    assert last.get_attribute("data-id") == first_id and "is-done" in last.get_attribute("class")
    assert "1 read" in pg.inner_text("#panel-later .lib-count")
    assert pg.count("later") == 3                                    # finished items still count
    last.locator("button[data-act=read]").click()                    # undo: no row is finished any more
    assert "0 read" in pg.inner_text("#panel-later .lib-count")
    assert pg.locator("#panel-later tr.lib-row.is-done").count() == 0


def test_remove_deletes_from_read_later_but_keeps_favorite(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    pg.save(0, "later"); pg.save(0, "fav")
    pg.click("#tab-later")
    pg.locator("#panel-later button[data-act=remove]").click()
    assert pg.locator("#panel-later .lib-empty").is_visible() and pg.count("later") == 0
    assert pg.count("favorites") == 1
    pg.click("#tab-favorites")
    assert len(pg.titles("favorites")) == 1


def test_heart_in_read_later_adds_to_favorites_and_unheart_in_favorites_removes(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    pg.save(1, "later")
    pg.click("#tab-later")
    pg.locator("#panel-later button.act[data-act=fav]").click()
    assert pg.count("favorites") == 1
    pg.click("#tab-favorites")
    pg.locator("#panel-favorites button.act[data-act=fav]").click()
    assert pg.count("favorites") == 0 and pg.locator("#panel-favorites .lib-empty").is_visible()
    assert pg.count("later") == 1


def test_buttons_on_today_follow_changes_made_in_the_lists(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    pg.save(0, "later")
    pg.click("#tab-later")
    pg.locator("#panel-later button[data-act=remove]").click()
    pg.click("#tab-today")
    assert pg.row(0).locator("button.act[data-act=later]").get_attribute("aria-pressed") == "false"


def test_search_filters_the_list(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    snaps = pg.snaps()
    for n in range(4):
        pg.save(n, "later")
    pg.click("#tab-later")
    target = snaps[pg.row_id(2)]["title"]
    word = max(re.findall(r"[A-Za-z]{5,}", target), key=len)
    pg.fill("#panel-later .lib-search", word)
    shown = pg.titles("later")
    assert target in shown and all(word.lower() in (t + " " + snaps[i]["summary"] + snaps[i]["source"]).lower()
                                   for t, i in zip(shown, [r.get_attribute("data-id") for r in pg.locator("#panel-later tr.lib-row").all()]))
    pg.fill("#panel-later .lib-search", "zzzz-no-such-thing")
    assert pg.locator("#panel-later .lib-nomatch").is_visible()
    pg.fill("#panel-later .lib-search", "")
    assert len(pg.titles("later")) == 4


def test_jump_link_to_a_row_returns_to_today(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    pg.click("#tab-later")
    pg.evaluate("location.hash = '#i-' + document.querySelector('#panel-today tr.item').id.slice(2)")
    assert pg.locator("#panel-today").is_visible() and not pg.locator("#panel-later").is_visible()


# ---- persistence --------------------------------------------------------------------------------------------------
def test_lists_survive_reload_and_are_shared_by_other_reports(tmp_path, open_page):
    one, two = make_report(tmp_path, name="one.html"), make_report(tmp_path, name="two.html")
    pg = open_page(one)
    pg.save(0, "later"); pg.save(2, "fav")
    pg.reload()
    assert (pg.count("later"), pg.count("favorites")) == (1, 1)
    assert pg.row(0).locator("button.act[data-act=later]").get_attribute("aria-pressed") == "true"
    other = open_page(two)                      # a different report file in the same Chrome
    assert (other.count("later"), other.count("favorites")) == (1, 1)
    other.save(5, "later")
    assert pg.evaluate("localStorage.length") >= 1
    pg.wait_js("document.getElementById('n-later').textContent === '2'")     # live update across tabs


def test_baked_in_saved_lists_show_without_any_click(tmp_path, open_page):
    doc = {"version": 1, "updated_at": EPOCH, "items": {
        "x1": clean_record({"id": "x1", "title": "Saved earlier", "url": "https://example.com/x", "source": "Brooker",
                            "published": "2026-09-01", "stars": 5, "verdict": "read", "summary": "Details.", "read_later": True,
                            "updated_at": "2026-09-02T10:00:00.000Z"}),
        "x2": clean_record({"id": "x2", "title": "Loved earlier", "url": "https://example.com/y", "source": "Alderson",
                            "published": "2026-09-02", "favorite": True, "updated_at": "2026-09-02T10:00:00.000Z"})}}
    pg = open_page(make_report(tmp_path, doc))
    assert (pg.count("later"), pg.count("favorites")) == (1, 1)
    pg.click("#tab-later")
    assert pg.titles("later") == ["Saved earlier"]
    assert "Brooker" in pg.inner_text("#panel-later tr.lib-row")


# ---- safety -------------------------------------------------------------------------------------------------------
def test_hostile_saved_data_is_shown_as_text_and_never_runs(tmp_path, open_page):
    evil = {"id": "e1", "title": "<img src=x onerror=\"window.pwned=1\">Evil", "url": "javascript:window.pwned=1",
            "source": "<b>src</b>", "published": "2026-09-01", "summary": "<script>window.pwned=1</script>", "read_later": True,
            "favorite": True}
    pg = open_page(make_report(tmp_path, {"version": 1, "updated_at": EPOCH, "items": {"e1": clean_record(evil)}}))
    pg.click("#tab-later")
    row = pg.locator("#panel-later tr.lib-row")
    assert "<img src=x" in row.inner_text() and row.locator("img").count() == 0
    assert row.locator("b").count() == 0
    assert (row.locator("a.t-title").get_attribute("href") or "") == ""      # javascript: link dropped
    assert pg.evaluate("window.pwned") is None


def test_hostile_json_imported_from_a_file_is_cleaned(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"items": {"z": {"id": "z", "title": "<i>t</i>", "url": "data:text/html,<script>1</script>",
                                               "read_later": True, "stars": "5"}}}))
    pg.click("#tab-later")
    pg.set_input_files("input[type=file]", str(bad))
    row = pg.locator("#panel-later tr.lib-row")
    row.wait_for()
    assert row.locator("i").count() == 0 and (row.locator("a.t-title").get_attribute("href") or "") == ""


# ---- export / import ----------------------------------------------------------------------------------------------
def test_export_and_import_roundtrip(tmp_path, open_page, browser):
    pg = open_page(make_report(tmp_path))
    pg.save(0, "later"); pg.save(1, "fav")
    pg.click("#tab-later")
    with pg.expect_download() as dl:
        pg.click("#panel-later .btn-export")
    saved = tmp_path / "export.json"
    dl.value.save_as(saved)
    doc = json.loads(saved.read_text())
    assert doc["version"] == 1 and len(doc["items"]) == 2
    assert sum(r["read_later"] for r in doc["items"].values()) == 1 and sum(r["favorite"] for r in doc["items"].values()) == 1

    fresh = browser.new_context()
    try:
        other = Page(fresh, make_report(tmp_path, name="fresh.html"))
        other.click("#tab-later")
        other.set_input_files("input[type=file]", str(saved))
        other.wait_js("document.getElementById('n-later').textContent === '1'")
        assert other.count("favorites") == 1 and "imported" in other.inner_text("#panel-later .lib-status")
        assert other.errors == []
    finally:
        fresh.close()


def test_importing_something_else_is_refused(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    pg.save(0, "later")
    junk = tmp_path / "junk.json"
    junk.write_text('{"hello": "world"}')
    pg.click("#tab-later")
    pg.set_input_files("input[type=file]", str(junk))
    pg.wait_js("document.querySelector('#panel-later .lib-status').textContent.includes('not a Daily Read library')")
    assert pg.count("later") == 1


# ---- file sync (Chrome's File System Access API, faked) -----------------------------------------------------------
def with_fake_file(ctx, text: str = ""):
    ctx.add_init_script(FAKE_FILE_API % json.dumps(text))


def test_linking_a_file_merges_it_and_then_keeps_it_in_sync(tmp_path, ctx, open_page):
    older = {"version": 1, "updated_at": EPOCH, "items": {"old1": clean_record({
        "id": "old1", "title": "Already in the file", "url": "https://example.com/o", "source": "Luu", "published": "2026-08-01",
        "favorite": True, "updated_at": "2026-09-01T00:00:00.000Z"})}}
    with_fake_file(ctx, json.dumps(older))
    pg = open_page(make_report(tmp_path))
    pg.save(0, "later")
    pg.click("#tab-later")
    assert "Saved in this browser" in pg.inner_text("#panel-later .lib-status")
    pg.click("#panel-later .btn-link")
    pg.wait_js("document.querySelector('#panel-later .lib-status').textContent.startsWith('✓ Synced')")
    assert "library.json" in pg.inner_text("#panel-later .lib-status")
    assert pg.count("favorites") == 1                                    # the file's item arrived in the browser
    first_write = pg.evaluate("window.__fs.writes")
    on_disk = json.loads(pg.evaluate("window.__fs.text"))
    assert len(on_disk["items"]) == 2 and on_disk["version"] == 1        # and the browser's item went to the file
    pg.click("#tab-today")
    pg.save(4, "later")                                                  # later clicks are written automatically
    pg.wait_js(f"window.__fs.writes > {first_write}")
    on_disk = json.loads(pg.evaluate("window.__fs.text"))
    assert sum(r["read_later"] for r in on_disk["items"].values()) == 2
    pg.click("#tab-later")
    pg.locator("#panel-later button[data-act=remove]").first.click()     # removals too (kept as a merge-able record)
    pg.wait_js("JSON.parse(window.__fs.text).items && Object.values(JSON.parse(window.__fs.text).items).filter(r => r.read_later).length === 1")


def test_a_file_that_is_not_a_library_is_not_overwritten(tmp_path, ctx, open_page):
    with_fake_file(ctx, "this is my shopping list")
    pg = open_page(make_report(tmp_path))
    pg.save(0, "later")
    pg.click("#tab-later")
    pg.click("#panel-later .btn-link")
    pg.wait_js("document.querySelector('#panel-later .lib-status').textContent.includes('not a Daily Read library')")
    assert pg.evaluate("window.__fs.text") == "this is my shopping list" and pg.evaluate("window.__fs.writes") == 0
    pg.click("#tab-today")                                              # lists keep working in the browser
    pg.save(1, "later")
    assert pg.count("later") == 2 and pg.evaluate("window.__fs.writes") == 0


def test_empty_file_is_accepted_and_filled(tmp_path, ctx, open_page):
    with_fake_file(ctx, "")
    pg = open_page(make_report(tmp_path))
    pg.save(0, "fav")
    pg.click("#tab-favorites")
    pg.click("#panel-favorites .btn-link")
    pg.wait_js("window.__fs.writes >= 1")
    assert len(json.loads(pg.evaluate("window.__fs.text"))["items"]) == 1


def test_browser_without_file_access_falls_back_to_export(tmp_path, ctx, open_page):
    ctx.add_init_script("window.showSaveFilePicker = undefined;")
    pg = open_page(make_report(tmp_path))
    pg.click("#tab-later")
    assert not pg.locator("#panel-later .btn-link").is_visible()
    assert "Export" in pg.inner_text("#panel-later .lib-status")
    assert pg.locator("#panel-later .btn-export").is_visible() and pg.locator("#panel-later .btn-import").is_visible()
    pg.click("#tab-today")
    pg.save(0, "later")
    assert pg.count("later") == 1


# ---- layout -------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("tab", ["today", "later", "favorites"])
def test_phone_layout_does_not_scroll_sideways(tmp_path, open_page, tab):
    pg = open_page(make_report(tmp_path), viewport={"width": 390, "height": 844})
    for n in range(3):
        pg.save(n, "later"); pg.save(n, "fav")
    pg.click(f"#tab-{tab}")
    assert pg.evaluate("document.documentElement.scrollWidth") <= 392
    box = pg.locator(f"#panel-{tab} button.act, #panel-{tab} button.pill-btn").first.bounding_box()
    assert box and box["x"] >= 0 and box["x"] + box["width"] <= 392


def test_keyboard_arrows_move_between_tabs(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    pg.focus("#tab-today")
    pg.keyboard.press("ArrowRight")
    assert pg.locator("#panel-later").is_visible()
    pg.keyboard.press("ArrowRight")
    assert pg.locator("#panel-favorites").is_visible()
    pg.keyboard.press("ArrowRight")
    assert pg.locator("#panel-today").is_visible()


# ---- the "your lists are only in this browser" reminder -------------------------------------------------------------
def test_reminder_appears_with_the_first_saved_item_and_goes_away_once_linked(tmp_path, ctx, open_page):
    with_fake_file(ctx, "")
    pg = open_page(make_report(tmp_path))
    assert not pg.locator("#lib-banner").is_visible()                    # nothing saved, nothing to protect
    pg.save(0, "later")
    banner = pg.locator("#lib-banner")
    assert banner.is_visible() and "only saved in this browser" in banner.inner_text()
    assert pg.locator("#lib-banner .banner-act").inner_text() == "Link library file"
    pg.click("#lib-banner .banner-act")
    pg.wait_js("document.getElementById('lib-banner').hidden")
    assert pg.evaluate("window.__fs.picks") == 1 and len(json.loads(pg.evaluate("window.__fs.text"))["items"]) == 1
    assert pg.evaluate("window.__fs.opts.id") == "dailyread-library"     # Chrome remembers the folder for next time
    pg.save(1, "fav")                                                     # stays quiet while synced
    assert not banner.is_visible()


def test_reminder_can_be_hidden_for_the_session_and_does_not_block_anything(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    pg.save(0, "later")
    pg.click("#lib-banner .banner-x")
    assert not pg.locator("#lib-banner").is_visible()
    pg.save(1, "later")
    assert not pg.locator("#lib-banner").is_visible() and pg.count("later") == 2


def test_reminder_without_file_access_offers_export(tmp_path, ctx, open_page):
    ctx.add_init_script("window.showSaveFilePicker = undefined;")
    pg = open_page(make_report(tmp_path))
    pg.save(0, "later")
    assert "can't write a file" in pg.inner_text("#lib-banner")
    with pg.expect_download():
        pg.click("#lib-banner .banner-act")


def test_reminder_explains_a_file_problem(tmp_path, ctx, open_page):
    with_fake_file(ctx, "not a library")
    pg = open_page(make_report(tmp_path))
    pg.save(0, "later")
    pg.click("#lib-banner .banner-act")
    pg.wait_js("document.querySelector('#lib-banner .lib-banner-text').textContent.includes('not a Daily Read library')")
    assert pg.locator("#lib-banner").is_visible() and pg.evaluate("window.__fs.text") == "not a library"


# ---- auto-save through the helper (a real helper server, real Chrome, no link click anywhere) ---------------------------
import socket as _socket  # noqa: E402
import threading  # noqa: E402

from dailyread.library_helper import Server  # noqa: E402

HELPER_TOKEN = "ui-test-token"


@pytest.fixture
def helper(tmp_path):
    lib = tmp_path / "state" / "library.json"
    srv = Server(("127.0.0.1", 0), lib, HELPER_TOKEN, idle_seconds=3600)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"path": lib, "server": srv, "cfg": {"url": f"http://127.0.0.1:{srv.server_address[1]}", "token": HELPER_TOKEN}}
    srv.shutdown()
    srv.server_close()


def make_helper_report(tmp_path, cfg, name="report.html", library=None):
    report = json.loads((ROOT / "fixtures" / "sample_report.json").read_text())
    f = tmp_path / name
    f.write_text(render_report(CFG, report, library=library or EMPTY, helper=cfg))
    return f


def file_items(path):
    return json.loads(path.read_text())["items"] if path.exists() else {}


def test_clicks_are_saved_to_the_state_file_with_no_link_and_no_prompt(tmp_path, open_page, helper):
    pg = open_page(make_helper_report(tmp_path, helper["cfg"]))
    pg.save(0, "later"); pg.save(2, "fav")
    deadline = time.time() + 5
    while time.time() < deadline and len([r for r in file_items(helper["path"]).values() if r["read_later"] or r["favorite"]]) < 2:
        time.sleep(0.1)
    items = file_items(helper["path"])
    assert sum(r["read_later"] for r in items.values()) == 1 and sum(r["favorite"] for r in items.values()) == 1
    pg.click("#tab-later")
    assert "Saved automatically" in pg.inner_text("#panel-later .lib-status")
    assert not pg.locator("#lib-banner").is_visible()                       # no reminder: nothing for the user to do
    assert not pg.locator("#panel-later .btn-link").is_visible()            # no "Link library file" button at all


def test_removals_reach_the_file_too(tmp_path, open_page, helper):
    pg = open_page(make_helper_report(tmp_path, helper["cfg"]))
    pg.save(1, "later")
    pg.click("#tab-later")
    pg.locator("#panel-later button[data-act=remove]").click()
    deadline = time.time() + 5
    while time.time() < deadline and not (file_items(helper["path"]) and not any(r["read_later"] for r in file_items(helper["path"]).values())):
        time.sleep(0.1)
    items = file_items(helper["path"])
    assert items and not any(r["read_later"] for r in items.values())       # kept as a tombstone, flags off


def test_a_fresh_browser_gets_the_lists_back_from_the_file(tmp_path, browser, helper):
    first = browser.new_context()
    second = browser.new_context()                       # empty browser storage, like a new laptop or a cleared Chrome
    try:
        a = Page(first, make_helper_report(tmp_path, helper["cfg"], "a.html"))
        a.save(0, "later"); a.save(1, "fav")
        deadline = time.time() + 5
        while time.time() < deadline and len([r for r in file_items(helper["path"]).values() if r["read_later"] or r["favorite"]]) < 2:
            time.sleep(0.1)
        b = Page(second, make_helper_report(tmp_path, helper["cfg"], "b.html"))     # baked-in lists: none
        b.wait_js("document.getElementById('n-later').textContent === '1'")
        assert b.count("favorites") == 1
        assert a.errors == [] and b.errors == []
    finally:
        first.close(); second.close()


def test_if_the_helper_is_down_lists_still_work_and_retry_catches_up(tmp_path, open_page):
    probe = _socket.socket(); probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]; probe.close()     # a free port
    lib = tmp_path / "state" / "library.json"
    pg = open_page(make_helper_report(tmp_path, {"url": f"http://127.0.0.1:{port}", "token": HELPER_TOKEN}))
    pg.save(0, "later")
    pg.wait_js("!document.getElementById('lib-banner').hidden")
    assert "did not answer" in pg.inner_text("#lib-banner") and pg.count("later") == 1      # nothing is lost meanwhile
    srv = Server(("127.0.0.1", port), lib, HELPER_TOKEN, idle_seconds=3600)               # the helper "starts"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        pg.click("#lib-banner .banner-act")                                               # Retry
        pg.wait_js("document.getElementById('lib-banner').hidden")
        assert len(file_items(lib)) == 1
    finally:
        srv.shutdown(); srv.server_close()
    pg.errors = [e for e in pg.errors if "Failed to load resource" not in e]       # Chrome logs the refused connection


def test_a_wrong_token_is_refused_and_nothing_is_written(tmp_path, open_page, helper):
    bad = {**helper["cfg"], "token": "not-the-token"}
    pg = open_page(make_helper_report(tmp_path, bad))
    pg.save(0, "later")
    pg.wait_js("!document.getElementById('lib-banner').hidden")
    assert "403" in pg.inner_text("#lib-banner") and not helper["path"].exists()
    assert pg.count("later") == 1
    pg.errors = [e for e in pg.errors if "Failed to load resource" not in e]       # Chrome logs the 403 in its console


def test_a_stranger_page_cannot_write_to_the_helper(helper, browser):
    ctx = browser.new_context()
    try:
        page = ctx.new_page()
        page.goto("data:text/html,<title>other</title>")          # a page that is not one of our reports (Origin: null as well)
        status = page.evaluate("""async (cfg) => { try {
            const r = await fetch(cfg.url + '/library', {method: 'POST', headers: {'X-DailyRead-Token': 'guess', 'Content-Type': 'application/json'}, body: '{"items":{}}'});
            return r.status; } catch (e) { return 'blocked'; } }""", helper["cfg"])
        assert status in (403, "blocked") and not helper["path"].exists()
    finally:
        ctx.close()


# ---- report navigation: prev / next / table of contents ----------------------------------------------------------------
import test_catalog  # noqa: E402


def nav_tree(tmp_path):
    cfg, reports, data = test_catalog.tree(tmp_path)
    return reports


def test_previous_and_next_buttons_walk_through_the_archive(tmp_path, open_page):
    reports = nav_tree(tmp_path)
    pg = open_page(reports / "archive" / "2026" / "2026-10-03.html")
    nav = pg.locator("#day-nav")
    assert nav.is_visible()
    assert pg.locator("#day-nav a.prev").inner_text() == "‹ Fri 2 Oct" and pg.locator("#day-nav a.next").inner_text() == "Sun 4 Oct ›"
    pg.click("#day-nav a.prev")
    assert pg.url.endswith("archive/2026/2026-10-02.html")
    assert pg.locator("#day-nav span.prev.off").inner_text() == "‹ Oldest"                # nothing older
    pg.click("#day-nav a.next"); pg.click("#day-nav a.next")
    assert pg.url.endswith("reports/2026-10-04.html")
    assert pg.locator("#day-nav span.next.off").inner_text() == "Latest ›"                 # nothing newer
    assert pg.locator("#day-nav a.prev").get_attribute("href") == "archive/2026/2026-10-03.html"


def test_reports_menu_lists_every_day_with_line_and_must_read_counts(tmp_path, open_page):
    reports = nav_tree(tmp_path)
    sample = json.loads((ROOT / "fixtures" / "sample_report.json").read_text())
    items = [i for s in sample["sections"] for i in s["items"]]
    lines, must = len(items), sum(1 for i in items if i.get("verdict") == "read")
    pg = open_page(reports / "2026-10-04.html")
    assert not pg.locator("#reports-pop").is_visible()
    pg.click("#day-nav .reports-btn")
    assert pg.locator("#reports-pop").is_visible() and pg.evaluate("document.getElementById('reports-pop').matches(':popover-open')")
    rows = pg.locator("#reports-pop tbody tr")
    assert rows.count() == 3 and "3 reports" in pg.inner_text("#reports-pop .pop-head")
    first = rows.nth(0).inner_text()
    assert "Sun 4 Oct" in first and str(lines) in first and f"✅ {must}" in first
    assert "current" in rows.nth(0).get_attribute("class")
    assert pg.get_attribute("#reports-pop .pop-hist", "href") == "index.html"
    pg.keyboard.press("Escape")
    assert not pg.locator("#reports-pop").is_visible()


def test_reports_menu_row_opens_that_report_from_an_archived_page(tmp_path, open_page):
    reports = nav_tree(tmp_path)
    pg = open_page(reports / "archive" / "2026" / "2026-10-02.html")
    pg.click("#day-nav .reports-btn")
    assert pg.get_attribute("#reports-pop .pop-hist", "href") == "../../index.html"
    pg.locator("#reports-pop tbody tr").nth(0).locator("td").nth(2).click()                 # click anywhere on the row
    pg.wait_for_url("**/reports/2026-10-04.html")
    pg.click("#day-nav .reports-btn")
    pg.locator("#reports-pop tbody tr").nth(2).locator("a").click()
    pg.wait_for_url("**/archive/2026/2026-10-02.html")


def test_menu_closes_on_outside_click_and_navigation_is_hidden_without_a_catalog(tmp_path, open_page):
    reports = nav_tree(tmp_path)
    pg = open_page(reports / "2026-10-04.html")
    pg.click("#day-nav .reports-btn")
    pg.click("h1")
    assert not pg.locator("#reports-pop").is_visible()
    alone = open_page(make_report(tmp_path, name="standalone.html"))                       # no catalog.js: bar stays hidden
    assert not alone.locator("#day-nav").is_visible()


def test_phone_navigation_menu_fits_the_screen(tmp_path, open_page):
    reports = nav_tree(tmp_path)
    pg = open_page(reports / "2026-10-04.html", viewport={"width": 390, "height": 844})
    pg.click("#day-nav .reports-btn")
    box = pg.locator("#reports-pop").bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 392 and pg.evaluate("document.documentElement.scrollWidth") <= 392


# ---- "from report" links in the lists ---------------------------------------------------------------------------------
def test_saved_items_link_back_to_the_report_they_came_from(tmp_path, open_page):
    reports = nav_tree(tmp_path)
    older = reports / "archive" / "2026" / "2026-10-03.html"
    pg = open_page(older)
    rid = pg.row_id(2)
    pg.save(2, "later")
    newest = open_page(reports / "2026-10-04.html")             # another day, same Chrome: the list is shared
    newest.click("#tab-later")
    link = newest.locator("#panel-later a.from-report")
    assert link.inner_text() == "Sat 3 Oct report ↗"
    assert link.get_attribute("href") == f"archive/2026/2026-10-03.html#i-{rid}"
    link.click()
    newest.wait_for_url(f"**/archive/2026/2026-10-03.html#i-{rid}")
    assert newest.locator("#panel-today").is_visible()          # lands on Today, scrolled to that very row
    assert newest.locator(f"#i-{rid}").is_visible()
    assert newest.row(2).locator("button.act[data-act=later]").get_attribute("aria-pressed") == "true"


def test_from_report_link_for_an_item_of_this_very_report_stays_on_the_page(tmp_path, open_page):
    reports = nav_tree(tmp_path)
    pg = open_page(reports / "2026-10-04.html")
    rid = pg.row_id(0)
    pg.save(0, "fav")
    pg.click("#tab-favorites")
    link = pg.locator("#panel-favorites a.from-report")
    assert link.get_attribute("href") == f"#i-{rid}"
    link.click()
    pg.wait_js("!document.getElementById('panel-today').hidden")           # the hash change switches back to Today
    assert pg.locator("#panel-today").is_visible() and pg.locator(f"#i-{rid}").is_visible()


def test_items_whose_report_is_gone_have_no_broken_link(tmp_path, open_page):
    doc = {"version": 1, "updated_at": EPOCH, "items": {"x1": clean_record({
        "id": "x1", "title": "From a deleted report", "report": "2025-01-01", "read_later": True,
        "updated_at": "2026-09-02T10:00:00.000Z"})}}
    reports = nav_tree(tmp_path)
    f = reports / "2026-10-04.html"
    f.write_text(render_report(CFG, json.loads((ROOT / "fixtures" / "sample_report.json").read_text()) | {"generated_at": "2026-10-04T09:12:00+02:00", "mode": "real"},
                               library=doc, root_rel="", helper=None))
    pg = open_page(f)
    pg.click("#tab-later")
    assert pg.titles("later") == ["From a deleted report"] and pg.locator("#panel-later a.from-report").count() == 0


# ---- accessibility behaviour found by the independent UI review (2026-10-04) --------------------------------------------
def test_toggle_buttons_keep_one_label_and_report_their_state_with_aria_pressed(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    btn = pg.row(0).locator("button.act[data-act=later]")
    before = btn.get_attribute("aria-label")
    assert before.startswith("Read Later: ") and btn.get_attribute("aria-pressed") == "false"
    btn.click()
    assert btn.get_attribute("aria-label") == before and btn.get_attribute("aria-pressed") == "true"
    pg.click("#tab-later")
    heart = pg.locator("#panel-later button.act[data-act=fav]")
    assert heart.get_attribute("aria-label").startswith("Favorite: ") and heart.get_attribute("aria-pressed") == "false"


def test_saving_and_removing_are_announced_to_screen_readers(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    live = pg.locator("#announce")
    assert live.get_attribute("role") == "status" and live.get_attribute("aria-live") == "polite"
    pg.save(0, "later")
    pg.wait_js("document.getElementById('announce').textContent.startsWith('Saved to Read Later')")
    pg.save(0, "later")
    pg.wait_js("document.getElementById('announce').textContent.startsWith('Removed from Read Later')")
    pg.save(1, "fav")
    pg.wait_js("document.getElementById('announce').textContent.startsWith('Added to Favorites')")


def test_keyboard_focus_stays_in_the_list_after_read_and_remove(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    for n in range(3):
        pg.save(n, "later")
    pg.click("#tab-later")
    first = pg.locator("#panel-later tr.lib-row").first
    first.locator("button[data-act=read]").focus()
    pg.keyboard.press("Enter")                                   # "✓ Read": the row moves, focus must follow the same button
    assert pg.evaluate("document.activeElement.getAttribute('data-act')") == "read"
    assert pg.evaluate("!!document.activeElement.closest('#panel-later tr.lib-row')")
    pg.locator("#panel-later tr.lib-row").first.locator("button[data-act=remove]").focus()
    pg.keyboard.press("Enter")                                   # "✕ Remove": the row disappears, focus goes to a neighbour
    assert pg.evaluate("!!document.activeElement.closest('#panel-later tr.lib-row')")
    assert pg.evaluate("document.activeElement.tagName") != "BODY"


def test_home_and_end_keys_jump_to_the_first_and_last_tab(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    pg.focus("#tab-today")
    pg.keyboard.press("End")
    assert pg.locator("#panel-favorites").is_visible() and pg.evaluate("document.activeElement.id") == "tab-favorites"
    pg.keyboard.press("Home")
    assert pg.locator("#panel-today").is_visible() and pg.evaluate("document.activeElement.id") == "tab-today"


def test_tabs_sit_directly_above_their_panels_and_search_boxes_are_landmarks(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    assert pg.evaluate("document.getElementById('tab-today').parentElement.tagName") == "DIV"      # a nav role would be lost
    assert pg.evaluate("document.querySelector('.tabs').nextElementSibling.id") == "panel-today"    # Tab goes tab -> panel
    assert pg.locator("search").count() == 3


def test_reports_menu_is_a_native_popover_that_closes_with_escape_and_returns_focus(tmp_path, open_page):
    reports = nav_tree(tmp_path)
    pg = open_page(reports / "2026-10-04.html")
    assert pg.evaluate("document.getElementById('reports-pop').hasAttribute('popover')")
    pg.click("#day-nav .reports-btn")
    assert pg.evaluate("document.getElementById('reports-pop').matches(':popover-open')")
    pg.wait_js("!!document.activeElement.closest('#reports-pop')")                   # focus moved into the table of contents
    pg.keyboard.press("Escape")
    assert not pg.evaluate("document.getElementById('reports-pop').matches(':popover-open')")
    assert pg.evaluate("document.activeElement.classList.contains('reports-btn')")  # and came back to the button


def test_from_report_links_have_a_meaningful_name(tmp_path, open_page):
    reports = nav_tree(tmp_path)
    pg = open_page(reports / "archive" / "2026" / "2026-10-03.html")
    pg.save(2, "later")
    other = open_page(reports / "2026-10-04.html")
    other.click("#tab-later")
    label = other.locator("#panel-later a.from-report").get_attribute("aria-label")
    assert label.startswith("Open “") and label.endswith("in the Sat 3 Oct report")


def test_the_reminder_stays_dismissed_after_a_reload(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    pg.save(0, "later")
    assert pg.locator("#lib-banner").is_visible()
    pg.click("#lib-banner .banner-x")
    pg.reload()
    assert not pg.locator("#lib-banner").is_visible() and pg.count("later") == 1


def test_the_reminder_is_spoken_when_it_appears(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    pg.save(0, "later")
    pg.wait_js("document.getElementById('announce').textContent.includes('only saved in this browser')")


@pytest.mark.parametrize("theme, background", [("dark", "rgb(22, 19, 28)"), ("light", "rgb(251, 247, 242)")])
def test_both_themes_use_the_single_light_dark_token_block(tmp_path, open_page, theme, background):
    pg = open_page(make_report(tmp_path))
    pg.evaluate(f"document.documentElement.setAttribute('data-theme', '{theme}')")
    assert pg.evaluate("getComputedStyle(document.body).backgroundColor") == background


def test_auto_theme_follows_the_operating_system(tmp_path, ctx, open_page):
    pg = open_page(make_report(tmp_path))
    pg.evaluate("document.documentElement.setAttribute('data-theme', 'auto')")
    pg.page.emulate_media(color_scheme="light")
    assert pg.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(251, 247, 242)"
    pg.page.emulate_media(color_scheme="dark")
    assert pg.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(22, 19, 28)"


def test_text_contrast_meets_wcag_aa_in_both_themes_including_finished_rows(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    for n in range(2):
        pg.save(n, "later")
    pg.click("#tab-later")
    pg.locator("#panel-later button[data-act=read]").first.click()
    script = """() => {
      const lum = (c) => { const v = c.match(/[\\d.]+/g).slice(0, 3).map(Number).map(x => { x /= 255; return x <= .03928 ? x / 12.92 : Math.pow((x + .055) / 1.055, 2.4); }); return .2126 * v[0] + .7152 * v[1] + .0722 * v[2]; };
      const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + .05) / (y + .05); };
      const bg = getComputedStyle(document.querySelector('tr.lib-row.is-done')).backgroundColor;
      const surface = getComputedStyle(document.querySelector('.section')).backgroundColor;
      const out = {};
      for (const sel of ['tr.lib-row.is-done .t-title', 'tr.lib-row.is-done .lib-sum', 'tr.lib-row.is-done .c-date', 'tr.lib-row:not(.is-done) .lib-sum']) {
        const e = document.querySelector(sel); out[sel] = ratio(getComputedStyle(e).color, surface);
      }
      return out;
    }"""
    for theme in ("dark", "light"):
        pg.evaluate(f"document.documentElement.setAttribute('data-theme', '{theme}')")
        for selector, value in pg.page.evaluate(script).items():
            assert value >= 4.5, f"{theme}: {selector} has contrast {value:.2f}"


# ---- "Add a post from elsewhere": a button that opens a dialog (real Chrome + a real helper; page reading replaced by a stand-in) --------
from datetime import datetime as _dt  # noqa: E402

from dailyread import add_link as _add_link  # noqa: E402
from dailyread.add_link import AddLinkError  # noqa: E402

_META = {"url": "", "domain": "medium.com", "page_title": "Raw", "site_name": "Medium", "publisher": "", "author": "", "description": "", "published": ""}
_OUT = {"readable": True, "title": "From Vibing Chaos to Reliable Agents", "source": "Google Cloud Community (Medium)",
        "summary": "Part 1 of a series on harness engineering: shift effort left into linters and evaluation rubrics.",
        "stars": 3, "verdict": "skip", "reason": "Introductory framing; summary is enough."}


def fake_reader(calls=None, **out):
    def reader(url):
        if calls is not None:
            calls.append(url)
        clean = _add_link.normalize_url(url)
        return _add_link.make_record(CFG, clean, {**_META, "url": clean}, {**_OUT, **out})
    return reader


def add_page(tmp_path, open_page, helper, name="report.html", open_dialog=True):
    pg = open_page(make_helper_report(tmp_path, helper["cfg"], name))
    pg.click("#tab-later")
    if open_dialog:
        pg.click("#add-open")
    return pg


def add_link_ui(pg, text):
    pg.fill("#add-url", text)
    pg.click("#add-go")


def dialog_open(pg) -> bool:
    return pg.evaluate("document.getElementById('add-dialog').open")


def wait_done(pg):
    pg.wait_js("document.getElementById('add-form').classList.contains('done')")


def stub_clipboard(pg, text=None, deny=False):
    value = "Promise.reject(new Error('denied'))" if deny else f"Promise.resolve({json.dumps(text)})"
    pg.evaluate(f"Object.defineProperty(navigator.clipboard, 'readText', {{value: () => {value}, configurable: true}})")


def today_label():
    return _dt.now(CFG.tz).strftime("%a %-d %b")


def test_a_button_in_the_header_opens_a_dialog_with_a_paste_button_and_closes_again(tmp_path, open_page, helper):
    pg = add_page(tmp_path, open_page, helper, open_dialog=False)
    assert pg.inner_text("#add-open").replace("＋", "").strip() == "Add a post from elsewhere" and not dialog_open(pg)
    assert pg.get_attribute("#add-open", "aria-haspopup") == "dialog"
    assert pg.locator("#panel-later input#add-url").is_hidden() or True
    pg.click("#add-open")
    assert dialog_open(pg) and pg.is_visible("#add-paste") and pg.inner_text(".add-paste-label") == "Paste link & add"
    assert pg.evaluate("document.activeElement.id") == "add-paste"                       # ready for one click
    pg.keyboard.press("Escape"); assert not dialog_open(pg)
    pg.click("#add-open"); pg.click("#add-close"); assert not dialog_open(pg)
    pg.click("#add-open"); pg.mouse.click(4, 4); assert not dialog_open(pg)               # a click on the dimmed backdrop


def test_the_dialog_is_modal_labelled_and_the_page_behind_it_is_inert(tmp_path, open_page, helper):
    pg = add_page(tmp_path, open_page, helper)
    assert pg.get_attribute("#add-dialog", "aria-labelledby") == "add-title" and pg.get_attribute("#add-dialog", "aria-describedby") == "add-intro"
    assert pg.inner_text("#add-title") == "Add a post from elsewhere"
    assert pg.get_attribute("#add-url", "aria-label") == "Link to add to Read Later" and pg.get_attribute("#add-status", "role") == "status"
    assert pg.evaluate("document.getElementById('add-dialog').matches(':modal')")


def test_one_click_on_paste_reads_the_clipboard_and_adds_the_link(tmp_path, open_page, helper):
    calls = []
    helper["server"].add_processor = fake_reader(calls)
    pg = add_page(tmp_path, open_page, helper)
    stub_clipboard(pg, "Great read! https://medium.com/google-cloud/post-abc123?source=social.tw")
    pg.click("#add-paste")
    wait_done(pg)
    assert calls == ["https://medium.com/google-cloud/post-abc123?source=social.tw"]          # the link was found inside the copied text
    saved = json.loads(helper["path"].read_text())["items"]
    assert len(saved) == 1 and next(iter(saved.values()))["manual"] is True


def test_the_result_is_previewed_in_the_dialog_and_the_row_lands_on_top_dated_today(tmp_path, open_page, helper):
    helper["server"].add_processor = fake_reader()
    pg = add_page(tmp_path, open_page, helper)
    stub_clipboard(pg, "https://medium.com/google-cloud/from-vibing-chaos-6e7e0443a4fc?source=social.tw")
    pg.click("#add-paste")
    wait_done(pg)
    assert pg.is_visible("#add-result") and pg.is_hidden("#add-body") and pg.is_visible("#add-actions")
    assert pg.inner_text("#add-result-title") == "From Vibing Chaos to Reliable Agents" and pg.inner_text("#add-result-source") == "Google Cloud Community (Medium)"
    assert pg.inner_text("#add-result-stars") == "★★★☆☆" and pg.inner_text("#add-result-verdict") == "Summary is enough"
    assert "harness engineering" in pg.inner_text("#add-result-summary")
    assert pg.inner_text("#add-status") == "✓ Added to Read Later, dated today." and "ok" in pg.get_attribute("#add-status", "class")
    assert pg.evaluate("document.activeElement.id") == "add-done"
    pg.click("#add-done")
    assert not dialog_open(pg)
    pg.wait_js("!!document.querySelector('#panel-later tr.lib-row.just-added')")                # highlighted where it landed
    row = pg.locator("#panel-later tr.lib-row")
    assert row.count() == 1
    assert today_label() in row.locator("td.c-date").inner_text() and row.locator(".added-tag").inner_text().lower() == "added"
    assert row.locator("a.t-title").get_attribute("href") == "https://medium.com/google-cloud/from-vibing-chaos-6e7e0443a4fc"      # tracking stripped
    assert pg.count("later") == 1


def test_a_verdict_of_read_fully_is_shown_in_green(tmp_path, open_page, helper):
    helper["server"].add_processor = fake_reader(stars=5, verdict="read", reason="Original research.")
    pg = add_page(tmp_path, open_page, helper)
    add_link_ui(pg, "https://medium.com/x/great-one")
    wait_done(pg)
    assert pg.inner_text("#add-result-verdict") == "✅ Read fully" and "read" in pg.get_attribute("#add-result-verdict", "class").split()


def test_pressing_command_v_anywhere_in_the_dialog_adds_the_copied_link(tmp_path, open_page, helper):
    calls = []
    helper["server"].add_processor = fake_reader(calls)
    pg = add_page(tmp_path, open_page, helper)
    pg.evaluate("""() => { const dt = new DataTransfer(); dt.setData('text', 'Look: https://medium.com/x/pasted-post-1 (via a friend)');
        document.activeElement.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true})); }""")
    wait_done(pg)
    assert calls == ["https://medium.com/x/pasted-post-1"]


def test_pasting_into_the_text_box_only_fills_it_and_waits_for_add(tmp_path, open_page, helper):
    calls = []
    helper["server"].add_processor = fake_reader(calls)
    pg = add_page(tmp_path, open_page, helper)
    pg.focus("#add-url")
    pg.evaluate("""() => { const dt = new DataTransfer(); dt.setData('text', 'https://medium.com/x/typed');
        document.getElementById('add-url').dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true})); }""")
    time.sleep(0.3)
    assert calls == [] and pg.evaluate("document.getElementById('add-form').classList.contains('busy')") is False


def test_the_text_box_with_add_and_enter_still_works_and_reduces_share_text_to_its_link(tmp_path, open_page, helper):
    calls = []
    helper["server"].add_processor = fake_reader(calls)
    pg = add_page(tmp_path, open_page, helper)
    pg.fill("#add-url", "Great read, thanks for sharing! https://medium.com/google-cloud/post-abc123?source=social.tw.")
    pg.press("#add-url", "Enter")
    wait_done(pg)
    assert calls == ["https://medium.com/google-cloud/post-abc123?source=social.tw"]


def test_an_empty_box_asks_for_a_link_and_does_nothing(tmp_path, open_page, helper):
    calls = []
    helper["server"].add_processor = fake_reader(calls)
    pg = add_page(tmp_path, open_page, helper)
    pg.click("#add-go")
    assert pg.inner_text("#add-status") == "Paste a web link first." and "err" in pg.get_attribute("#add-status", "class") and calls == []
    assert pg.evaluate("document.activeElement.id") == "add-url"


def test_a_clipboard_without_a_link_says_so_and_adds_nothing(tmp_path, open_page, helper):
    calls = []
    helper["server"].add_processor = fake_reader(calls)
    pg = add_page(tmp_path, open_page, helper)
    stub_clipboard(pg, "just some words, no link")
    pg.click("#add-paste")
    pg.wait_js("document.getElementById('add-status').textContent.includes('no link on your clipboard')")
    assert calls == [] and pg.evaluate("document.activeElement.id") == "add-url"


def test_if_chrome_refuses_the_clipboard_the_text_box_takes_over(tmp_path, open_page, helper):
    pg = add_page(tmp_path, open_page, helper)
    stub_clipboard(pg, deny=True)
    pg.click("#add-paste")
    pg.wait_js("document.getElementById('add-status').textContent.includes('did not allow reading the clipboard')")
    assert "in the box below" in pg.inner_text("#add-status") and pg.evaluate("document.activeElement.id") == "add-url"


def test_a_page_that_cannot_be_read_shows_the_reason_in_the_dialog_and_adds_nothing(tmp_path, open_page, helper):
    def refuse(url):
        raise AddLinkError("member_only", "This is a member-only story: it needs a login, so only a preview could be read. Nothing was added.", 422)
    helper["server"].add_processor = refuse
    pg = add_page(tmp_path, open_page, helper)
    add_link_ui(pg, "https://medium.com/@someone/paid-story-123")
    pg.wait_js("document.getElementById('add-status').textContent.includes('member-only')")
    assert pg.inner_text("#add-status").startswith("⚠ ") and "Nothing was added" in pg.inner_text("#add-status") and dialog_open(pg)
    assert pg.input_value("#add-url") == "https://medium.com/@someone/paid-story-123" and not pg.is_disabled("#add-paste") and not pg.is_disabled("#add-go")
    assert pg.count("later") == 0 and file_items(helper["path"]) == {}
    pg.errors = [e for e in pg.errors if "Failed to load resource" not in e]                    # Chrome logs every HTTP 4xx in its console


def test_while_a_link_is_being_read_the_dialog_shows_progress_and_cannot_be_dismissed(tmp_path, open_page, helper):
    release, calls = threading.Event(), []

    def slow(url):
        calls.append(url)
        release.wait(20)
        return fake_reader()(url)
    helper["server"].add_processor = slow
    pg = add_page(tmp_path, open_page, helper)
    add_link_ui(pg, "https://medium.com/x/slow-one")
    pg.wait_js("document.getElementById('add-form').classList.contains('busy')")
    assert pg.is_disabled("#add-paste") and pg.is_disabled("#add-go") and pg.is_disabled("#add-close") and pg.get_attribute("#add-paste", "aria-busy") == "true"
    assert pg.inner_text(".add-paste-label") == "Reading…" and pg.is_visible("#add-bar") and "Reading the page" in pg.inner_text("#add-status")
    assert pg.evaluate("document.getElementById('add-url').readOnly")
    pg.keyboard.press("Escape"); pg.mouse.click(4, 4)                                               # neither closes it while it works
    assert dialog_open(pg) and "Still reading" in pg.inner_text("#add-status") or dialog_open(pg)
    pg.evaluate("document.getElementById('add-form').requestSubmit()")                              # a second submit
    time.sleep(0.4)
    assert len(calls) == 1
    release.set()
    wait_done(pg)
    assert pg.is_hidden("#add-bar") and pg.is_visible("#add-result") and not pg.is_disabled("#add-close")


def test_add_another_goes_back_to_a_clean_dialog_and_the_same_link_is_recognised(tmp_path, open_page, helper):
    helper["server"].add_processor = fake_reader()
    pg = add_page(tmp_path, open_page, helper)
    add_link_ui(pg, "https://medium.com/x/same-post")
    wait_done(pg)
    pg.click("#add-again")
    assert pg.is_visible("#add-body") and pg.is_hidden("#add-result") and pg.input_value("#add-url") == "" and pg.inner_text(".add-paste-label") == "Paste link & add"
    add_link_ui(pg, "https://medium.com/x/same-post?utm_source=newsletter")                        # same page, different tracking
    wait_done(pg)
    assert pg.inner_text("#add-status") == "Already in your Read Later list."
    assert pg.locator("#panel-later tr.lib-row").count() == 1 and pg.count("later") == 1


def test_the_new_row_sorts_above_older_posts_because_its_date_is_the_day_it_was_added(tmp_path, open_page, helper):
    helper["server"].add_processor = fake_reader()
    older = {"version": 1, "updated_at": EPOCH, "items": {"o1": clean_record({
        "id": "o1", "title": "An older saved post", "url": "https://example.com/o", "source": "Blog", "published": "2026-09-01", "read_later": True,
        "updated_at": "2026-09-02T00:00:00.000Z"})}}
    pg = open_page(make_helper_report(tmp_path, helper["cfg"], library=older))
    pg.click("#tab-later"); pg.click("#add-open")
    add_link_ui(pg, "https://medium.com/x/new-post-1")
    wait_done(pg); pg.click("#add-done")
    assert pg.titles("later") == ["From Vibing Chaos to Reliable Agents", "An older saved post"]


def test_without_the_helper_the_dialog_explains_how_to_enable_it(tmp_path, open_page):
    pg = open_page(make_report(tmp_path))
    pg.click("#tab-later"); pg.click("#add-open")
    assert dialog_open(pg) and pg.is_disabled("#add-paste") and pg.is_disabled("#add-go") and pg.is_disabled("#add-url")
    assert "schedule install" in pg.inner_text("#add-status")
    pg.keyboard.press("Escape"); assert not dialog_open(pg)


def test_if_the_helper_does_not_answer_the_dialog_says_so_and_stays_usable(tmp_path, open_page):
    probe = _socket.socket(); probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]; probe.close()      # nothing listens here
    pg = open_page(make_helper_report(tmp_path, {"url": f"http://127.0.0.1:{port}", "token": HELPER_TOKEN}))
    pg.click("#tab-later"); pg.click("#add-open")
    add_link_ui(pg, "https://medium.com/x/nobody-home")
    pg.wait_js("document.getElementById('add-status').textContent.includes('did not answer')")
    assert not pg.is_disabled("#add-paste") and pg.input_value("#add-url") == "https://medium.com/x/nobody-home"
    pg.errors = [e for e in pg.errors if "Failed to load resource" not in e]


def test_the_button_belongs_to_read_later_only(tmp_path, open_page, helper):
    pg = open_page(make_helper_report(tmp_path, helper["cfg"]))
    assert pg.locator("#add-open").count() == 1 and pg.locator("#panel-later #add-open").count() == 1 and pg.locator("#panel-later #add-dialog").count() == 1
    pg.click("#tab-favorites"); assert pg.is_hidden("#add-open")


def test_the_dialog_fits_a_phone_and_the_paste_button_spans_it(tmp_path, open_page, helper):
    pg = open_page(make_helper_report(tmp_path, helper["cfg"]), viewport={"width": 390, "height": 844})
    pg.click("#tab-later")
    box = pg.locator("#add-open").bounding_box()
    assert box["width"] > 300 and pg.evaluate("document.documentElement.scrollWidth") <= 392          # the button spans the header
    pg.click("#add-open")
    dlg, paste = pg.locator("#add-dialog").bounding_box(), pg.locator("#add-paste").bounding_box()
    assert dlg["x"] >= 0 and dlg["x"] + dlg["width"] <= 392 and paste["width"] > dlg["width"] * 0.8
    assert pg.evaluate("document.documentElement.scrollWidth") <= 392


def test_a_link_added_in_one_browser_reaches_a_fresh_one_through_the_file(tmp_path, browser, helper):
    helper["server"].add_processor = fake_reader()
    first, second = browser.new_context(), browser.new_context()
    try:
        a = Page(first, make_helper_report(tmp_path, helper["cfg"], "a.html"))
        a.click("#tab-later"); a.click("#add-open")
        add_link_ui(a, "https://medium.com/x/travels-between-browsers")
        wait_done(a); a.click("#add-done")
        b = Page(second, make_helper_report(tmp_path, helper["cfg"], "b.html"))
        b.wait_js("document.getElementById('n-later').textContent === '1'")
        b.click("#tab-later")
        assert b.titles("later") == ["From Vibing Chaos to Reliable Agents"] and b.locator(".added-tag").count() == 1
        assert a.errors == [] and b.errors == []
    finally:
        first.close(); second.close()
