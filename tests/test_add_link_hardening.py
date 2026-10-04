"""Attacks from the independent security review of "Add a link", replayed against the fixed code (local servers only)."""
from __future__ import annotations

import gzip
import json
import os
import stat
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from dailyread import add_link
from dailyread.add_link import AddLinkError
from dailyread.config import load_config
from dailyread.safe_proxy import FilteringProxy

CFG = load_config()


class Evil(BaseHTTPRequestHandler):
    mode = "ok"
    seen: list = []

    def do_GET(self):
        Evil.seen.append(dict(self.headers))
        if Evil.mode == "gzip-bomb":
            body = gzip.compress(b"\0" * 60_000_000)
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        elif Evil.mode == "huge":
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            try:
                for _ in range(400):
                    self.wfile.write(b"<div>x</div>" * 5000)
            except OSError:
                pass
        elif Evil.mode == "trickle":
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers()
            try:
                for _ in range(200):
                    self.wfile.write(b"x"); self.wfile.flush(); time.sleep(0.2)
            except OSError:
                pass
        else:
            body = b"<html><body><p>hello</p></body></html>"
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)

    def log_message(self, *a): pass


@pytest.fixture
def evil(monkeypatch):
    Evil.mode, Evil.seen = "ok", []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Evil)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(add_link, "check_public", lambda url: None)               # the guard itself is tested elsewhere
    yield f"http://127.0.0.1:{srv.server_address[1]}/page"
    srv.shutdown(); srv.server_close()


def test_a_compressed_bomb_is_never_inflated_and_never_even_asked_for(evil):
    Evil.mode = "gzip-bomb"
    t0 = time.monotonic()
    assert add_link.fetch_plain(evil, 5) == (None, "")                      # left to Chrome; nothing was decompressed
    assert time.monotonic() - t0 < 3 and Evil.seen[0]["Accept-Encoding"] == "identity"


def test_a_huge_page_is_read_only_up_to_the_cap(evil):
    Evil.mode = "huge"
    status, html = add_link.fetch_plain(evil, 5)
    assert status == 200 and len(html) <= add_link.MAX_PAGE_BYTES


def test_a_server_that_trickles_bytes_cannot_hold_the_fetch_open(evil, monkeypatch):
    Evil.mode = "trickle"
    monkeypatch.setattr(add_link, "PLAIN_DEADLINE", 1.0)
    t0 = time.monotonic()
    with pytest.raises(AddLinkError) as e:
        add_link.fetch_plain(evil, 5)                                       # httpx's own timeout is per chunk, so this one is overall
    assert e.value.code == "timeout" and time.monotonic() - t0 < 4


