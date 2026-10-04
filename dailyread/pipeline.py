"""The daily pipeline: fetch -> summarize ‖ IT news -> review (stars, verdicts, highlights) -> render -> persist."""
from __future__ import annotations

import json
import logging
import math
import re
import shutil
import subprocess
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .catalog import root_rel, write_catalog
from .fsutil import write_private
from .config import Config, Section
from .http import Http, JsonCache, extract_article
from .library import backup_library, library_path
from .llm import Claude, LlmError, Usage
from .models import Item, SourceResult, make_id
from .render import render_index, render_report
from .sources import FETCHERS, IT_NEWS, RELEASE_NOTE_SECTIONS
from .sources.base import Context, fail
from .state import StateStore
from .timeutil import Window, yesterday_midnight

log = logging.getLogger(__name__)

SUMMARY_PROMPT = ("stages/summarize.md", "summary_rules.md", "output_rules.md", "verdict_criteria.md", "profile.md")
REVIEW_PROMPT = ("stages/review.md", "verdict_criteria.md", "verdict_budget.md", "output_rules.md", "profile.md")
OK_STATUSES = ("ok", "empty")
DATE_LOOKBACK_DAYS = 2          # real runs: re-check release-note date labels this far back (seen.json dedups)
MAX_SOURCE_CATCHUP_DAYS = 14    # a failing source catches up at most this far before the window start
MAX_UNSUMMARIZED_SHARE = 0.10   # a real run still succeeds if at most 10% of items lack a summary
MAX_WINDOW_DAYS = 14            # a real run after a long break covers at most the last 14 days

# Deterministic summary lint -> triggers the model "fix" pass
BANNED_OPENER = re.compile(r"^(this (article|post|release|update|blog)|the author|in this (post|article)|"
                           r"(google|anthropic|microsoft|openai|aws|amazon) (announces|announced|launches|launched))\b", re.I)
HYPE = re.compile(r"\b(revolutionary|game[- ]chang\w*|exciting|cutting[- ]edge|supercharg\w*|seamless\w*|unlock\w*)\b", re.I)


@dataclass
class RunOptions:
    dry_run: bool = True
    since: datetime | None = None          # dry-run window start override
    only: set[str] | None = None           # limit to these section keys (dry-run testing only)
    open_browser: bool = True
    now: datetime | None = None


@dataclass
class RunResult:
    report_path: Path
    data_path: Path
    record: dict
    errors: list[str] = field(default_factory=list)


