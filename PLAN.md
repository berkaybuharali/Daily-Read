# Daily Read — Plan

Status: **PHASE 1 — BUILT, in review/iteration** (approved 2026-10-02). Phase 2 (automation) not started.
Last updated: 2026-10-02 (rev 7)

> Rev 7 (2026-10-02): §4/§5 rewritten to the current all-Sonnet design; star rubric with "judge the idea, not
> the vendor"; empty sections sorted last; wider fluid layout; private sources (`config.local.yaml` +
> `local_sources/`, git-ignored) for open-sourcing; source health recorded on failed real runs; README + MIT licence.

> Rev 6: 1–5★ per item (4–5 = read, enforced in code), tables sorted by stars; all stages on Sonnet (measured faster
> and better than Haiku); own CSS design (Nunito/Fredoka, dark default, left sidebar filters); `enabled:` flag per
> source; review findings fixed (date lookback, stable ids, partial sources, PID lock, IT rules in code, injection
> hardening).

> Rev 5 changes: highlights (3–4 bullets) at the top instead of a ✅ shortlist; Sonnet does the overall review of
> everything; summaries moved to Sonnet after a measured comparison; extended thinking off for mechanical stages;
> HTTP retries 5× with exponential backoff + curl fallback; Pico CSS template; CLAUDE.md with knowledge base and
> "adding a source" playbook; usage history page.

## 1. Goal

Every day, the first time the Mac is on, generate one local HTML report of everything new since the previous
report from a fixed set of sources, and open it in Chrome. Each source gets its own table. Claude writes neutral
summaries and a strict "read fully?" verdict, and picks the most important IT-industry news. 100% local.

## 2. Decisions made

| Topic | Decision |
|---|---|
| Delivery | Local HTML only, opened in **Google Chrome** |
| LLM engine | `claude -p` (headless Claude Code) on the user's subscription — no API key |
| Models | Measured 2026-10-02: **Sonnet for every stage** (better and faster than Haiku; similar list-price cost because Haiku was verbose). Thinking on only for IT pick and the final review. Per-stage in `config.yaml` |
| Parallelism | Multiple `claude -p` processes in parallel (default cap 4) + parallel HTTP fetching |
| Timezone | Europe/Amsterdam |
| Window | End of previous successful report → now. First run: 2026-10-01 00:00 Amsterdam → now |
| Missed days | Tracked; next report covers the whole gap. Best effort for short-history feeds (not crucial) |
| Rows | One row per item: each article in a reading-list digest; **each individual release note** (a day with 3 notes = 3 rows, each with date + type badge); each Claude Code version (2 versions in a day = 2 rows); each post |
| Columns | Title · Summary · Read fully? · Link |
| Summary | ≤300 chars, concise, educational, neutral, no judgement, must not change meaning |
| Verdict | `✅ You should read because …` or `Skip — <few words why>` |
| Strictness | ~0–3 ✅ per day across all sections; more only on exceptional days |
| IT news | Techmeme + SiliconANGLE + TechCrunch (AI + Fundraising). **5–10 unique stories per report**, model decides how many. Priority: GCP, Anthropic/Claude, Netherlands, Europe, Türkiye, or something big. Whole window (not per day) |
| Empty section | "No releases/posts in this window. Last one: <date>" |
| History | Latest report in `reports/`; previous moved to `reports/archive/<YYYY>/` |
| HTML | Model returns JSON only; own CSS (Pico dropped after review), Nunito/Fredoka embedded, **dark default** + light/auto toggle, left sidebar (search, must-reads filter, sections), highlights on top, 1–5★ per item, tables sorted by stars |
| HTTP robustness | 429/5xx/network errors retried 5× with exponential backoff (2–32s, Retry-After honoured); 403/429 also retried via system `curl` (WordPress.com blocks Python TLS) |
| Open in Chrome | Yes — every manual run opens the report in Google Chrome (`--no-open` to skip) |
| Fetching | **Python only.** Claude never browses; it only reads text Python hands it (see §4.1) |
| Build order | **Phase 1:** run manually, iterate on output. **Phase 2:** automation, only after Phase 1 sign-off |

## 3. Sources (report order)

