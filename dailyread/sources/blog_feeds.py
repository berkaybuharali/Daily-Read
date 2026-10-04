"""Plain RSS/Atom blogs: Google Cloud Blog, Google Developers Blog, and the generic fetcher other sources reuse."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser

from ..http import html_to_text, truncate_words
from ..models import Item, SourceResult, make_id
from ..timeutil import local_day, parse_any
from .base import Context, clean_space, coverage_note, feed_span, fetch_article_text


def _entry_date(e) -> str | None:
    return e.get("published") or e.get("updated")


def _feed_text(e) -> str:
    html = (e.get("content") or [{}])[0].get("value") or e.get("summary", "")
    return html_to_text(html)


def strip_tracking(url: str) -> str:
    """Drop utm_* query parameters (some feeds add them to every link)."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not k.lower().startswith("utm_")]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _generic(ctx: Context, url_filter: re.Pattern | None, fetch_pages: bool, url: str | None = None) -> SourceResult:
    res = SourceResult(ctx.section.key)
    feed = feedparser.parse(ctx.http.get_text(url or ctx.section.url))
    entries = [(e, parse_any(_entry_date(e))) for e in feed.entries
               if not url_filter or url_filter.match(e.get("link", ""))]
    if not any(dt for _, dt in entries):
        raise ValueError(f"no dated entries in feed ({len(feed.entries)} entries) — not a feed?")
    feed_span(res, [local_day(dt, ctx.window.tz) for _, dt in entries if dt])
    coverage_note(ctx, res)
    for e, published in entries:
        if not ctx.window.contains(published):
            continue
        raw_link = e.get("link", "")
        link = strip_tracking(raw_link)
        res.items.append(Item(
            id=make_id(ctx.section.key, raw_link),             # id from the untouched link: stays stable for feeds with utm_*
            source=ctx.section.key, url=link, published=published,
            day=local_day(published, ctx.window.tz), title=clean_space(e.get("title", "")),
            content=truncate_words(_feed_text(e), ctx.max_words), content_origin="feed",
        ))
    if fetch_pages:
        ctx.http.map(lambda i: fetch_article_text(ctx, i, fallback=i.content), res.items)
    return res


# Cloud Blog feed occasionally carries non-blog entries (e.g. job postings) -> keep only real blog URLs.
CLOUD_BLOG = re.compile(r"^https://cloud\.google\.com/blog/")


def fetch_gcloud_blog(ctx: Context) -> SourceResult:
    return _generic(ctx, CLOUD_BLOG, fetch_pages=True)


DEV_DATE = re.compile(r'"datePublished"\s*:\s*"(\d{4}-\d{2}-\d{2})')


def fetch_google_dev_blog(ctx: Context) -> SourceResult:
    """Feed items have no dates -> publish date scraped from each page (cached per URL)."""
    from ..http import extract_article
    from ..timeutil import parse_iso

    res = SourceResult(ctx.section.key)
    feed = feedparser.parse(ctx.http.get_text(ctx.section.url))
    if not feed.entries:
        raise ValueError("no entries in feed — not a feed?")
    failed: list[str] = []

    def load(e):
        url = e.get("link", "")
        cached = ctx.page_cache.get(url)
        html = None
        if not cached or not cached.get("published"):
            try:
                html = ctx.http.get_text(url)
            except Exception as ex:
                failed.append(f"{url}: {ex}")
                return None
            m = DEV_DATE.search(html)
            if not m:
                failed.append(f"{url}: no datePublished")
                return None
            cached = {"published": m.group(1)}
            ctx.page_cache.set(url, cached)
        day = parse_iso(cached["published"] + "T00:00:00+00:00").date()
        return e, url, day, html

    loaded = [x for x in ctx.http.map(load, feed.entries) if x]
    feed_span(res, [d for _, _, d, _ in loaded])
    coverage_note(ctx, res)
    if failed:      # an undated post might be new: don't mark this window as covered
        res.stats.status, res.stats.error = "partial", f"{len(failed)} page(s) unreadable: " + "; ".join(failed)[:250]
    for e, url, day, html in loaded:
        if not ctx.window.contains_day(day):
            continue
        item = Item(
            id=make_id(ctx.section.key, url), source=ctx.section.key, url=url, published=None, day=day,
            title=clean_space(e.get("title", "")),
            content=truncate_words(_feed_text(e), ctx.max_words), content_origin="feed",
        )
        if html:
            text = extract_article(html, ctx.max_words)
            if len(text.split()) >= 60:
                item.content, item.content_origin = text, "article"
        else:
            fetch_article_text(ctx, item, fallback=item.content)
        res.items.append(item)
    return res
