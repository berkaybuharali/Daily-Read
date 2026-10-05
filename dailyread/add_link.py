"""Add a link to Read Later: read a page you found elsewhere, summarize and rate it, derive its source.

Runs inside the localhost helper (POST /add) when you paste a link into the Read Later tab. Steps:
1. clean the URL (drop tracking parameters) and refuse anything that is not a public http(s) website;
2. read the page: a plain fetch first (fast), then the Chrome you already have, headless, for sites that block scripts
   (Medium answers a script with a Cloudflare block page, a real browser connection gets through);
3. refuse pages that are not readable (login wall, bot check, member-only story, too little text);
4. one locked-down `claude -p` call (same runner and rules as the daily digest: no tools, page text only as data) returns
   a clean title, the source, a summary, stars and the verdict;
5. build the Read Later record. Its date is the day it was ADDED, so it sorts to the top.

Nothing is sent to a third-party reader or proxy. Heavy imports (httpx, trafilatura, Claude runner) happen only here, so
the helper stays small until a link is added."""
from __future__ import annotations

import json
import logging
import os
import re
import select
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from .config import Config
from .library import clean_record
from .safe_proxy import Denied, FilteringProxy, resolve_public

log = logging.getLogger(__name__)

MAX_URL = 2000
MAX_PAGE_BYTES = 2_000_000       # plain fetch: read no more than this (never decompress: see fetch_plain)
MAX_DOM_BYTES = 3_000_000        # headless Chrome: the printed page
MAX_HTML_CHARS = 1_000_000       # what is parsed at all: a hostile page must not cost seconds of CPU or gigabytes of memory
MAX_TAGS = 25_000                # ...and cost grows with the NUMBER of elements, so they are budgeted too (real pages: 2,000-15,000)
PLAIN_DEADLINE = 40              # seconds for the whole plain fetch including redirects (a trickling server cannot hold us)
MAX_REDIRECTS = 5
TRACKING_PARAMS = {"source", "ref", "ref_src", "ref_url", "fbclid", "gclid", "dclid", "msclkid", "yclid", "mc_cid", "mc_eid",
                   "si", "igshid", "_hsenc", "_hsmi", "mkt_tok", "ck_subscriber_id", "s", "t", "feature", "spm"}
# NOT stripped: `sk` (a Medium "friend link" token that unlocks a member-only story), and everything unknown.
BLOCK_PAGE = re.compile(r"attention required|just a moment|checking your browser|enable javascript and cookies|"
                        r"verify you are (a )?human|are you a robot|captcha|access denied|request blocked", re.I)
LOGIN_WALL = re.compile(r"sign in to (continue|read)|log in to (continue|read)|create a free account to (read|continue)|"
                        r"subscribe to (continue|read)|this content is for subscribers", re.I)
STALE_PROFILE_AGE = 3600         # a Chrome profile older than this was left by a killed run: sweep it
MEMBER_ONLY = re.compile(r"member-only story", re.I)
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/154.0.0.0 Safari/537.36")


class AddLinkError(Exception):
    """A problem the reader should see as a plain sentence. `code` is stable for the page and tests."""

    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


# ---- 1. the URL ------------------------------------------------------------------------------------------------------
def first_url(text: str) -> str:
    """The first http(s) link in pasted text (a share message is often 'Title https://…')."""
    m = re.search(r"https?://[^\s<>\"']+", text or "")
    return m.group(0).rstrip(".,;)") if m else (text or "").strip()


def normalize_url(raw: str) -> str:
    raw = first_url(raw)
    if not raw or len(raw) > MAX_URL:
        raise AddLinkError("invalid_url", "That does not look like a web link.", 400)
    if "://" not in raw:
        raw = "https://" + raw                           # "medium.com/…" pasted without the scheme
    try:
        parts = urlsplit(raw)
        host, port, user, password = parts.hostname, parts.port, parts.username, parts.password
    except ValueError:
        raise AddLinkError("invalid_url", "That does not look like a valid web link.", 400)
    if parts.scheme not in ("http", "https") or not host:
        raise AddLinkError("invalid_url", "Only http and https links can be added.", 400)
    if "%" in host:
        raise AddLinkError("blocked_host", "That address is on your own computer or network, so it was not opened.", 400)   # IPv6 zone id
    if user or password:
        raise AddLinkError("invalid_url", "Links with a user name or password are not accepted.", 400)
    if port not in (None, 80, 443):
        raise AddLinkError("blocked_host", "Only normal web ports (80 and 443) are allowed.", 400)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    host = host.lower().rstrip(".")
    if ":" in host:
        host = f"[{host}]"                               # an IPv6 literal needs its brackets back
    return urlunsplit((parts.scheme, host, parts.path or "/", urlencode(query), ""))      # the #fragment is dropped