| # | Section | Fetch | Row = | History depth |
|---|---|---|---|---|
| 1 | A curated daily reading list (private source, `local_sources/`) | RSS | each linked article (~10–12/list), link → original | ~13 d |
| 2 | Gemini Enterprise release notes | RSS `https://docs.cloud.google.com/feeds/gemini-enterprise-release-notes.xml` | each note item | 50+ d |
| 3 | Claude Code release notes | Atom `https://github.com/anthropics/claude-code/releases.atom` | each version | ~13 d |
| 4 | Claude Platform release notes | HTML `https://platform.claude.com/docs/en/release-notes/overview` | each dated item | full |
| 5 | Anthropic & Claude blogs (combined) | Sitemaps: `anthropic.com/sitemap.xml` (/news /research /engineering) + `claude.com/sitemap.xml` (English /blog) → new URLs → publish date from page meta | each post | full |
| 6 | Gemini Enterprise Agent Platform release notes | RSS `https://cloud.google.com/feeds/gemini-enterprise-agent-platform-release-notes.xml` | each note item | 50+ d |
| 7 | BigQuery release notes | RSS `https://docs.cloud.google.com/feeds/bigquery-release-notes.xml` | each note item | 50+ d |
| 8 | Google Cloud Blog | RSS `https://cloudblog.withgoogle.com/rss/` (drop job postings) | each post | ~6 d |
| 9 | Google Developers Blog — AI & Cloud only | RSS `https://developers.googleblog.com/feeds/posts/default` (no dates in feed → date from article page) | each post | 50 items |
| 10 | Simon Willison — long-form only | Atom `https://simonwillison.net/atom/entries/` | each post | 50+ d |
| 11 | IT industry news — top 5–10 | Techmeme `https://www.techmeme.com/river` (HTML, ~5 d) + SiliconANGLE `https://siliconangle.com/feed/` + TechCrunch `/category/artificial-intelligence/feed/` + `/category/fundraising/feed/` | each selected story | 1–5 d (best effort) |

All verified returning current content on 2026-10-02.

## 4. Pipeline & parallelism

Current design (measured 2026-10-02). Stage → model and thinking settings live in `config.yaml`.

```
S1  FETCH + PARSE (Python, ≤16 parallel HTTP requests, 5 retries + curl fallback)
    every enabled source (public + local_sources/ plugins) → individual items → filter to the source's own
    window (covered_until → now) → drop already-seen → article text (≤1,200 words) where the feed has excerpts only
    IT news: headlines + feed descriptions only (~100–120)
          │
          ├────────────────────────────────────────────┐
S2a SUMMARIZE + PRE-SCREEN — Sonnet, parallel (≤4)      S2b IT PRE-FILTER — Sonnet
    one call per chunk of 8 items per section             drop promos / off-profile, cluster the same story,
    summary ≤300 chars, title for untitled notes,         flag duplicates of other sections
    depth / full_text_adds, prescreen candidate|skip,     → ≤30 candidate stories
    Dev Blog AI/Cloud filter                                    │
    lint (length, filler, hype) → SHORTEN pass — Sonnet   S3b IT PICK — Sonnet (thinking)
          │                                                pick 5–10; code enforces ≤2/company, ~5 normal day,
          │                                                importance ≥4 to go beyond, max 10
          │                                               → fetch picked articles → S3c summarize — Sonnet
          └──────────────────────┬──────────────────────────────┘
S4  REVIEW — Sonnet (thinking), one call over every item
    1–5★ per item using the star rubric; 4–5★ = "Read fully" (enforced in code);
    budget ≈3 per day of window, hard cap budget+2 (pipeline.read_limits); 3–4 highlights
S5  RENDER — Jinja2 (autoescape, fonts/CSS inlined) → HTML; sections sorted by config order, empty ones last
S6  PERSIST — real runs only when complete: state.json, seen.json, sources.json, runs.jsonl, archive
```

- Why Sonnet everywhere: in a 10-item comparison Sonnet (no thinking) took 10s with 0/10 summaries over 300 chars;
  Haiku (no thinking) took 17s with 5/10 over; Haiku (thinking) 169s. The IT pre-filter was 14s on Sonnet vs 64s
  on Haiku at similar list-price cost. Haiku remains a per-stage option.
- Extended thinking only for IT pick and the review (judgement stages); off elsewhere (the CLI default is on).
- Concurrency cap for `claude -p`: 4. Each call retries twice (5s, 10s) on errors or invalid JSON, except a
  subscription session/usage limit, which stops the run at once (next run catches up).
- Missing items in model output are re-asked once; a real run fails (state not advanced) if the review fails or
  too many items stay unsummarized. Dry runs always render what they have.

### 4.1 Why Python fetches, not Claude

- **Correct dates & completeness:** parsing feeds in code gives exact timestamps; no skipped or invented items.
- **Capacity:** Claude browsing (WebFetch) costs many tool round-trips and tokens per page; Python fetches are free.
- **Speed:** parallel HTTP requests in seconds vs. sequential agent browsing.
- **Safety:** with no tools, text on a web page can't make Claude do anything (prompt injection has nothing to act on).
- **Testable:** parsing failures show up as errors in `state/sources.json`, not as silently wrong summaries.
- Trade-off: when a site changes its layout, its parser needs fixing → detected via error/zero-items in state.

