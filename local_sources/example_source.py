"""Example private source: a plain RSS/Atom feed, one row per entry.

Copy to local_sources/<name>.py, rename the key, and add a section with that key to config.local.yaml:

    sections:
      - key: my_private_blog
        after: bigquery_rn
        title: My private blog
        short: "Private blog"
        icon: "🔒"
        url: https://example.com/feed.xml
"""
from __future__ import annotations

from dailyread.sources.blog_feeds import _generic
from dailyread.sources.base import Context


def fetch(ctx: Context):
    return _generic(ctx, url_filter=None, fetch_pages=False)   # fetch_pages=True if the feed has only excerpts


FETCHERS = {"my_private_blog": fetch}
