"""SRE Weekly: one row per linked article inside each weekly issue (reliability, incidents, postmortems)."""
from __future__ import annotations

import feedparser
from bs4 import BeautifulSoup

from ..models import Item, SourceResult, make_id
from ..timeutil import local_day, parse_any
from .base import Context, clean_space, coverage_note, feed_span, fetch_article_text


def parse_issue(html: str) -> list[dict]:
    """`<div class="sreweekly-entry">` blocks -> {url, title, note, byline}. The sponsor box is not an entry."""
    rows = []
    for entry in BeautifulSoup(html, "lxml").select("div.sreweekly-entry"):
        link = entry.select_one(".sreweekly-title a[href]")
        if not link:
            continue
        desc = entry.select_one(".sreweekly-description")
        byline = desc.find("small") if desc else None
        byline_text = clean_space(byline.get_text(" ", strip=True)) if byline else ""
        if byline:
            byline.decompose()
        rows.append({
            "url": link["href"],
            "title": clean_space(link.get_text()),
            "note": clean_space(desc.get_text(" ", strip=True)) if desc else "",
            "byline": byline_text,
        })
    return rows


def fetch(ctx: Context) -> SourceResult:
    res = SourceResult(ctx.section.key)
    feed = feedparser.parse(ctx.http.get_text(ctx.section.url))
    entries = [(e, parse_any(e.get("published"))) for e in feed.entries]
    if not any(dt for _, dt in entries):
        raise ValueError(f"no dated entries in feed ({len(feed.entries)} entries) — not a feed?")
    feed_span(res, [local_day(dt, ctx.window.tz) for _, dt in entries if dt])
    coverage_note(ctx, res)

    for entry, published in entries:
        if not ctx.window.contains(published):
            continue
        html = (entry.get("content") or [{}])[0].get("value", "")
        for row in parse_issue(html):
            res.items.append(Item(
                id=make_id(ctx.section.key, row["url"]),
                source=ctx.section.key,
                url=row["url"],
                published=published,
                day=local_day(published, ctx.window.tz),
                title=row["title"],
                extra={"curator_note": row["note"], "byline": row["byline"],
                       "list_title": clean_space(entry.get("title", "")), "list_url": entry.get("link")},
            ))

    def load(item: Item) -> None:
        fallback = f"Title: {item.title}\nCurator note: {item.extra['curator_note']}"
        fetch_article_text(ctx, item, fallback=fallback, fallback_origin="curator_note")

    ctx.http.map(load, res.items)
    return res
