"""Read Later -> "Add a link": URL cleaning, the network-safety rules, page reading, readability checks, the record, and the
Claude call (faked). Nothing here touches the network, Chrome or Claude."""
from __future__ import annotations

import os
import socket
import stat
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from zoneinfo import ZoneInfo

import pytest

from dailyread import add_link
from dailyread.add_link import AddLinkError
from dailyread.config import load_config

CFG = load_config()
AMS = ZoneInfo("Europe/Amsterdam")
MEDIUM = "https://medium.com/google-cloud/from-vibing-chaos-to-reliable-agents-the-what-why-and-how-of-harness-engineering-6e7e0443a4fc"


def err_code(fn, *args):
    with pytest.raises(AddLinkError) as e:
        fn(*args)
    return e.value.code


# ---- 1. the URL -----------------------------------------------------------------------------------------------------------
def test_share_links_lose_their_tracking_but_keep_what_unlocks_the_page():
    assert add_link.normalize_url(MEDIUM + "?source=social.tw&utm_medium=x&utm_campaign=y#section") == MEDIUM
    assert add_link.normalize_url("https://x.dev/p?id=7&fbclid=abc&sk=FRIENDTOKEN&utm_source=n") == "https://x.dev/p?id=7&sk=FRIENDTOKEN"


def test_pasted_share_text_and_bare_domains_are_understood():
    assert add_link.normalize_url("Great read! " + MEDIUM + " (via a friend).") == MEDIUM
    assert add_link.normalize_url("  medium.com/@someone/post-abc123  ") == "https://medium.com/@someone/post-abc123"
    assert add_link.normalize_url("HTTPS://Example.COM./Path") == "https://example.com/Path"
    assert add_link.first_url("see https://a.dev/x, and https://b.dev") == "https://a.dev/x"


@pytest.mark.parametrize("bad", ["", "   ", "javascript:alert(1)", "file:///etc/passwd", "ftp://example.com/x", "data:text/html,hi",
                                 "https://user:pass@example.com/x", "x" * 2100])
def test_things_that_are_not_public_web_links_are_refused(bad):
    assert err_code(add_link.normalize_url, bad) in ("invalid_url", "blocked_host")


@pytest.mark.parametrize("url", ["http://example.com:8080/x", "https://example.com:22/x", "https://example.com:99999/x"])
def test_only_normal_web_ports_are_allowed(url):
    assert err_code(add_link.normalize_url, url) in ("blocked_host", "invalid_url")


# ---- 2. the network-safety rule: never your own computer or network ------------------------------------------------------------
def resolve_to(monkeypatch, *addresses):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443)) for a in addresses])


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "192.168.1.20", "172.16.5.5", "169.254.169.254", "0.0.0.0", "::1", "fe80::1",
                                "fc00::1", "::ffff:127.0.0.1", "100.64.0.1", "224.0.0.1"])
def test_private_loopback_and_metadata_addresses_are_blocked(monkeypatch, ip):
    resolve_to(monkeypatch, ip)
    assert err_code(add_link.check_public, "https://innocent.example/post") == "blocked_host"


def test_a_name_that_resolves_to_a_mix_is_blocked_if_any_address_is_private(monkeypatch):
    resolve_to(monkeypatch, "93.184.216.34", "10.0.0.5")                 # DNS rebinding style answer
    assert err_code(add_link.check_public, "https://mixed.example/") == "blocked_host"


def test_public_addresses_pass_and_unknown_names_are_reported(monkeypatch):
    resolve_to(monkeypatch, "93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946")
    add_link.check_public("https://example.com/")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(socket.gaierror("nope")))
    assert err_code(add_link.check_public, "https://no-such-site.invalid/") == "unreachable"


def test_every_redirect_hop_is_checked(monkeypatch):
    """A public page that redirects to your router or a cloud metadata address must be refused at that hop."""
    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "https://internal.example/admin")
            self.end_headers()
        def log_message(self, *a): pass
    srv = HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    seen = []

    def fake_check(url):
        seen.append(url)
        if "internal.example" in url:
            raise AddLinkError("blocked_host", "own network", 400)
    monkeypatch.setattr(add_link, "check_public", fake_check)
    try:
        with pytest.raises(AddLinkError) as e:
            add_link.fetch_plain(f"http://127.0.0.1:{srv.server_address[1]}/start", 5)
    finally:
        srv.shutdown(); srv.server_close()
    assert e.value.code == "blocked_host" and any("internal.example" in u for u in seen) and len(seen) == 2


