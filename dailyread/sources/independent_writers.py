"""Independent writers: one grouped section for individual bloggers who post now and then.

The names live in the section's `writers:` list in config.yaml: [{name, url, fetch_pages?}]. Every writer is a plain
RSS/Atom feed fetched with the generic fetcher; each row carries the writer's name. `fetch_pages: true` is for feeds
that only carry an excerpt (the article page is then read for the summary). One broken feed does not fail the section:
it is reported as `partial`, so the window is not marked as covered and the next run retries it."""
from __future__ import annotations

import logging

from ..models import SourceResult
from .base import Context, fail
from .blog_feeds import _generic

log = logging.getLogger(__name__)


def fetch(ctx: Context) -> SourceResult:
    writers = ctx.section.writers
    if not writers:
        raise ValueError(f"section {ctx.section.key} has no `writers:` list")
    res = SourceResult(ctx.section.key)
    failed: list[str] = []
    oldest: list[str] = []
    newest: list[str] = []
    for w in writers:
        try:
            r = _generic(ctx, None, fetch_pages=bool(w.get("fetch_pages")), url=w["url"])
        except Exception as e:
            log.warning("writer %s failed: %s", w["name"], e)
            failed.append(f"{w['name']}: {str(e)[:80]}")
            continue
        for item in r.items:
            item.extra["writer"] = w["name"]
            if not w.get("fetch_pages"):
                item.extra["full_text"] = True      # the feed carries the whole post, not an excerpt
        res.items += r.items
        res.stats.items_in_feed += r.stats.items_in_feed
        oldest += [r.stats.oldest_item_in_feed] if r.stats.oldest_item_in_feed else []
        newest += [r.stats.newest_item_in_feed] if r.stats.newest_item_in_feed else []
    if len(failed) == len(writers):
        return fail(res, RuntimeError("all writer feeds failed: " + "; ".join(failed)))
    res.stats.oldest_item_in_feed = min(oldest) if oldest else None
    res.stats.newest_item_in_feed = max(newest) if newest else None
    if failed:      # a missing writer might have new posts: don't mark this window as covered
        res.stats.status = "partial"
        res.stats.error = f"{len(failed)} feed(s) failed: " + "; ".join(failed)
    return res
