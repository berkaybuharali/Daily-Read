"""Source registry: config section key -> fetch function.

To add a source: implement `fetch(ctx) -> SourceResult` (see CLAUDE.md "Adding a source"),
register it here, and add a section entry in config.yaml at the right position.

Private sources: put a module in `local_sources/` (git-ignored) that defines
`FETCHERS = {"<key>": fetch}` (and optionally `RELEASE_NOTE_SECTIONS = {...}`), and add its section to
`config.local.yaml`. Every `local_sources/*.py` is loaded automatically."""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Callable

from ..models import SourceResult
from . import anthropic_blogs, blog_feeds, claude_code, claude_platform, gcp_release_notes, independent_writers, it_news, sre_weekly
from .base import Context

log = logging.getLogger(__name__)
LOCAL_DIR = Path(__file__).resolve().parents[2] / "local_sources"

FETCHERS: dict[str, Callable[[Context], SourceResult]] = {
    "gemini_enterprise_rn": gcp_release_notes.fetch,
    "claude_code_rn": claude_code.fetch,
    "claude_platform_rn": claude_platform.fetch,
    "anthropic_blogs": anthropic_blogs.fetch,
    "agent_platform_rn": gcp_release_notes.fetch,
    "bigquery_rn": gcp_release_notes.fetch,
    "gcloud_blog": blog_feeds.fetch_gcloud_blog,
    "google_dev_blog": blog_feeds.fetch_google_dev_blog,
    "sre_weekly": sre_weekly.fetch,
    "independent_writers": independent_writers.fetch,
    "it_news": it_news.fetch,
}

# Sections whose rows are release notes (type badges, generated titles when missing)
RELEASE_NOTE_SECTIONS = {"gemini_enterprise_rn", "claude_code_rn", "claude_platform_rn", "agent_platform_rn", "bigquery_rn"}
IT_NEWS = "it_news"


def load_local_sources(directory: Path = LOCAL_DIR) -> list[str]:
    """Register fetchers from private modules in `local_sources/`. Returns the keys added."""
    added = []
    for path in sorted(directory.glob("*.py")) if directory.is_dir() else ():
        if path.name.startswith(("_", "example_")):
            continue
        spec = importlib.util.spec_from_file_location(f"dailyread_local_sources.{path.stem}", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        FETCHERS.update(getattr(mod, "FETCHERS", {}))
        RELEASE_NOTE_SECTIONS.update(getattr(mod, "RELEASE_NOTE_SECTIONS", ()))
        added += list(getattr(mod, "FETCHERS", {}))
    return added


load_local_sources()
