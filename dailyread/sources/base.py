"""Shared plumbing for sources."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date

from ..config import Config, Section
from ..http import Http, HttpError, JsonCache, extract_article
from ..models import Item, SourceResult, SourceStats
from ..timeutil import Window

log = logging.getLogger(__name__)


@dataclass
class Context:
    cfg: Config
    http: Http
    window: Window
    section: Section
    page_cache: JsonCache       # url -> {"published": iso} for pages whose date we had to scrape

    @property
    def max_words(self) -> int:
        return self.cfg.limits["article_max_words"]


def feed_span(result: SourceResult, days: list[date]) -> None:
    """Record how far back the feed reaches (used for health stats + the coverage note)."""
    if not days:
        return
    result.stats.items_in_feed = len(days)
    result.stats.oldest_item_in_feed = min(days).isoformat()
    result.stats.newest_item_in_feed = max(days).isoformat()


def coverage_note(ctx: Context, result: SourceResult, full_feed: bool = True) -> None:
    """If the feed's oldest item is newer than the window start, some items may have scrolled out."""
    oldest = result.stats.oldest_item_in_feed
    start_day = ctx.window.start.astimezone(ctx.window.tz).date()
    if full_feed and oldest and date.fromisoformat(oldest) > start_day:
        result.stats.note = f"Feed only reaches back to {date.fromisoformat(oldest):%-d %b}; older items may be missing."


def fetch_article_text(ctx: Context, item: Item, fallback: str = "", fallback_origin: str = "feed") -> None:
    """Populate item.content with article text; fall back to feed text when the page can't be read."""
    try:
        html = ctx.http.get_text(item.url)
        text = extract_article(html, ctx.max_words)
    except Exception as e:      # any page problem -> fall back to feed text, never fail the section
        log.info("article fetch failed %s: %s", item.url, e)
        text = ""
    if len(text.split()) >= 60:
        item.content, item.content_origin = text, "article"
    else:
        item.content, item.content_origin = fallback, fallback_origin


def fail(result: SourceResult, e: Exception) -> SourceResult:
    status = "http_error" if isinstance(e, HttpError) else "parse_error"
    result.stats = SourceStats(status=status, http_status=getattr(e, "status", None), error=str(e)[:300])
    log.warning("source %s failed: %s", result.key, e)
    return result


def clean_space(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()