def check_public(url: str) -> None:
    """Refuse an address that is not a public website (your own machine, your network, cloud metadata). The filtering
    proxy enforces the same rule on every real connection; this check gives a clear message before connecting."""
    parts = urlsplit(url)
    try:
        resolve_public(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80))
    except Denied as e:
        if e.reason == "unresolvable":
            raise AddLinkError("unreachable", "That website's address could not be found.", 422)
        raise AddLinkError("blocked_host", "That address is on your own computer or network, not a public website.", 400)
    except ValueError:
        raise AddLinkError("invalid_url", "That does not look like a valid web link.", 400)


# ---- 2. reading the page ------------------------------------------------------------------------------------------------
def fetch_plain(url: str, timeout: float, proxy_url: str | None = None) -> tuple[int | None, str]:
    """GET with redirects followed by hand: every hop is checked, and the connection goes through the filtering proxy.
    Never decompresses (a compressed bomb cannot be inflated into memory), reads at most MAX_PAGE_BYTES, and gives up
    after PLAIN_DEADLINE seconds in total. Returns (status, html)."""
    import httpx
    headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5", "Accept-Language": "en-US,en;q=0.8",
               "Accept-Encoding": "identity"}
    end = time.monotonic() + PLAIN_DEADLINE
    current = url
    with httpx.Client(follow_redirects=False, timeout=timeout, headers=headers, proxy=proxy_url, trust_env=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            check_public(current)
            try:
                with client.stream("GET", current) as r:
                    if r.is_redirect and r.headers.get("location"):
                        current = normalize_url(urljoin(current, r.headers["location"]))
                        continue
                    if r.status_code != 200:
                        return r.status_code, ""
                    if r.headers.get("content-encoding", "identity").lower() not in ("", "identity"):
                        log.info("plain fetch of %s is compressed: leaving it to Chrome", urlsplit(current).hostname)
                        return None, ""
                    ctype = r.headers.get("content-type", "")
                    if ctype and not re.search(r"html|xml|text/plain", ctype, re.I):
                        raise AddLinkError("not_html", "That link is not a web page (it looks like a file or media).", 422)
                    body = bytearray()
                    for chunk in r.iter_raw():                    # whatever has arrived (a size here would wait for a full block)
                        body += chunk
                        if len(body) >= MAX_PAGE_BYTES:
                            break
                        if time.monotonic() > end:
                            raise AddLinkError("timeout", "That page took too long to load. Nothing was added.", 504)
                    return 200, bytes(body[:MAX_PAGE_BYTES]).decode(r.encoding or "utf-8", "replace")
            except httpx.HTTPError as e:
                log.info("plain fetch failed for %s: %s", urlsplit(current).hostname, type(e).__name__)
                return None, ""
            if time.monotonic() > end:
                raise AddLinkError("timeout", "That page took too long to load. Nothing was added.", 504)
    raise AddLinkError("unreachable", "That link redirects too many times.", 422)


def _kill_group(pgid: int) -> None:
    """SIGKILL a whole process group and keep going until nothing of it is left (Chrome starts many helper processes)."""
    for _ in range(20):
        try:
            os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            return                                           # no member left
        time.sleep(0.05)


def sweep_stale_profiles(max_age: float = STALE_PROFILE_AGE) -> None:
    """Remove throwaway Chrome profiles left behind by a run that was killed (power loss, launchd)."""
    root = tempfile.gettempdir()
    try:
        names = os.listdir(root)
    except OSError:
        return
    for name in names:
        path = os.path.join(root, name)
        if name.startswith("dr-add-") and os.path.isdir(path) and time.time() - os.path.getmtime(path) > max_age:
            shutil.rmtree(path, ignore_errors=True)


def fetch_chrome(url: str, chrome: str, timeout: float, proxy_url: str | None = None) -> str:
    """The page as a real browser sees it (after scripts). Chrome is started headless with a throwaway profile, every
    connection it makes goes through the filtering proxy (redirects, iframes and images to private addresses are refused),
    downloads are sent into the throwaway profile, and it is killed as soon as it has printed the page (it often does
    not exit by itself) and never runs longer than `timeout`."""
    if not chrome or not Path(chrome).exists():
        raise AddLinkError("no_chrome", "This site blocks simple readers and Google Chrome was not found to open it.", 422)
    sweep_stale_profiles()
    profile = tempfile.mkdtemp(prefix="dr-add-")
    (Path(profile) / "Default").mkdir()
    (Path(profile) / "Default" / "Preferences").write_text(json.dumps(
        {"download": {"default_directory": str(Path(profile) / "downloads"), "prompt_for_download": False, "directory_upgrade": True}}))
    flags = ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--mute-audio",
             "--disable-background-networking", "--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
             "--js-flags=--max-old-space-size=256", f"--user-data-dir={profile}", f"--user-agent={UA}"]
    if proxy_url:
        flags += [f"--proxy-server={proxy_url}", "--proxy-bypass-list=<-loopback>",           # even localhost goes through the filter
                  "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"]                # and Chrome resolves nothing itself
    # `nice`: a hostile page cannot slow the Mac down; a minimal environment: Chrome needs none of the helper's settings
    cmd = ["/usr/bin/nice", "-n", "10", chrome, *flags, "--dump-dom", url]
    env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": os.environ.get("HOME", profile), "TMPDIR": tempfile.gettempdir()}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, env=env)
    buf = bytearray()
    try:
        deadline = time.monotonic() + timeout
        fd = proc.stdout.fileno()
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.25)
            if ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break                                   # Chrome exited: that was everything
                buf += chunk
                if len(buf) > MAX_DOM_BYTES or bytes(buf[-64:]).lower().rstrip().endswith(b"</html>"):
                    break                                   # the page is complete (or absurdly large)
            elif proc.poll() is not None:
                break
    finally:
        _kill_group(proc.pid)
        proc.wait(timeout=5)
        _kill_group(proc.pid)                                # a child forked at the very moment of the first signal can escape it
        shutil.rmtree(profile, ignore_errors=True)
    return bytes(buf[:MAX_DOM_BYTES]).decode("utf-8", "replace")