# ---- 3. readability: what is NOT an article -------------------------------------------------------------------------------------
ARTICLE = "<html><head><title>A post</title></head><body><article>" + "<p>" + ("This sentence is real article text. " * 60) + "</p></article></body></html>"


def assess(html):
    return add_link.assess(html, CFG)[2]


def test_a_real_article_is_readable():
    text, words, problem = add_link.assess(ARTICLE, CFG)
    assert problem == "" and words >= 150


def test_cloudflare_and_bot_check_pages_are_recognised():
    block = "<html><head><title>Attention Required! | Cloudflare</title></head><body><p>Sorry, you have been blocked.</p></body></html>"
    assert assess(block) == "blocked"
    assert assess("<html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>") == "blocked"


def test_member_only_stories_and_login_walls_are_refused_not_summarized_from_a_preview():
    preview = "<html><head><title>x</title></head><body><span>Member-only story</span><article><p>" + ("Some preview words here. " * 40) + "</p></article></body></html>"
    assert assess(preview) == "member_only"
    wall = "<html><head><title>x</title></head><body><article><p>Sign in to continue reading this article. " + ("filler words " * 40) + "</p></article></body></html>"
    assert assess(wall) == "login"


def test_pages_with_hardly_any_text_are_refused():
    assert assess("<html><head><title>Video</title></head><body><p>Watch now</p></body></html>") == "short"
    assert assess("") == "short"


def test_a_long_article_that_mentions_member_only_in_passing_is_still_read():
    long_article = ARTICLE.replace("</article>", "<p>Medium has a Member-only story label.</p>" + "<p>" + "More real content words. " * 400 + "</p></article>")
    assert assess(long_article) == ""


# ---- reading the page: plain first, Chrome when blocked -------------------------------------------------------------------------
def test_a_readable_plain_page_never_starts_chrome(monkeypatch):
    monkeypatch.setattr(add_link, "fetch_plain", lambda url, t, p=None: (200, ARTICLE))
    monkeypatch.setattr(add_link, "fetch_chrome", lambda *a: pytest.fail("Chrome must not start for a normal page"))
    assert add_link.read_page("https://blog.example/post", CFG)[2] == "plain"


def test_a_blocked_page_falls_back_to_chrome(monkeypatch):
    monkeypatch.setattr(add_link, "fetch_plain", lambda url, t, p=None: (403, ""))
    monkeypatch.setattr(add_link, "fetch_chrome", lambda url, chrome, t, p=None: ARTICLE)
    html, text, via = add_link.read_page("https://medium.com/x/y", CFG)
    assert via == "chrome" and len(text.split()) >= 150


@pytest.mark.parametrize("plain, chrome, code", [
    ((403, ""), "<html><head><title>Attention Required! | Cloudflare</title></head></html>", "blocked"),
    ((200, "<html><body><span>Member-only story</span><article><p>" + "preview text " * 50 + "</p></article></body></html>"), "", "member_only"),
    ((None, ""), "", "unreachable"),
])
def test_unreadable_pages_give_a_plain_error_and_nothing_is_added(monkeypatch, plain, chrome, code):
    monkeypatch.setattr(add_link, "fetch_plain", lambda url, t, p=None: plain)
    monkeypatch.setattr(add_link, "fetch_chrome", lambda url, c, t, p=None: chrome)
    with pytest.raises(AddLinkError) as e:
        add_link.read_page("https://x.example/p", CFG)
    assert e.value.status == 422 and "nothing was added" in e.value.message.lower()
    assert code in e.value.code or e.value.code in ("blocked", "member_only", "unreachable", "short", "login")