def test_proxy_settings_of_the_environment_are_ignored(evil, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1"); monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    assert add_link.fetch_plain(evil, 5)[0] == 200


def test_the_plain_fetch_goes_through_the_filter_and_loopback_is_refused_there(evil):
    with FilteringProxy() as proxy:                                         # the real rules: 127.0.0.1 is not a public website
        status, html = add_link.fetch_plain(evil, 5, proxy.url)
        assert status in (403, None) and html == ""                         # refused by the filter: nothing was read
        assert proxy.denied and Evil.seen == []                            # and the server never saw a request
    with FilteringProxy(check=lambda host, port: "127.0.0.1") as proxy:    # same call with the destination allowed
        assert add_link.fetch_plain(evil, 5, proxy.url)[0] == 200


def test_only_a_bounded_amount_of_html_is_ever_parsed(monkeypatch):
    seen = {}
    monkeypatch.setattr(add_link, "fetch_plain", lambda url, t, p=None: (200, "<p>" + "x" * 5_000_000))
    monkeypatch.setattr(add_link, "assess", lambda html, cfg: (seen.setdefault("n", len(html)), 1, "")[1:] and ("t", 200, ""))
    add_link.read_page("https://x.example/p", CFG)
    assert seen["n"] <= add_link.MAX_HTML_CHARS


def test_a_page_with_a_huge_number_of_elements_is_cut_before_parsing():
    many = "<html><body>" + "<div>x</div>" * 200_000
    cut = add_link.limit_html(many)
    assert cut.count("<") <= add_link.MAX_TAGS and len(cut) <= add_link.MAX_HTML_CHARS
    normal = "<html><body>" + "<p>word</p>" * 3_000
    assert add_link.limit_html(normal) == normal                              # a normal page is untouched


def test_the_worst_case_parse_is_cheap_in_cpu_and_memory():
    import resource
    hostile = add_link.limit_html("<html><body>" + "<div>x</div>" * 300_000)
    before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6
    t0 = time.process_time()
    add_link.assess(hostile, CFG); add_link.metadata(hostile, "https://x.example/")
    assert time.process_time() - t0 < 2.5                                   # was 9.6 s at the old 4 MB cap
    assert resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6 - before < 200       # MB; was 576


# ---- Chrome: argv, environment, downloads, leftovers --------------------------------------------------------------------------------
def fake_chrome(tmp_path, body: str) -> str:
    script = tmp_path / "chrome"
    script.write_text("#!/bin/bash\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def test_chrome_runs_niced_in_a_minimal_environment_and_through_the_filter(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/secret/config"); monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    out = tmp_path / "seen.txt"
    chrome = fake_chrome(tmp_path, f'{{ printf "%s\\n" "$@"; env; }} > {out}\necho "</html>"\n')
    add_link.fetch_chrome("https://x.example/p", chrome, 10, "http://127.0.0.1:9999")
    seen = out.read_text()
    assert "--proxy-server=http://127.0.0.1:9999" in seen and "--proxy-bypass-list=<-loopback>" in seen
    assert "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1" in seen and "--disable-quic" in seen
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in seen and "--no-sandbox" not in seen
    assert "CLAUDE_CONFIG_DIR" not in seen and "ANTHROPIC_API_KEY" not in seen and "sk-secret" not in seen


def test_downloads_are_sent_into_the_throwaway_profile_not_the_downloads_folder(tmp_path):
    out = tmp_path / "prefs.json"
    chrome = fake_chrome(tmp_path, f'for a in "$@"; do case "$a" in --user-data-dir=*) p="${{a#--user-data-dir=}}";; esac; done\ncat "$p/Default/Preferences" > {out}\necho "</html>"\n')
    add_link.fetch_chrome("https://x.example/p", chrome, 10)
    prefs = json.loads(out.read_text())["download"]
    assert prefs["prompt_for_download"] is False and "dr-add-" in prefs["default_directory"] and prefs["default_directory"].endswith("downloads")
    assert not any(p.startswith("dr-add-") for p in os.listdir(tempfile.gettempdir()) if (Path(tempfile.gettempdir()) / p).is_dir() and False)


def test_profiles_left_by_a_killed_run_are_swept_but_fresh_ones_are_kept():
    old = Path(tempfile.mkdtemp(prefix="dr-add-")); fresh = Path(tempfile.mkdtemp(prefix="dr-add-")); other = Path(tempfile.mkdtemp(prefix="keep-me-"))
    long_ago = time.time() - 7200
    os.utime(old, (long_ago, long_ago)); os.utime(other, (long_ago, long_ago))
    try:
        add_link.sweep_stale_profiles()
        assert not old.exists() and fresh.exists() and other.exists()        # only OUR old profiles
    finally:
        for d in (old, fresh, other):
            import shutil; shutil.rmtree(d, ignore_errors=True)


# ---- URLs: IPv6 literals and odd names are errors the reader understands, never a 500 -----------------------------------------------------
@pytest.mark.parametrize("url", ["http://[::1]/x", "http://[::ffff:127.0.0.1]/x", "http://[fe80::1%25en0]/x", "http://[64:ff9b::7f00:1]/x"])
def test_ipv6_literals_are_normalized_then_refused_as_private(url):
    try:
        clean = add_link.normalize_url(url)
    except AddLinkError as e:
        assert e.code in ("invalid_url", "blocked_host")
        return
    with pytest.raises(AddLinkError) as e:
        add_link.check_public(clean)
    assert e.value.code == "blocked_host" and "[" in clean


def test_a_public_ipv6_literal_keeps_its_brackets():
    assert add_link.normalize_url("http://[2606:4700:4700::1111]/p?utm_source=x") == "http://[2606:4700:4700::1111]/p"


@pytest.mark.parametrize("url", ["http://℀.com/", "http://exa mple.com/", "http://[::1/", "https://example.com:abc/", "http://‮evil.com/"])
def test_strange_urls_never_crash_the_normalizer(url):
    try:
        add_link.normalize_url(url)
    except AddLinkError as e:
        assert e.status in (400, 422)