def assess(html: str, cfg: Config) -> tuple[str, int, str]:
    """(article text, word count, problem). `problem` is "" when the page is a readable article."""
    from .http import extract_article
    text = extract_article(html, cfg.raw["add_link"]["max_words"]) if html else ""
    words = len(text.split())
    head = (re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S) or [None, ""])[1]
    if BLOCK_PAGE.search(head) or (words < 120 and BLOCK_PAGE.search(html or "")):
        return text, words, "blocked"
    if MEMBER_ONLY.search(html or "") and words < 800:
        return text, words, "member_only"
    if LOGIN_WALL.search(text[:600]) and words < 400:
        return text, words, "login"
    if words < cfg.raw["add_link"]["min_words"]:
        return text, words, "short"
    return text, words, ""


def limit_html(html: str) -> str:
    """At most MAX_HTML_CHARS characters and MAX_TAGS tags: the rest of a hostile page is never parsed."""
    html = html[:MAX_HTML_CHARS]
    at = -1
    for _ in range(MAX_TAGS):
        at = html.find("<", at + 1)
        if at < 0:
            return html
    return html[:at]


def read_page(url: str, cfg: Config, proxy_url: str | None = None) -> tuple[str, str, str]:
    """Returns (html, article text, how it was read). Everything is parsed from at most MAX_HTML_CHARS characters and MAX_TAGS tags."""
    ac = cfg.raw["add_link"]
    problem = "unreachable"
    status, html = fetch_plain(url, ac["fetch_timeout"], proxy_url)
    if status == 200:
        html = limit_html(html)
        text, words, problem = assess(html, cfg)
        if not problem:
            return html, text, "plain"
    log.info("plain fetch of %s gave status=%s problem=%s: trying Chrome", urlsplit(url).hostname, status, problem)
    html = limit_html(fetch_chrome(url, os.path.expanduser(ac["chrome"]), ac["chrome_timeout"], proxy_url))
    text, words, problem = assess(html, cfg)
    if not problem:
        return html, text, "chrome"
    messages = {
        "member_only": "This is a member-only story: it needs a login, so only a preview could be read. Nothing was added.",
        "login": "The page asks you to sign in to read it, so nothing was added.",
        "blocked": "The site blocked automatic reading (bot check), so nothing was added.",
        "short": "Not enough readable text on that page (a video, a listing or an app page?), so nothing was added.",
        "unreachable": "That page could not be opened, so nothing was added.",
    }
    raise AddLinkError(problem if problem in messages else "unreadable", messages.get(problem, messages["short"]), 422)


