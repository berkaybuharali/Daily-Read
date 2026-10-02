"""IT industry news candidates: Techmeme river (HTML) + SiliconANGLE + TechCrunch feeds.
Only headlines + short descriptions here; full text is fetched later for the picked stories only."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import feedparser
from bs4 import BeautifulSoup

from ..http import html_to_text
from ..models import Item, SourceResult, make_id
from ..timeutil import local_day, parse_any, parse_human_date, to_utc
from .base import Context, clean_space

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")      # Techmeme river times are US Eastern
PROMO = re.compile(r"(?i)\b(disrupt 20\d\d|tickets?|save up to|exhibit(or)?s?\b|thecube|livestream|webinar|"
                   r"strictlyvc|early.?bird|register now|last chance)\b")


def normalize_url(url: str) -> str:
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc.lower().removeprefix("www."), p.path.rstrip("/"), "", ""))


def _techmeme(ctx: Context, url: str) -> list[Item]:
    soup = BeautifulSoup(ctx.http.get_text(url), "lxml")
    items = []
    for h2 in soup.find_all(["h2", "H2"]):
        day = parse_human_date(clean_space(h2.get_text()))
        if not day:
            continue
        table = h2.find_next_sibling("table")
        if not table:
            continue
        for tr in table.find_all("tr", class_="ritem"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            try:
                t = datetime.strptime(clean_space(tds[0].get_text()).split("•")[0].strip(), "%I:%M %p").time()
            except ValueError:
                continue
            published = to_utc(datetime.combine(day, t, tzinfo=ET))
            cite = tds[1].find("cite")
            outlet = clean_space(cite.find("a").get_text()) if cite and cite.find("a") else None
            link = [a for a in tds[1].find_all("a", href=True) if not (cite and a in cite.find_all("a"))]
            if not link:
                continue
            a = link[-1]
            items.append(Item(
                id=make_id("it", normalize_url(a["href"])), source=ctx.section.key, url=a["href"],
                published=published, day=local_day(published, ctx.window.tz), title=clean_space(a.get_text()),
                content="", content_origin="feed",
                extra={"outlet": outlet or "Techmeme", "via": "Techmeme", "on_techmeme": True},
            ))
    if not items:
        raise ValueError("Techmeme river: no rows parsed — layout changed?")
    return items


def _rss(ctx: Context, url: str, outlet: str) -> list[Item]:
    feed = feedparser.parse(ctx.http.get_text(url))
    out = []
    for e in feed.entries:
        published = parse_any(e.get("published") or e.get("updated"))
        if not published:
            continue
        desc = clean_space(html_to_text(e.get("summary", "")))[:400]
        out.append(Item(
            id=make_id("it", normalize_url(e.get("link", ""))), source=ctx.section.key, url=e.get("link", ""),
            published=published, day=local_day(published, ctx.window.tz), title=clean_space(e.get("title", "")),
            content=desc, content_origin="feed", extra={"outlet": outlet, "via": outlet, "on_techmeme": False},
        ))
    return out


def fetch(ctx: Context) -> SourceResult:
    res = SourceResult(ctx.section.key)
    fetchers = [("Techmeme", lambda: _techmeme(ctx, ctx.section.url))]
    for u in ctx.section.extra_urls:
        outlet = "SiliconANGLE" if "siliconangle" in u else "TechCrunch" if "techcrunch" in u else urlsplit(u).netloc
        fetchers.append((outlet, lambda u=u, o=outlet: _rss(ctx, u, o)))

    errors, all_items = [], []
    for name, fn in fetchers:
        try:
            all_items += fn()
        except Exception as e:      # best effort: one broken feed must not kill the section
            errors.append(f"{name}: {e}")
            log.warning("IT news feed %s failed: %s", name, e)
    if not all_items:
        raise RuntimeError("; ".join(errors) or "no IT news items")

    by_url: dict[str, Item] = {}
    for item in all_items:
        if not ctx.window.contains(item.published) or PROMO.search(item.title or ""):
            continue
        key = normalize_url(item.url)
        if key in by_url:                               # same article from two feeds -> merge flags
            by_url[key].extra["on_techmeme"] |= item.extra["on_techmeme"]
            by_url[key].content = by_url[key].content or item.content
        else:
            by_url[key] = item
    res.items = sorted(by_url.values(), key=lambda i: i.published, reverse=True)
    days = [i.day for i in all_items]
    res.stats.items_in_feed = len(all_items)
    res.stats.oldest_item_in_feed = min(days).isoformat()
    res.stats.newest_item_in_feed = max(days).isoformat()
    if errors:
        res.stats.note = "Partial: " + "; ".join(errors)[:200]
    return res
