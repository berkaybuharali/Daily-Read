"""Google Cloud release-notes feeds: one Atom entry per day, split into one row per <h3> note."""
from __future__ import annotations

import feedparser
from bs4 import BeautifulSoup

from ..http import html_to_text, truncate_words
from ..models import Item, SourceResult, make_id
from ..timeutil import parse_human_date
from .base import Context, clean_space, coverage_note, feed_span

TYPE_ALIASES = {"change": "Changed", "changed": "Changed", "fix": "Fixed", "fixed": "Fixed",
                "issue": "Issue", "issues": "Issue", "deprecation": "Deprecated", "deprecated": "Deprecated",
                "breaking": "Breaking", "feature": "Feature", "announcement": "Announcement",
                "security": "Security", "libraries": "Libraries", "library": "Libraries"}


def split_notes(html: str) -> list[tuple[str, str | None, str]]:
    """Return [(type, bold_title_or_None, html_fragment)] for each <h3> block of a day entry."""
    soup = BeautifulSoup(html, "lxml")
    notes: list[tuple[str, str | None, str]] = []
    for h3 in soup.find_all("h3"):
        parts = []
        for sib in h3.next_siblings:
            if getattr(sib, "name", None) == "h3":
                break
            parts.append(str(sib))
        fragment = "".join(parts)
        frag_soup = BeautifulSoup(fragment, "lxml")
        first_p = frag_soup.find("p")
        title = None
        if first_p and first_p.find("strong") and clean_space(first_p.get_text()) == clean_space(first_p.find("strong").get_text()):
            title = clean_space(first_p.get_text())
        raw_type = clean_space(h3.get_text())
        notes.append((TYPE_ALIASES.get(raw_type.lower(), raw_type), title, fragment))
    return notes


def fetch(ctx: Context) -> SourceResult:
    res = SourceResult(ctx.section.key)
    feed = feedparser.parse(ctx.http.get_text(ctx.section.url))
    days = [(e, parse_human_date(e.get("title", ""))) for e in feed.entries]
    if not any(d for _, d in days):
        raise ValueError(f"no dated entries in feed ({len(feed.entries)} entries) — not a feed or title format changed?")
    feed_span(res, [d for _, d in days if d])
    coverage_note(ctx, res)

    for entry, day in days:
        if not day or not ctx.window.contains_day(day):
            continue
        html = (entry.get("content") or [{}])[0].get("value", "") or entry.get("summary", "")
        occurrences: dict[str, int] = {}
        for ntype, title, fragment in split_notes(html):
            full = html_to_text(fragment)
            text = truncate_words(full, ctx.max_words)
            # id from content, not position: a note added later to the same day must not re-key the others
            key = make_id(ntype, " ".join(full.split()))
            occurrences[key] = occurrences.get(key, 0) + 1
            res.items.append(Item(
                id=make_id(ctx.section.key, day.isoformat(), key, str(occurrences[key])),
                source=ctx.section.key,
                url=entry.get("link", ctx.section.url),
                published=None,
                day=day,
                title=title,
                kind=ntype,
                content=text,
                content_origin="release_note",
            ))
    return res