# ---- 3. what the page says about itself -----------------------------------------------------------------------------------
def metadata(html: str, url: str) -> dict:
    """Title, site, author, publisher, date and description from the page's own metadata (all untrusted text)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "lxml")

    def meta(*names: str) -> str:
        for n in names:
            tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if tag and tag.get("content"):
                return re.sub(r"\s+", " ", tag["content"]).strip()[:300]
        return ""

    ld: list[dict] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        ld += [d for d in (data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []) if isinstance(d, dict)]

    def ld_name(key: str) -> str:
        for d in ld:
            v = d.get(key)
            v = v[0] if isinstance(v, list) and v else v
            if isinstance(v, dict) and v.get("name"):
                return str(v["name"])[:200]
            if isinstance(v, str) and key != "datePublished":
                return v[:200]
        return ""

    title = soup.title.get_text(" ", strip=True)[:300] if soup.title else ""
    host = (urlsplit(url).hostname or "").removeprefix("www.")
    return {"url": url, "domain": host, "page_title": meta("og:title", "twitter:title") or title, "html_title": title,
            "site_name": meta("og:site_name"), "publisher": ld_name("publisher"), "author": meta("author", "article:author") or ld_name("author"),
            "description": meta("og:description", "description"), "published": meta("article:published_time") or ld_name("datePublished")}


# ---- 4. summarize and rate ----------------------------------------------------------------------------------------------
def summarize(cfg: Config, meta: dict, text: str) -> dict:
    from .llm import USAGE_LIMIT, Claude, LlmError, Usage
    claude = Claude(cfg, Usage())
    system = claude.prompt("stages/add_link.md", "summary_rules.md", "output_rules.md", "verdict_criteria.md", "profile.md")
    payload = {**meta, "text": text}
    try:
        out = claude.call("add_link", system, payload, claude.schema("add_link"), retries=1, timeout=cfg.raw["add_link"]["claude_timeout"])
    except LlmError as e:
        if USAGE_LIMIT.search(str(e)):
            raise AddLinkError("claude_limit", "Your Claude usage limit is reached; try again later. Nothing was added.", 503)
        raise AddLinkError("claude_error", "Claude could not summarize that page just now. Nothing was added.", 502)
    if not out.get("readable"):
        raise AddLinkError("unreadable", "That page does not contain a readable article, so nothing was added.", 422)
    return out


# ---- 5. the Read Later record -------------------------------------------------------------------------------------------
def record_id(url: str) -> str:
    from .models import make_id
    return make_id("manual", url)


def make_record(cfg: Config, url: str, meta: dict, out: dict, now: datetime | None = None, text: str = "") -> dict:
    from .models import full_words, reading_minutes
    from .pipeline import clamp_summary
    now = now or datetime.now(cfg.tz)
    stars = int(out["stars"])
    utc = now.astimezone(timezone.utc)
    stamp = utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"
    rec = clean_record({
        "id": record_id(url), "title": out["title"] or meta["page_title"] or meta["domain"], "url": url,
        "source": out["source"] or meta["site_name"] or meta["domain"],
        "published": now.date().isoformat(),               # the day it was ADDED: the list sorts by it, newest first
        "stars": stars, "verdict": "read" if stars >= 4 else "skip",         # 4-5 stars <=> "Read fully" (project rule)
        "summary": clamp_summary(out["summary"], 300), "reason": out["reason"],
        "minutes": reading_minutes(full_words(text) or 0) if text.strip() else 0,      # counted from the article text
        "read_later": True, "favorite": False, "read": False, "manual": True, "added_at": stamp, "updated_at": stamp,
    })
    if rec is None:
        raise AddLinkError("unreadable", "That page could not be turned into a list entry. Nothing was added.", 422)
    return rec


def process(cfg: Config, raw_url: str) -> dict:
    """Everything from a pasted link to a finished record (not yet saved). Raises AddLinkError with a plain-language reason."""
    url = normalize_url(raw_url)
    check_public(url)
    with FilteringProxy(deadline=cfg.raw["add_link"]["chrome_timeout"] + PLAIN_DEADLINE + 20) as proxy:
        html, text, via = read_page(url, cfg, proxy.url)
    meta = metadata(html, url)
    out = summarize(cfg, meta, text)
    rec = make_record(cfg, url, meta, out, text=text)
    log.info("added a link from %s via %s: %s stars, source %r", urlsplit(url).hostname, via, rec["stars"], rec["source"])
    return rec
