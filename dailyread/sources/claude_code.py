"""Claude Code GitHub releases: one row per version."""
from __future__ import annotations

import feedparser

from ..http import html_to_text, truncate_words
from ..models import Item, SourceResult, make_id
from ..timeutil import local_day, parse_any
from .base import Context, clean_space, coverage_note, feed_span


def fetch(ctx: Context) -> SourceResult:
    res = SourceResult(ctx.section.key)
    feed = feedparser.parse(ctx.http.get_text(ctx.section.url))
    entries = [(e, parse_any(e.get("updated"))) for e in feed.entries]
    if not any(dt for _, dt in entries):
        raise ValueError(f"no dated entries in feed ({len(feed.entries)} entries) — not a feed?")
    feed_span(res, [local_day(dt, ctx.window.tz) for _, dt in entries if dt])
    coverage_note(ctx, res)

    for entry, published in entries:
        if not ctx.window.contains(published):
            continue
        version = clean_space(entry.get("title", ""))
        html = (entry.get("content") or [{}])[0].get("value", "")
        res.items.append(Item(
            id=make_id(ctx.section.key, version),
            source=ctx.section.key,
            url=entry.get("link", ctx.section.url),
            published=published,
            day=local_day(published, ctx.window.tz),
            title=f"Claude Code {version}",
            kind="Release",
            content=truncate_words(html_to_text(html), ctx.max_words),
            content_origin="release_note",
        ))
    return res