class RunFailed(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------------
# Paths, windows
# ---------------------------------------------------------------------------------------------------------------
def paths(cfg: Config, dry_run: bool) -> dict[str, Path]:
    if dry_run:
        base = cfg.root / "reports" / "dry-run"
        return {"reports": base, "archive": base, "data": cfg.root / "data" / "dry-run",
                "state": cfg.root / "state-dryrun"}
    return {"reports": cfg.root / "reports", "archive": cfg.root / "reports" / "archive",
            "data": cfg.root / "data" / "reports", "state": cfg.root / "state"}


def _first_window_start(cfg: Config) -> datetime:
    raw = cfg.raw["first_window_start"]
    dt = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    return dt if dt.tzinfo else dt.replace(tzinfo=cfg.tz)


def compute_window(cfg: Config, store: StateStore, opts: RunOptions, now: datetime) -> tuple[Window, bool]:
    """Returns (window, is_rerun_today)."""
    if opts.dry_run:
        return Window(opts.since or yesterday_midnight(cfg.tz, now), now, cfg.tz), False
    st = store.state()
    today = now.date().isoformat()
    if st.get("last_report_date") == today and st.get("last_window_start"):
        # manual re-run on the same day: rebuild today's report over the same window, up to now
        start, rerun = datetime.fromisoformat(st["last_window_start"]), True
    elif st.get("last_window_end"):
        start, rerun = datetime.fromisoformat(st["last_window_end"]), False
    else:
        return Window(_first_window_start(cfg), now, cfg.tz), False      # first run: exactly as configured
    start = max(start, now - timedelta(days=MAX_WINDOW_DAYS))           # long break: keep the report readable
    return Window(start, now, cfg.tz, day_lookback=DATE_LOOKBACK_DAYS), rerun


def source_window(window: Window, src_state: dict, dry_run: bool) -> Window:
    """A source that failed (or was disabled) is fetched from its own last good point, with bounded catch-up."""
    if dry_run or not src_state.get("covered_until"):
        return window
    covered = datetime.fromisoformat(src_state["covered_until"])
    start = max(min(covered, window.start), window.start - timedelta(days=MAX_SOURCE_CATCHUP_DAYS))
    return Window(start, window.end, window.tz, day_lookback=window.day_lookback)


# ---------------------------------------------------------------------------------------------------------------
# Summaries
# ---------------------------------------------------------------------------------------------------------------
def clamp_summary(text: str, limit: int) -> str:
    """Last-resort hard cut (after the model's fix pass): end at a sentence if possible."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    m = list(re.finditer(r"[.!?](\s|$)", cut))
    if m and m[-1].end() > limit * 0.6:
        return cut[:m[-1].end()].strip()
    cut = cut[: limit - 2]
    return cut[: cut.rfind(" ")].rstrip(",;:—-") + " …"


def summary_problems(summary: str, limit: int) -> list[str]:
    problems = []
    if len(summary) > limit:
        problems.append(f"too long ({len(summary)} chars, max {limit})")
    if BANNED_OPENER.search(summary):
        problems.append("banned filler opener")
    if m := HYPE.search(summary):
        problems.append(f"hype word: {m.group(0)}")
    if summary.rstrip().endswith(("…", "...")):
        problems.append("ends with an ellipsis")
    return problems


def fix_summaries(claude: Claude, cfg: Config, items: list[Item]) -> None:
    """Model rewrite for summaries failing the lint (no mid-sentence cuts); hard-cut only as a fallback."""
    limit = cfg.limits["summary_max_chars"]
    bad = {i.id: (i, probs) for i in items if i.summary and (probs := summary_problems(i.summary, limit))}
    if bad:
        system = claude.prompt("stages/shorten.md", "summary_rules.md").replace("{target}", str(limit - 20))
        schema = claude.with_enum(claude.schema("shorten"), ("items", "[]", "id"), list(bad))
        try:
            out = claude.call("shorten", system, {"items": [{"id": k, "summary": i.summary, "problems": probs}
                                                           for k, (i, probs) in bad.items()]}, schema)
            for r in out.get("items", []):
                if r.get("id") in bad and r.get("summary"):
                    bad[r["id"]][0].summary = re.sub(r"\s+", " ", r["summary"]).strip()
        except LlmError as e:
            log.warning("summary fix pass failed: %s", e)
    for i in items:
        if i.summary:
            i.summary = clamp_summary(i.summary, limit)


def _even_chunks(items: list[Item], size: int) -> list[list[Item]]:
    if not items:
        return []
    n = math.ceil(len(items) / size)
    k = math.ceil(len(items) / n)
    return [items[j:j + k] for j in range(0, len(items), k)]


def summarize_items(claude: Claude, cfg: Config, section: Section, items: list[Item], stage: str = "summarize",
                    filter_mode: str | None = None) -> None:
    """Summary (+title, relevance, prescreen, depth) for each item, in parallel chunks. Retries missing ids once."""
    if not items:
        return
    system = claude.prompt(*SUMMARY_PROMPT)
    base_schema = claude.schema("summarize")

    def payload(chunk: list[Item]) -> dict:
        return {"section": section.title, "filter": filter_mode or "none", "items": [{
            "id": i.id, "date": i.day.isoformat(), "kind": i.kind, "title": i.title, "needs_title": not i.title,
            "source_type": "title_only" if i.word_count < 15 and i.content_origin == "feed" else i.content_origin,
            "word_count": i.word_count, "truncated": i.truncated, "content": i.content,
        } for i in chunk]}

    pending = list(items)
    for attempt in range(2):
        by_id = {i.id: i for i in pending}
        chunks = _even_chunks(pending, cfg.limits["summarize_chunk_size"])
        with ThreadPoolExecutor(max_workers=len(chunks)) as ex:
            futures = [ex.submit(claude.call, stage, system, payload(c),
                                 claude.with_enum(base_schema, ("items", "[]", "id"), [i.id for i in c]))
                       for c in chunks]
            for fut in futures:
                try:
                    out = fut.result()
                except LlmError as e:
                    log.error("%s: %s", section.key, e)
                    continue
                for r in out.get("items", []):
                    item = by_id.pop(r.get("id"), None)
                    if not item:
                        continue
                    item.summary = re.sub(r"\s+", " ", r.get("summary") or "").strip() or None
                    if not item.title and r.get("title"):
                        item.gen_title = r["title"].strip()[:100]
                    item.relevant = bool(r.get("relevant", True)) if filter_mode else True
                    item.prescreen = r.get("prescreen", "candidate")
                    item.reason = (r.get("skip_reason") or "").strip() or None
                    item.depth = r.get("depth")
                    item.full_text_adds = (r.get("full_text_adds") or "").strip() or None
        pending = list(by_id.values())
        if not pending:
            break
        log.warning("%s: %d items missing from model output (attempt %d)", section.key, len(pending), attempt + 1)
    for i in pending:          # still missing after the retry
        i.llm_error = "summary unavailable"
    fix_summaries(claude, cfg, items)


# ---------------------------------------------------------------------------------------------------------------
# IT news
# ---------------------------------------------------------------------------------------------------------------
def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (t or "").lower()).strip()


def recent_it_titles(data_dir: Path, today: str, n: int = 3) -> list[str]:
    """IT picks of the last n reports, so a story isn't picked again tomorrow from another outlet."""
    titles: list[str] = []
    for f in sorted(data_dir.glob("*.json"), reverse=True):
        if f.stem[:10] >= today:
            continue
        try:
            d = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        titles += [i.get("title") or "" for s in d.get("sections", []) if s.get("key") == IT_NEWS for i in s["items"]]
        n -= 1
        if n == 0:
            break
    return [t for t in titles if t]


def it_pick_count(window_days: float, lim: dict) -> int:
    """Base number of IT stories: 5 for a normal day, a bit more for multi-day windows."""
    return min(lim["it_picks_max"], lim["it_picks_min"] + max(0, round(2 * (window_days - 1.5))))


def run_it_news(claude: Claude, cfg: Config, http: Http, section: Section, raw: list[Item],
                already_covered: list[str], window: Window) -> tuple[list[Item], str]:
    """Haiku pre-filter -> Sonnet rating -> rules enforced in code -> fetch text -> summary."""
    lim = cfg.limits
    seen_titles, uniq = set(), []
    for i in raw:                                   # identical headlines from two feeds: keep one
        key = _norm_title(i.title)
        if key and key not in seen_titles:
            seen_titles.add(key)
            uniq.append(i)
    raw = uniq[:200]
    if not raw:
        return [], ""
    hid = {f"h{n}": i for n, i in enumerate(raw, 1)}          # short ids: fewer output tokens, fewer copy errors
    pre_schema = claude.schema("it_prefilter")
    pre_schema = claude.with_enum(pre_schema, ("stories", "[]", "primary_id"), list(hid))
    pre_schema = claude.with_enum(pre_schema, ("stories", "[]", "other_ids", "[]"), list(hid))
    pre = claude.call(
        "it_prefilter",
        claude.prompt("stages/it_prefilter.md", "it_news_rules.md", "profile.md").replace(
            "{max_candidates}", str(lim["it_candidates_max"])),
        {"window_days": round(window.days, 1), "already_covered": already_covered[:200],
         "headlines": [{"id": h, "title": i.title, "outlet": i.extra.get("outlet"),
                        "on_techmeme": i.extra.get("on_techmeme", False), "description": i.content[:220]}
                       for h, i in hid.items()]},
        pre_schema,
    )
    stories: dict[str, dict] = {}
    for n, s in enumerate(pre.get("stories", [])[: lim["it_candidates_max"]], 1):
        members = [m for m in dict.fromkeys([s.get("primary_id"), *s.get("other_ids", [])]) if m in hid]
        if members:
            stories[f"s{n}"] = {"title": s["title"], "why": s.get("why", ""), "members": [hid[m] for m in members]}
    if not stories:
        return [], "No IT stories passed the first filter."

    pick_schema = claude.with_enum(claude.schema("it_pick"), ("picks", "[]", "story_id"), list(stories))
    picked = claude.call(
        "it_pick",
        claude.prompt("stages/it_pick.md", "it_news_rules.md", "profile.md")
        .replace("{min_picks}", str(lim["it_picks_min"])).replace("{max_picks}", str(lim["it_picks_max"])),
        {"window_days": round(window.days, 1), "already_covered": already_covered[:200],
         "stories": [{"id": sid, "title": s["title"], "why": s["why"],
                      "outlets": sorted({m.extra.get("outlet") or "?" for m in s["members"]}),
                      "on_techmeme": any(m.extra.get("on_techmeme") for m in s["members"]),
                      "headlines": [m.title for m in s["members"]][:5]} for sid, s in stories.items()]},
        pick_schema,
    )

    # Rules enforced in code (prompt-only rules were broken in early runs)
    per_company: dict[str, int] = {}
    base, keep, kept_ids = it_pick_count(window.days, lim), [], set()
    for pk in picked.get("picks", []):
        sid = pk.get("story_id")
        if sid not in stories or sid in kept_ids:
            continue
        if 0 <= pk.get("duplicate_of_covered", -1) < len(already_covered):
            log.info("IT: drop %s (duplicate of covered item)", sid)
            continue
        company = (pk.get("company") or "").strip().lower()
        if company and per_company.get(company, 0) >= 2:
            log.info("IT: drop %s (third story about %s)", sid, company)
            continue
        importance = int(pk.get("importance") or 3)
        if importance <= 1 or (len(keep) >= base and importance < 4) or len(keep) >= lim["it_picks_max"]:
            continue
        per_company[company] = per_company.get(company, 0) + 1
        kept_ids.add(sid)
        keep.append(pk)

    items: list[Item] = []
    for pk in keep:
        s = stories[pk["story_id"]]
        primary = s["members"][0]
        items.append(Item(
            id=make_id("it-story", primary.url), source=section.key, url=primary.url,
            published=primary.published, day=primary.day, title=primary.title, kind=pk.get("priority"),
            content="\n".join(f"{m.extra.get('outlet')}: {m.title}. {m.content}" for m in s["members"]),
            content_origin="feed",
            extra={"outlets": list(dict.fromkeys(m.extra.get("outlet") or "?" for m in s["members"])),
                   "coverage": len(s["members"]), "why": pk.get("why", ""), "importance": pk.get("importance"),
                   "company": pk.get("company"), "candidates": [m.url for m in s["members"]]},
        ))

    def load_text(item: Item) -> None:
        for url in item.extra["candidates"]:
            try:
                text = extract_article(http.get_text(url, retries=2), cfg.limits["article_max_words"])
            except Exception:
                continue
            if len(text.split()) >= 120:
                item.content, item.content_origin, item.url = text, "article", url
                return

    http.map(load_text, items)
    summarize_items(claude, cfg, section, items, stage="it_summarize")
    for i in items:
        i.prescreen, i.reason = "candidate", None      # every pick goes to the final review
    return items, picked.get("note", "")


# ---------------------------------------------------------------------------------------------------------------
# Review: stars, verdicts, highlights
# ---------------------------------------------------------------------------------------------------------------
def read_limits(window: Window) -> tuple[int, int]:
    budget = max(1, round(3 * window.days))
    return budget, budget + 2


def review(claude: Claude, cfg: Config, sections: list[dict], window: Window) -> list[dict]:
    """Sonnet rates every item 1-5 stars (4-5 = read) and writes 3-4 highlights. Limits enforced in code."""
    all_items: dict[str, Item] = {i.id: i for s in sections for i in s["items"] if not i.llm_error}
    if not all_items:
        return []
    budget, cap = read_limits(window)
    payload = {
        "window_days": round(window.days, 1), "read_budget": budget, "read_cap": cap,
        "items": [{"id": i.id, "section": s["title"], "date": i.day.isoformat(), "kind": i.kind, "depth": i.depth,
                   "word_count": i.word_count, "title": i.display_title, "summary": i.summary,
                   "full_text_adds": i.full_text_adds, "prescreen": i.prescreen,
                   "skip_reason": i.reason if i.prescreen == "skip" else "",
                   **({"outlets": i.extra.get("outlets"), "it_priority": i.kind} if s["key"] == IT_NEWS else {})}
                  for s in sections for i in s["items"] if i.id in all_items],
    }
    ids = list(all_items)
    schema = claude.with_enum(claude.schema("review"), ("items", "[]", "id"), ids)
    schema = claude.with_enum(schema, ("highlights", "[]", "item_ids", "[]"), ids)
    # ~60 output tokens per item: give long windows (many items) proportionally more time
    timeout = max(cfg.claude["timeout_seconds"], 3 * len(ids) + 120)
    out = claude.call("review", claude.prompt(*REVIEW_PROMPT), payload, schema, timeout=timeout)

    order: dict[str, int] = {}
    for pos, r in enumerate(out.get("items", [])):
        item = all_items.get(r.get("id"))
        if not item or item.id in order:
            continue
        order[item.id] = pos
        item.stars = min(5, max(1, int(r.get("stars") or 2)))
        item.verdict = "read" if item.stars >= 4 else "skip"          # stars decide; verdict must agree
        item.reason = (r.get("reason") or "").strip() or None
    for item in all_items.values():
        if item.id not in order:                                      # model skipped it: keep first-pass view
            item.stars, item.verdict = (2 if item.prescreen == "skip" else 3), "skip"
            item.reason = item.reason if item.prescreen == "skip" and item.reason else "not rated in review"

    # Hard cap on must-reads: keep the best by (stars, model order), demote the rest to 3 stars
    reads = sorted((i for i in all_items.values() if i.verdict == "read"),
                   key=lambda i: (-i.stars, order.get(i.id, 999)))
    for i in reads[cap:]:
        log.info("review: demoting %s (over read cap %d)", i.id, cap)
        i.stars, i.verdict, i.reason = 3, "skip", "lower priority today"

    for item in all_items.values():                                   # normalize reason wording
        if item.verdict == "read":
            reason = item.reason or "it was rated a must-read in review"
            if not reason.lower().startswith("you should read"):
                reason = "You should read because " + reason[0].lower() + reason[1:]
            item.reason = reason[:160]
        else:
            if not item.reason or item.reason.lower().startswith("you should read"):
                item.reason = "summary is enough"
            item.reason = item.reason[:60]

    highlights = []
    for h in out.get("highlights", [])[:4]:
        if h.get("text"):
            ids_ = [i for i in h.get("item_ids", []) if i in all_items][:3]
            highlights.append({"text": h["text"].strip(), "item_ids": ids_})
    return highlights


def sort_items(items: list[Item]) -> list[Item]:
    """Stars first (must-reads on top), then newest."""
    return sorted(items, key=lambda i: (-(i.stars or 0), -i.day.toordinal(),
                                        -(i.published.timestamp() if i.published else 0)))


def carry_over_failed_sections(results: dict[str, SourceResult], today_data: Path) -> None:
    """Same-day re-run: a source that fails now keeps the items it had in this morning's report (they are already
    in seen.json, so without this they would disappear for good)."""
    try:
        previous = {s["key"]: s["items"] for s in json.loads(today_data.read_text())["sections"]}
    except (OSError, json.JSONDecodeError, KeyError):
        return
    for key, r in results.items():
        if r.stats.status not in OK_STATUSES and key != IT_NEWS and previous.get(key):
            have = {i.id for i in r.items}
            r.items += [Item.from_dict(d) for d in previous[key] if d["id"] not in have]
            r.stats.note = "Source unreachable now; showing items from this morning's run."


# ---------------------------------------------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------------------------------------------
def run(cfg: Config, opts: RunOptions) -> RunResult:
    if opts.only and not opts.dry_run:
        raise ValueError("--only is for dry runs; a real run must cover every enabled source")
    if opts.since and not opts.dry_run:
        raise ValueError("--since is for dry runs; a real run always continues from where the last report ended")
    now = (opts.now or datetime.now(cfg.tz)).astimezone(cfg.tz).replace(microsecond=0)
    p = paths(cfg, opts.dry_run)
    store = StateStore(p["state"])
    with store.lock():
        http = Http(cfg.http["user_agent"], cfg.http["timeout_seconds"], cfg.http["max_parallel"],
                    retries=cfg.http.get("retries", 5), backoff_base=cfg.http.get("backoff_base_seconds", 2))
        try:
            return _pipeline(cfg, opts, now, p, store, http)
        except RunFailed:
            raise
        except Exception as e:      # unexpected crash: record it so failures are counted (Phase 2 notifies)
            if opts.dry_run:
                raise
            log.exception("real run crashed")
            store.append_run({"run_id": f"{now:%Y%m%d-%H%M%S}-crash", "ok": False, "mode": "real",
                              "started_at": now.isoformat(timespec="seconds"), "report_date": now.date().isoformat(),
                              "errors": [f"{type(e).__name__}: {e}"]})
            store.record_failure(now.date().isoformat(), f"{type(e).__name__}: {e}"[:500])
            raise RunFailed(f"{type(e).__name__}: {e}") from e
        finally:
            http.close()


def _atomic_write(path: Path, text: str) -> None:
    write_private(path, text)


def _pipeline(cfg: Config, opts: RunOptions, now: datetime, p: dict[str, Path], store: StateStore,
              http: Http) -> RunResult:
    run_id = f"{now:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:4]}"
    window, is_rerun = compute_window(cfg, store, opts, now)
    log.info("run %s (%s) window %s", run_id, "dry-run" if opts.dry_run else "real", window.label())
    usage = Usage()
    claude = Claude(cfg, usage)
    page_cache = JsonCache(cfg.cache_dir / "page_dates.json")
    errors: list[str] = []
    llm_failed = False
    t_start = datetime.now(cfg.tz)
    today = now.date().isoformat()

    # ---- S1 fetch ------------------------------------------------------------------------------------------
    src_state = store.sources()
    seen = {} if opts.dry_run else store.seen()
    sections_cfg = [s for s in cfg.sections if not opts.only or s.key in opts.only]

    def fetch_one(section: Section) -> SourceResult:
        win = source_window(window, src_state.get(section.key, {}), opts.dry_run)
        try:
            res = FETCHERS[section.key](Context(cfg, http, win, section, page_cache))
        except Exception as e:                      # one broken source never kills the run
            return fail(SourceResult(section.key), e)
        unique: dict[str, Item] = {}
        for i in res.items:                         # same link twice (e.g. in two daily reading lists): keep first
            unique.setdefault(i.id, i)
        res.items = list(unique.values())
        res.stats.found_in_window = len(res.items)
        # dedup against earlier reports; items first reported today are allowed back (today's file is rewritten)
        res.items = [i for i in res.items if seen.get(i.id, today) == today]
        res.stats.new_after_dedup = len(res.items)
        if res.stats.status == "ok" and not res.items:
            res.stats.status = "empty"
        return res

    with ThreadPoolExecutor(max_workers=len(sections_cfg) or 1) as ex:
        results = dict(zip([s.key for s in sections_cfg], ex.map(fetch_one, sections_cfg)))
    page_cache.save()
    if is_rerun:
        carry_over_failed_sections(results, p["data"] / f"{today}.json")
    for key, r in results.items():
        if r.stats.status not in OK_STATUSES:
            errors.append(f"{key}: {r.stats.status}: {r.stats.error}")
    if not opts.dry_run and results and not any(r.stats.status in OK_STATUSES for r in results.values()):
        # nothing fetched (offline, DNS, captive portal): an empty report would end today's attempts for nothing
        msg = "no source could be fetched (offline?)"
        store.append_run({"run_id": f"{run_id}-offline", "ok": False, "mode": "real",
                          "started_at": now.isoformat(timespec="seconds"), "report_date": today, "errors": [msg]})
        store.record_failure(today, msg)
        raise RunFailed(msg)

    # ---- S2 summaries ‖ IT news ------------------------------------------------------------------------------
    it_section = next((s for s in sections_cfg if s.key == IT_NEWS), None)
    it_raw = results[IT_NEWS].items if it_section else []
    main_sections = [s for s in sections_cfg if s.key != IT_NEWS]
    already = [i.title or i.content[:100] for s in main_sections for i in results[s.key].items]
    already += recent_it_titles(p["data"], today)
    it_items: list[Item] = []
    it_note = ""
    with ThreadPoolExecutor(max_workers=len(main_sections) + 1) as ex:
        it_future: Future | None = None
        if it_section and it_raw:
            it_future = ex.submit(run_it_news, claude, cfg, http, it_section, it_raw, already, window)
        sum_futures = [ex.submit(summarize_items, claude, cfg, s, results[s.key].items,
                                 filter_mode="ai_cloud" if s.key == "google_dev_blog" else None)
                       for s in main_sections]
        for f in sum_futures:
            f.result()
        if it_future:
            try:
                it_items, it_note = it_future.result()
            except LlmError as e:
                llm_failed = True
                errors.append(f"llm it_news: {e}")
                it_note = "IT news selection failed this run."

    # ---- assemble sections ----------------------------------------------------------------------------------
    sections: list[dict] = []
    for s in sections_cfg:
        r = results[s.key]
        items = it_items if s.key == IT_NEWS else r.items
        filtered = [i for i in items if not i.relevant]
        items = [i for i in items if i.relevant]
        note = it_note if s.key == IT_NEWS else (r.stats.note or (r.stats.error if r.stats.status == "partial" else None))
        sections.append({"key": s.key, "title": s.title, "url": s.url, "items": items,
                         "filtered_out": len(filtered), "stats": r.stats, "note": note,
                         "is_release_notes": s.key in RELEASE_NOTE_SECTIONS, "is_it_news": s.key == IT_NEWS})

    all_items = [i for s in sections for i in s["items"]]
    unsummarized = [i.id for i in all_items if i.llm_error]
    if unsummarized:
        errors.append(f"{len(unsummarized)} item(s) without summary")

    # ---- S3 review ------------------------------------------------------------------------------------------
    highlights: list[dict] = []
    try:
        highlights = review(claude, cfg, sections, window)
    except LlmError as e:
        llm_failed = True
        errors.append(f"llm review: {e}")
        for i in all_items:
            i.verdict, i.reason = i.verdict or "skip", i.reason or "not reviewed"

    for s in sections:
        s["items"] = sort_items(s["items"])
        st = s["stats"]
        st.kept = len(s["items"])
        st.read_fully = sum(1 for i in s["items"] if i.verdict == "read")
        if s["items"]:
            newest = max(s["items"], key=lambda i: i.day)
            st.latest_item_date, st.latest_item_title = newest.day.isoformat(), newest.display_title

    # Real runs must be (nearly) complete before state advances; dry runs always render what they have.
    too_many_missing = len(unsummarized) > MAX_UNSUMMARIZED_SHARE * max(1, len(all_items))
    if not opts.dry_run and (llm_failed or too_many_missing):
        store.append_run(_record(run_id, opts, window, now, t_start, sections, usage, errors, ok=False))
        store.record_failure(today, "; ".join(errors)[:500])
        store.update_sources({s["key"]: s["stats"] for s in sections}, now, window, advance=False)
        _atomic_write(p["reports"] / "index.html", render_index(cfg, p["reports"], store, dry_run=opts.dry_run))
        raise RunFailed("; ".join(errors))

    # ---- S4 render + persist --------------------------------------------------------------------------------
    record = _record(run_id, opts, window, now, t_start, sections, usage, errors, ok=True)
    report = {
        "run_id": run_id, "mode": "dry-run" if opts.dry_run else "real",
        "generated_at": now.isoformat(), "window": {"start": window.start.isoformat(), "end": window.end.isoformat(),
                                                   "label": window.label(), "days": round(window.days, 2)},
        "highlights": highlights,
        "sections": [{**{k: v for k, v in s.items() if k not in ("items", "stats")},
                      "items": [i.to_dict() for i in s["items"]], "stats": asdict(s["stats"])} for s in sections],
        "errors": errors, "usage": record["usage"],
    }
    stamp = f"{now:%Y-%m-%d_%H%M}" if opts.dry_run else today
    data_path = p["data"] / f"{stamp}.json"
    _atomic_write(data_path, json.dumps(report, ensure_ascii=False, indent=1))

    index_path = p["reports"] / "index.html"
    moved = [] if opts.dry_run else archive_previous(p["reports"], p["archive"], keep=f"{today}.html")
    report_path = p["reports"] / f"{stamp}.html"
    # absolute link: still works after the report is moved into archive/<YYYY>/
    _atomic_write(report_path, render_report(cfg, report, index_href=index_path.as_uri(), root_rel=""))
    for step in (lambda: write_catalog(p["reports"], p["data"]),     # prev/next buttons + Reports menu know the new report
                 lambda: rerender_moved(cfg, p, moved)):               # moved reports need their new path to catalog.js
        try:
            step()
        except Exception as e:                  # navigation is a convenience: it must never fail a finished report
            log.warning("report navigation update failed: %s", e)

    store.append_run(record)
    if not opts.dry_run:
        store.save_state({
            "last_report_date": today,
            "last_window_start": window.start.isoformat(),
            "last_window_end": window.end.isoformat(),
            "last_report_file": str(report_path.relative_to(cfg.root)),
            "last_success_at": datetime.now(cfg.tz).isoformat(timespec="seconds"),
            "failed_attempts_today": 0,
            "last_error": None,
        })
        store.add_seen([i.id for r in results.values() for i in r.items] + [i.id for i in it_items], today)
        try:
            backup_library(library_path(cfg), today)
        except OSError as e:
            log.warning("library backup failed: %s", e)
    store.update_sources({s["key"]: s["stats"] for s in sections}, now, window)
    _atomic_write(index_path, render_index(cfg, p["reports"], store, dry_run=opts.dry_run))

    if opts.open_browser and cfg.output.get("open_in_browser", True):
        open_in_browser(cfg, report_path)
    return RunResult(report_path, data_path, record, errors)


def _record(run_id, opts, window, now, t_start, sections, usage: Usage, errors, ok: bool) -> dict:
    t_end = datetime.now(t_start.tzinfo)
    return {
        "run_id": run_id, "ok": ok, "mode": "dry-run" if opts.dry_run else "real",
        "started_at": t_start.isoformat(timespec="seconds"), "report_date": now.date().isoformat(),
        "duration_s": round((t_end - t_start).total_seconds(), 1),
        "window_start": window.start.isoformat(), "window_end": window.end.isoformat(),
        "window_days": round(window.days, 2),
        "sources": {s["key"]: {"status": s["stats"].status, "found": s["stats"].found_in_window,
                               "new": s["stats"].new_after_dedup, "kept": len(s["items"]),
                               "read": sum(1 for i in s["items"] if i.verdict == "read")} for s in sections},
        "totals": {"items": sum(len(s["items"]) for s in sections),
                   "read": sum(1 for s in sections for i in s["items"] if i.verdict == "read")},
        "usage": {"by_model": usage.summary(), "by_stage": usage.by_stage(), "calls": usage.to_list()},
        "errors": errors,
    }


def archive_previous(reports_dir: Path, archive_dir: Path, keep: str) -> list[Path]:
    """Move every dated report except today's into archive/<YYYY>/. Returns the new locations."""
    moved = []
    for f in reports_dir.glob("????-??-??.html"):
        if f.name == keep:
            continue
        dest = archive_dir / f.name[:4]
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), dest / f.name)
        moved.append(dest / f.name)
    return moved


def rerender_moved(cfg: Config, p: dict[str, Path], moved: list[Path]) -> None:
    """Re-render archived reports from their saved data (no Claude call) so their links to the catalog still work."""
    for f in moved:
        data = p["data"] / f"{f.stem}.json"
        if not data.exists():
            log.warning("no saved data for %s: its prev/next buttons will not work until `render --all`", f.name)
            continue
        try:
            _atomic_write(f, render_report(cfg, json.loads(data.read_text()), index_href=(p["reports"] / "index.html").as_uri(),
                                           root_rel=root_rel(f, p["reports"])))
        except (OSError, ValueError, KeyError) as e:
            log.warning("could not re-render %s (%s): its navigation may be stale until `render --all`", f.name, e)


def open_in_browser(cfg: Config, path: Path) -> None:
    app = cfg.output.get("browser_app", "Google Chrome")
    r = subprocess.run(["open", "-a", app, str(path)], capture_output=True)
    if r.returncode != 0:
        subprocess.run(["open", str(path)])