### 4.2 Measured cost

First real run (2026-10-02, 1.6-day window, 34 items + ~110 IT headlines): **13 Sonnet calls, ~155k input /
~12k output tokens, 47s**, ≈ $0.71 list-price equivalent (paid from the subscription). Most input is cache
creation (system prompts); `claude -p` caches by default. Multi-day windows scale roughly linearly.
Every call is logged per stage in `state/runs.jsonl`; `bin/dailyread usage` shows the history.

### `claude -p` lockdown (every call)

```
claude -p --model <sonnet|haiku> \
  --tools "" \                     # no built-in tools (no Bash/Read/Write/Web)
  --strict-mcp-config \            # no MCP servers
  --setting-sources "" \           # ignore user/project settings, hooks, plugins
  --no-session-persistence \       # don't store as sessions
  --output-format json --json-schema <stage schema> \
  --system-prompt-file <assembled prompt files> \
  --settings {"alwaysThinkingEnabled": <per stage>}  < input.json
```
The model only reads stdin and returns schema-validated JSON. Web text goes only in the stdin `<data>` payload,
after `prompts/untrusted_input.md`. `--bare` is not used (it may skip subscription auth).

## 5. Prompt pack (what the models get)

Everything is plain Markdown; tuning needs no code changes. Every prompt starts with `untrusted_input.md`.

| File | Purpose | Used by |
|---|---|---|
| `profile.md` | who the reader is, what they value, source bias, priority boosts (user-owned, git-ignored; `profile.example.md` is the public template) | summarize, IT, review |
| `summary_rules.md` | ≤300 chars, neutral, educational, faithful; curator-note and release-note handling | summarize, IT summarize, shorten |
| `verdict_criteria.md` | the core test ("does it change what the reader builds or how they think?"), 5★→1★ rubric, ± adjustments, calibration examples, skip reasons | summarize (pre-screen), review |
| `verdict_budget.md` | read budget / cap, reason format | review |
| `it_news_rules.md` | story clustering, priority boosts, "big story" bar, sizing | IT pre-filter, IT pick |
| `output_rules.md` | field rules, backtick-only inline markup, title conventions | summarize, review |
| `stages/*.md` | task per stage: summarize, it_prefilter, it_pick, review, shorten | — |

Schemas per stage are in `schemas/` and enforced via `--json-schema`; item ids are constrained to the ids sent.

Key rules (details in the files):
- **Summaries:** what it is / what changed + why it matters technically; exact names, versions, numbers, GA/Preview;
  no opinion, hype or filler openers. Reading-list rows summarize the linked article, not the curator's comment;
  if the article is unreachable, the curator note is used without its opinion and the row is labelled.
- **Stars:** judge the idea, not the vendor. 5★ new cross-vendor standards, original data, first-of-kind patterns,
  breaking changes; 4★ deep practitioner write-ups, explainers of emerging standards, new capability classes;
  3★ incremental launches; 2★ minor; 1★ noise. At most one pure product launch per day as a must-read.
  Title-only items are rated on their topic.
- **IT news:** unit = story; merge outlets; exclude promos and stories already covered; priority boosts from the
  profile; "big" stories qualify regardless of topic; 5 on a normal day, up to 10.

## 6. Output & design

```
reports/
  2026-10-05.html            latest report only
  index.html                 list of all reports: window, item count, ✅ count
  archive/2026/
    2026-10-02.html          previous reports (moved here on each new run)
```

**Design goals: looks good, easy to scan in 2 minutes, fully self-contained (inline CSS + small inline JS, no CDN).**

- **Header:** date, window covered ("Thu 1 Oct 00:00 → Fri 2 Oct 09:14"), totals (items, ✅).
- **Highlights** card at the top: 3–4 bullets written by Sonnet in the overall review, each linking to its items.
- **Sticky section nav:** chips per section with item counts (e.g. "BigQuery 3"); empty sections greyed.
- **Section cards** in fixed order, collapsible; empty sections collapse to one line
  ("No releases in this window · last one: 29 Sep 2026").
- **Table rows:** title (links to source, opens new tab) · summary · verdict · link icon.
  - Release notes: date + colored type badge (Feature / Changed / Fixed / Deprecated / Breaking),
    and GA / Preview badge when stated.
  - ✅ rows visually highlighted; Skip reasons muted grey.
  - Reading-list rows: small tag for article type ([blog] / [article] / [youtube]) as given in the list.
  - IT news rows: outlet name(s) + "covered by N outlets".
- **Filter toggle:** "Show only ✅" and a quick text filter.
- **Typography & theme:** system font stack, comfortable line length, light + dark mode
  (follows macOS setting), responsive (works on narrow windows), print-friendly.
