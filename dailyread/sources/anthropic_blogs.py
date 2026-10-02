"""Anthropic news/research/engineering + claude.com/blog, combined. No RSS exists, so:
sitemaps -> URLs whose <lastmod> is inside the window -> fetch page -> real publish date from page metadata."""
from __future__ import annotations

import json
import re
from datetime import datetime

from bs4 import BeautifulSoup

from ..http import extract_article
from ..models import Item, SourceResult, make_id
from ..timeutil import local_day, parse_human_date, parse_iso
from .base import Context, clean_space

PATTERNS = [
    (re.compile(r"^https://www\.anthropic\.com/(news|research|engineering)/[^/?#]+$"), None),
    (re.compile(r"^https://claude\.com/blog/[^/?#]+$"), "blog"),     # English only (localized live under /xx/blog)
]
KIND_LABELS = {"news": "News", "research": "Research", "engineering": "Engineering", "blog": "Claude blog"}
TITLE_SUFFIX = re.compile(r"\s*(\\|\||–|-)\s*(Anthropic|Claude by Anthropic|Claude)\s*$")


def _sitemap(ctx: Context, url: str) -> list[tuple[str, str, datetime]]:
    soup = BeautifulSoup(ctx.http.get_text(url), "xml")
    out = []
    for u in soup.find_all("url"):
        loc, lastmod = u.find("loc"), u.find("lastmod")
        if not loc or not lastmod:
            continue
        loc = loc.get_text(strip=True)
        for pat, kind in PATTERNS:
            m = pat.match(loc)
            if m:
                out.append((loc, kind or m.group(1), parse_iso(lastmod.get_text(strip=True))))
    return out


def _page_meta(html: str) -> tuple[str | None, datetime | None, str | None]:
    """(title, published datetime-or-None, published date string) from a post page."""
    soup = BeautifulSoup(html, "lxml")
    og = soup.find("meta", property="og:title")
    title = clean_space(og["content"]) if og and og.get("content") else clean_space(soup.title.get_text() if soup.title else "")
    title = TITLE_SUFFIX.sub("", title) or None
    meta = soup.find("meta", property="article:published_time")
    if meta and meta.get("content"):
        return title, parse_iso(meta["content"]), None
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for obj in data if isinstance(data, list) else [data]:
            if isinstance(obj, dict) and obj.get("datePublished"):
                raw = str(obj["datePublished"])
                return title, parse_iso(raw), raw
    m = re.search(r'"datePublished"\s*:\s*"([^"]+)"', html)
    return title, (parse_iso(m.group(1)) if m else None), (m.group(1) if m else None)


def fetch(ctx: Context) -> SourceResult:
    res = SourceResult(ctx.section.key)
    entries = []
    for url in (ctx.section.url, *ctx.section.extra_urls):
        entries += _sitemap(ctx, url)
    if not entries:
        raise ValueError("no matching URLs in sitemaps — layout changed?")
    entries.sort(key=lambda e: e[2] or datetime.min.replace(tzinfo=ctx.window.tz), reverse=True)

    # lastmod >= publish date, so anything last modified before the window can't be new.
    # Always include the most recent couple so "last one: <date>" is known on empty days.
    todo = [e for e in entries if e[2] and e[2] >= ctx.window.start] or entries[:2]

    failed: list[str] = []

    def load(entry):
        url, kind, _ = entry
        try:
            html = ctx.http.get_text(url)
        except Exception as e:
            failed.append(f"{url}: {e}")
            return None
        title, published, raw = _page_meta(html)
        day = local_day(published, ctx.window.tz) if published else (parse_human_date(raw) if raw else None)
        if not day:
            return None
        return Item(
            id=make_id(ctx.section.key, url),
            source=ctx.section.key, url=url, published=published, day=day, title=title,
            kind=KIND_LABELS.get(kind, kind.title()),
            content=extract_article(html, ctx.max_words), content_origin="article",
        )

    loaded = [i for i in ctx.http.map(load, todo) if i]
    if failed:      # a post modified inside the window couldn't be read: retry this window next time
        res.stats.status, res.stats.error = "partial", f"{len(failed)} page(s) unreadable: " + "; ".join(failed)[:250]
    days = [i.day for i in loaded]
    if days:
        res.stats.newest_item_in_feed = max(days).isoformat()
    res.stats.items_in_feed = len(entries)
    for item in loaded:
        inside = ctx.window.contains(item.published) if item.published else ctx.window.contains_day(item.day)
        if inside:
            if len(item.content.split()) < 60:
                item.content, item.content_origin = f"Title: {item.title}", "feed"
            res.items.append(item)
    return res
