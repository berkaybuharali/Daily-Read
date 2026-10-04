"""Does a report opened from disk (file://) in STOCK Chrome save through the localhost helper with no prompt?

Why this exists: Chrome has been tightening what web pages may do towards local addresses (Local Network Access, Chrome
142+; loopback split in 145; more in later releases). Playwright's own Chrome launch turns Chrome's staged rollouts off
(`--disable-field-trial-config`), so the normal UI tests could miss a new prompt. This test starts Google Chrome the way
a user does (fresh temporary profile, none of Playwright's launch flags) and only talks to it over the debugging port.

Run it after every Chrome major update:  uv run --group dev --group ui pytest tests/test_stock_chrome.py -v
If it fails, Chrome now blocks or prompts for file:// -> 127.0.0.1. Reports then fall back to browser storage with the
reminder bar (nothing is lost); the fix would be serving reports from the helper over http://127.0.0.1 (same origin)."""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import sync_playwright  # noqa: E402

from dailyread.config import load_config  # noqa: E402
from dailyread.library import EPOCH  # noqa: E402
from dailyread.library_helper import Server  # noqa: E402
from dailyread.render import render_report  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(not Path(CHROME).exists(), reason="Google Chrome is not installed")
def test_stock_chrome_saves_through_the_helper_without_any_prompt():
    tmp = Path(tempfile.mkdtemp(prefix="dr-stock-"))
    lib = tmp / "state" / "library.json"
    srv = Server(("127.0.0.1", 0), lib, "stock-token", 3600)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    report = json.loads((ROOT / "fixtures" / "sample_report.json").read_text())
    page = tmp / "report.html"
    page.write_text(render_report(load_config(), report, library={"version": 1, "updated_at": EPOCH, "items": {}},
                                  helper={"url": f"http://127.0.0.1:{srv.server_address[1]}", "token": "stock-token"}))
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    proc = subprocess.Popen([CHROME, "--headless=new", f"--user-data-dir={tmp / 'profile'}", f"--remote-debugging-port={port}",
                             "--no-first-run", "--no-default-browser-check", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                version = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1))["Browser"]
                break
            except OSError:
                time.sleep(0.5)
        else:
            pytest.skip("Chrome did not start")
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            pg = browser.contexts[0].new_page()
            pg.goto(page.as_uri())
            pg.locator("#panel-today tr.item").first.locator('button.act[data-act="later"]').click()
            deadline = time.time() + 12
            while time.time() < deadline and not (lib.exists() and json.loads(lib.read_text())["items"]):
                time.sleep(0.2)
            saved = lib.exists() and bool(json.loads(lib.read_text())["items"])
            banner = pg.locator("#lib-banner").is_visible()
            status = pg.inner_text("#panel-later .lib-status")
            browser.close()
        assert saved, f"{version}: the click never reached state/library.json (Chrome blocked or is prompting?)"
        assert not banner and "Saved automatically" in status, f"{version}: the page fell back ({status})"
    finally:
        proc.terminate()
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(tmp, ignore_errors=True)