- Low-priority grey note when a short-history feed may not reach back to window start.
- Look & feel iterated in Phase 1 — first step of Phase 1 is a static mockup with sample data for review.

## 7. State — detailed

All in the project folder, `state/`. Updated only after a fully successful report.
Phase 1 dry-run writes to `state-dryrun/` instead, so the real state is never touched.

**`state/state.json` — control (what decides "already ran today")**
```json
{
  "last_report_date": "2026-10-02",
  "last_window_start": "2026-10-01T00:00:00+02:00",
  "last_window_end":   "2026-10-02T09:14:00+02:00",
  "last_report_file":  "reports/2026-10-02.html",
  "last_success_at":   "2026-10-02T09:17:41+02:00",
  "failed_attempts_today": 0,
  "last_error": null
}
```

**`state/sources.json` — per-source health & stats (updated every run)**
```json
{
  "bigquery-release-notes": {
    "last_fetch_at": "2026-10-02T09:14:05+02:00",
    "last_fetch_status": "ok",                 // ok | http_error | parse_error | empty
    "http_status": 200,
    "items_in_feed": 30,
    "oldest_item_in_feed": "2026-07-29",
    "newest_item_in_feed": "2026-10-01",
    "found_in_window": 3,                      // after splitting into individual notes
    "new_after_dedup": 3,
    "kept": 3,                                 // after filters (e.g. Dev Blog AI/Cloud)
    "read_fully": 1,
    "latest_item_date": "2026-10-01",
    "latest_item_title": "[Change] Updated Simba JDBC driver",
    "consecutive_failures": 0,
    "last_error": null
  }
}
```

**`state/runs.jsonl` — one line per run (history)**
- run id, start/end, window, mode (manual / scheduled / dry-run), success/failure
- per-source counts (found / new / kept / ✅)
- per Claude call: stage, model, input/output tokens, duration, retries
- totals: items, ✅, Claude calls, tokens per model, total duration

**Other**
```
state/seen.json      IDs of every item already reported (+ first-seen date) → no duplicates
state/run.lock/      directory lock → prevents two runs at the same time
state/items/         (Phase 2, optional) collector store for short-history feeds
```

The report index page (`reports/index.html`) also gets a small **"Sources health"** table from
`sources.json`: last fetch status, newest item date, items this run — so a broken parser is visible.

## 8. Scheduling (Phase 2)

- **launchd** = built-in macOS scheduler. LaunchAgent plist in `~/Library/LaunchAgents/`:
  `RunAtLoad` (login) + `StartInterval 1800` (every 30 min; missed ticks during sleep fire once on wake).
- `run.sh` guard: lock → `last_report_date == today` → exit (ms, no Claude) → before 07:00 → exit → run.
- Failure: state not advanced → next tick retries. After 3 failures/day → macOS notification, stop until tomorrow.
- Success: move previous report to archive, write new report, update index + state, `open -a "Google Chrome"`.
- Manual: `bin/dailyread now` rebuilds today's report for the same window.

## 9. Build phases

**Phase 1 — Local, manual, iterate (no automation)**
- Build S1–S5, run by hand in dry-run mode.
- Iterate on: HTML look, summary quality, Haiku vs Sonnet, verdict strictness, IT news picks, prompt pack, parsing.
- Exit: user says output looks good.

**Phase 2 — Automation (after Phase 1 sign-off)**
- launchd, guard, lock, 07:00 rule, retries + notification, archive move, index, state, Chrome.
- Optional: collector job every 2h (no LLM) for short-history IT feeds — best effort.

## 10. Tech stack

Python 3 (uv venv) · feedparser · httpx · trafilatura · Jinja2 · bash · launchd ·
`claude` CLI 2.1.287 (`~/.local/bin/claude`) · Google Chrome (installed).

Build notes:
- python.org Python 3.13 has no SSL root certs configured (CERTIFICATE_VERIFY_FAILED) → uv venv + certifi.
- Full Chrome User-Agent string for all HTTP requests (some sites 403 a bare `Mozilla/5.0`).
- Techmeme timestamps `-0400` → normalize to Amsterdam.
- Hacker News blocked on the current network by a local DNS filter → not used.

## 11. Research log

- 2026-10-02: all section 1–10 feeds verified; Anthropic has no RSS (404) → sitemap + page meta.
- 2026-10-02: IT news research (22 feeds). Chosen: Techmeme, SiliconANGLE, TechCrunch AI + Fundraising.
  Rejected: VentureBeat (429), Reuters (no RSS/401), Bloomberg/FT/The Information/Stratechery (paywall/bot-block),
  Axios (politics), The Verge/Wired (consumer/noisy), The Register/BleepingComputer (not selected by user), HN (blocked).

## 12. Open items

- [ ] User approval of this plan → then build Phase 1 (starting with a static HTML mockup)