def fake_chrome(tmp_path, body: str) -> str:
    script = tmp_path / "chrome"
    script.write_text("#!/bin/bash\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def chrome_dirs() -> set[str]:
    import tempfile
    return {p for p in os.listdir(tempfile.gettempdir()) if p.startswith("dr-add-")}


def test_chrome_is_stopped_as_soon_as_the_page_is_printed_and_cleaned_up(tmp_path):
    before = chrome_dirs()
    chrome = fake_chrome(tmp_path, 'echo "<html><body>hello page</body></html>"\nsleep 61.7\n')     # like real Chrome: prints, then lingers
    t0 = time.monotonic()
    html = add_link.fetch_chrome("https://x.example/p", chrome, 20)
    assert "hello page" in html and time.monotonic() - t0 < 5
    assert chrome_dirs() == before                                          # the throwaway profile is gone
    time.sleep(0.3)
    assert os.system("pgrep -f 'sleep 61[.]7' >/dev/null") != 0             # and the process group was killed (the [.] keeps pgrep from matching its own shell)


def test_chrome_that_hangs_is_killed_at_the_timeout(tmp_path):
    chrome = fake_chrome(tmp_path, 'echo "<html><body>partial"\nsleep 62.3\n')
    t0 = time.monotonic()
    html = add_link.fetch_chrome("https://x.example/p", chrome, 2)
    assert 1.5 < time.monotonic() - t0 < 6 and "partial" in html


def test_missing_chrome_is_a_plain_error(tmp_path):
    assert err_code(add_link.fetch_chrome, "https://x.example/p", str(tmp_path / "nothing"), 5) == "no_chrome"


def test_chrome_gets_no_extensions_a_throwaway_profile_and_the_given_url(tmp_path):
    argfile = tmp_path / "args.txt"
    chrome = fake_chrome(tmp_path, f'printf "%s\\n" "$@" > {argfile}\necho "</html>"\n')
    add_link.fetch_chrome("https://x.example/p?q=1", chrome, 10)
    args = argfile.read_text().split("\n")
    assert "--headless=new" in args and "--disable-extensions" in args and "https://x.example/p?q=1" in args
    assert any(a.startswith("--user-data-dir=/") and "dr-add-" in a for a in args) and "--no-sandbox" not in args


# ---- 4. what the page says about itself ---------------------------------------------------------------------------------------
MEDIUM_HTML = """<html><head><title>From Vibing Chaos | by Dazbo (Darren Lester) | Google Cloud - Community | Sep, 2026 | Medium</title>
<meta property="og:title" content="From Vibing Chaos to Reliable Agents"><meta property="og:site_name" content="Medium">
<meta name="author" content="Dazbo (Darren Lester)"><meta property="article:published_time" content="2026-09-28T09:59:24.262Z">
<meta property="og:description" content="You cannot ship vibes to production.">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"Article","publisher":{"@type":"Organization","name":"Google Cloud - Community"},
"datePublished":"2026-09-28T09:59:24.262Z","author":{"name":"Dazbo (Darren Lester)"}}]}</script></head><body></body></html>"""


def test_title_site_author_publisher_and_date_come_from_the_page_metadata():
    m = add_link.metadata(MEDIUM_HTML, MEDIUM)
    assert m["page_title"] == "From Vibing Chaos to Reliable Agents" and m["site_name"] == "Medium"
    assert m["author"] == "Dazbo (Darren Lester)" and m["publisher"] == "Google Cloud - Community"
    assert m["published"].startswith("2026-09-28") and m["domain"] == "medium.com"
    assert "Dazbo" in m["html_title"]                                         # the raw title still carries the publication


def test_a_page_without_metadata_still_gets_a_domain():
    m = add_link.metadata("<html><body>bare</body></html>", "https://www.plain.example/post")
    assert m["domain"] == "plain.example" and m["page_title"] == "" and m["author"] == ""


def test_broken_json_ld_is_ignored():
    m = add_link.metadata('<html><head><script type="application/ld+json">{not json</script></head></html>', "https://a.example/")
    assert m["publisher"] == ""


# ---- 5. the record and the Claude call (faked) ------------------------------------------------------------------------------------
GOOD = {"readable": True, "title": "From Vibing Chaos to Reliable Agents", "source": "Google Cloud Community (Medium)",
        "summary": "Part 1 of a series on harness engineering.", "stars": 3, "verdict": "skip", "reason": "Introductory framing; summary is enough."}
META = {"url": MEDIUM, "domain": "medium.com", "page_title": "Raw title", "site_name": "Medium", "publisher": "", "author": "", "description": "", "published": ""}


def test_the_record_is_dated_the_day_it_was_added_not_the_post_date():
    rec = add_link.make_record(CFG, MEDIUM, {**META, "published": "2020-01-01T09:59:24Z"}, GOOD, now=datetime(2026, 10, 1, 21, 30, tzinfo=AMS))
    assert rec["published"] == "2026-10-01" and rec["manual"] is True and rec["read_later"] and not rec["favorite"] and not rec["read"]
    assert rec["source"] == "Google Cloud Community (Medium)" and rec["url"] == MEDIUM and rec["report"] == ""
    late = add_link.make_record(CFG, MEDIUM, META, GOOD, now=datetime(2026, 10, 2, 0, 30, tzinfo=AMS))
    assert late["published"] == "2026-10-02"                                  # the LOCAL date, although it is still 1 Oct in UTC
    assert late["added_at"].startswith("2026-10-01T22:30")                    # while stored timestamps are UTC


@pytest.mark.parametrize("stars, verdict", [(1, "skip"), (3, "skip"), (4, "read"), (5, "read")])
def test_four_and_five_stars_mean_read_fully_whatever_the_model_says(stars, verdict):
    rec = add_link.make_record(CFG, MEDIUM, META, {**GOOD, "stars": stars, "verdict": "skip" if stars >= 4 else "read"})
    assert rec["verdict"] == verdict and rec["stars"] == stars


def test_the_same_link_always_gets_the_same_id_and_other_links_differ():
    assert add_link.record_id(MEDIUM) == add_link.record_id(MEDIUM) != add_link.record_id(MEDIUM + "2")


def test_missing_model_fields_fall_back_to_the_page_not_to_nothing():
    rec = add_link.make_record(CFG, MEDIUM, META, {**GOOD, "title": "", "source": ""})
    assert rec["title"] == "Raw title" and rec["source"] == "Medium"


def test_an_overlong_summary_is_cut_cleanly():
    rec = add_link.make_record(CFG, MEDIUM, META, {**GOOD, "summary": "A sentence. " * 80})
    assert len(rec["summary"]) <= 300


class FakeClaude:
    calls: list = []

    def __init__(self, cfg, usage): self.out = FakeClaude.reply
    def prompt(self, *names): FakeClaude.calls.append(("prompt", names)); return "SYSTEM"
    def schema(self, name): return {"name": name}
    def call(self, stage, system, payload, schema, retries=None, timeout=None):
        FakeClaude.calls.append((stage, payload, schema, retries, timeout))
        if isinstance(self.out, Exception):
            raise self.out
        return self.out


def test_claude_is_called_with_the_page_text_as_data_and_the_add_link_stage(monkeypatch):
    import dailyread.llm as llm
    FakeClaude.calls, FakeClaude.reply = [], GOOD
    monkeypatch.setattr(llm, "Claude", FakeClaude)
    out = add_link.summarize(CFG, META, "the article text")
    assert out["stars"] == 3
    prompt, call = FakeClaude.calls
    assert prompt[1][0] == "stages/add_link.md" and "verdict_criteria.md" in prompt[1] and "profile.md" in prompt[1]
    assert call[0] == "add_link" and call[1]["text"] == "the article text" and call[1]["domain"] == "medium.com" and call[2] == {"name": "add_link"}


def test_claude_errors_become_plain_messages(monkeypatch):
    import dailyread.llm as llm
    monkeypatch.setattr(llm, "Claude", FakeClaude)
    FakeClaude.reply = llm.LlmError("You've hit your session limit, resets 3pm")
    assert err_code(add_link.summarize, CFG, META, "t") == "claude_limit"
    FakeClaude.reply = llm.LlmError("exit 1: boom")
    assert err_code(add_link.summarize, CFG, META, "t") == "claude_error"
    FakeClaude.reply = {**GOOD, "readable": False}
    assert err_code(add_link.summarize, CFG, META, "t") == "unreadable"


def test_the_whole_chain_with_every_outside_step_faked(monkeypatch):
    import dailyread.llm as llm
    monkeypatch.setattr(add_link, "check_public", lambda url: None)            # no real DNS in a unit test
    monkeypatch.setattr(add_link, "fetch_plain", lambda url, t, p=None: (403, ""))
    monkeypatch.setattr(add_link, "fetch_chrome", lambda url, c, t, p=None: MEDIUM_HTML.replace("<body></body>", "<body><article><p>" + "Real article words here. " * 80 + "</p></article></body>"))
    monkeypatch.setattr(llm, "Claude", FakeClaude)
    FakeClaude.calls, FakeClaude.reply = [], GOOD
    rec = add_link.process(CFG, MEDIUM + "?source=social.tw")
    assert rec["url"] == MEDIUM and rec["manual"] and rec["source"] == "Google Cloud Community (Medium)"
    payload = FakeClaude.calls[-1][1]
    assert payload["publisher"] == "Google Cloud - Community" and payload["author"] == "Dazbo (Darren Lester)" and payload["text"]


def test_the_stage_exists_in_the_config_prompts_and_schema():
    assert CFG.claude["models"]["add_link"] and CFG.claude["effort"]["add_link"]
    for key in ("max_words", "min_words", "fetch_timeout", "chrome_timeout", "claude_timeout", "chrome"):
        assert key in CFG.raw["add_link"]
    assert (CFG.prompts_dir / "stages" / "add_link.md").exists()
    import json
    schema = json.loads((CFG.schemas_dir / "add_link.json").read_text())
    assert schema["properties"]["summary"]["maxLength"] <= 300 and set(schema["required"]) >= {"readable", "title", "source", "summary", "stars", "verdict"}
