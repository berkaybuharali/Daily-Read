"""Claude Platform release notes (HTML page): <h3 id="september-30-2026"> followed by <ul><li> notes."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

from ..http import html_to_text, truncate_words
from ..models import Item, SourceResult, make_id
from ..timeutil import parse_human_date
from .base import Context, coverage_note, feed_span

DATE_ID = re.compile(r"^([a-z]+)-(\d{1,2})-(\d{4})$")


def fetch(ctx: Context) -> SourceResult:
    res = SourceResult(ctx.section.key)
    soup = BeautifulSoup(ctx.http.get_text(ctx.section.url), "lxml")
    dated = []
    for h3 in soup.find_all("h3"):
        # heading text also contains icon glyphs from a copy-link button -> use the id ("september-30-2026")
        m = DATE_ID.match(h3.get("id") or "")
        day = parse_human_date(f"{m.group(1)} {m.group(2)}, {m.group(3)}") if m else None
        if day:
            dated.append((h3, day))
    if not dated:
        raise ValueError("no dated <h3> headings found — page layout changed?")
    feed_span(res, [d for _, d in dated])
    coverage_note(ctx, res, full_feed=False)   # page holds full history

    for h3, day in dated:
        if not ctx.window.contains_day(day):
            continue
        anchor = h3.get("id") or ""
        # notes are the <li> of the list(s) directly following the heading, until the next heading
        for sib in h3.find_next_siblings():
            if sib.name in {"h2", "h3"}:
                break
            if sib.name != "ul":
                continue
            for idx, li in enumerate(sib.find_all("li", recursive=False)):
                text = truncate_words(html_to_text(str(li)), ctx.max_words)
                res.items.append(Item(
                    id=make_id(ctx.section.key, day.isoformat(), text[:200]),
                    source=ctx.section.key,
                    url=f"{ctx.section.url}#{anchor}" if anchor else ctx.section.url,
                    published=None,
                    day=day,
                    title=None,
                    kind=None,
                    content=text,
                    content_origin="release_note",
                ))
    return res
