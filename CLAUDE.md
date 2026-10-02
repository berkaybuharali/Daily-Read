# Daily Read — project guide for Claude

Local daily reading digest. Python fetches sources → `claude -p` (headless Claude Code, user's subscription, no API
key) summarizes and judges → Jinja2 renders a self-contained HTML report → opened in Chrome.
Full design and decisions: `PLAN.md`. Phase 1 (pipeline) and Phase 2 (launchd automation, `dailyread/schedule.py`) are built.

## How to run

```bash
bin/dailyread mockup                 # design only: renders fixtures/sample_report.json, no network, no Claude
bin/dailyread run --dry-run          # full pipeline, window = yesterday 00:00 → now, never touches state/
bin/dailyread run --dry-run --since 2026-10-01T00:00 --only gcloud_blog,bigquery_rn   # narrower test
bin/dailyread render data/dry-run/<stamp>.json   # re-render saved data after template/CSS changes (no Claude)
bin/dailyread usage [--dry-run]      # token usage history
bin/dailyread run                    # REAL run: advances state/, archives previous report
bin/dailyread schedule install|uninstall|status   # launchd LaunchAgent (Phase 2); `scheduled` = one tick
uv run --group dev pytest            # unit tests (no network, no Claude) — run after any change to dailyread/
```
Add `-v` to `run` for progress logs; `--no-open` to skip opening Chrome. Logs: `logs/<date>.log`.

## Layout

```
config.yaml              public sources (ordered = report order), models per stage, thinking, limits, http retries
config.local.yaml        private sources/overrides merged on top (git-ignored)
local_sources/           private source modules, auto-loaded (git-ignored except README + example)
dailyread/
  sources/               one module per source type; registry in sources/__init__.py (FETCHERS)
  pipeline.py            stages: fetch → summarize ‖ IT news → review → render → persist
  llm.py                 locked-down `claude -p` runner + usage records
  state.py               state.json / sources.json / seen.json / runs.jsonl / run.lock
  schedule.py            launchd tick (skip rules, retries, notification) + LaunchAgent install/status
  render.py, cli.py, http.py (retries + curl fallback), timeutil.py, models.py, config.py
prompts/                 profile.md (user-owned), untrusted_input.md (prepended to every prompt), summary_rules.md,
                         verdict_criteria.md, verdict_budget.md, it_news_rules.md, output_rules.md, stages/*.md
schemas/                 JSON schema per LLM stage (enforced via --json-schema)
templates/               report/index Jinja templates, report.css (own design, dark default), report.js,
                         vendor/fonts.css (Nunito + Fredoka embedded, OFL)
tests/                   pytest unit tests
fixtures/sample_report.json   mockup data
reports/                 real reports: latest <date>.html + archive/<YYYY>/ + index.html
reports/dry-run/         dry-run reports (kept for comparison) + index.html
data/                    report JSON per run (re-renderable)
state/  state-dryrun/    persistent state (real / dry-run)
cache/page_dates.json    publish dates scraped from article pages
```

## Rules that must not be broken

- **Claude never browses.** Python fetches everything; `claude -p` runs with `--tools ""`, `--strict-mcp-config`,
  `--setting-sources ""`, `--json-schema`. Untrusted web text goes only in the stdin `<data>` payload, never in
  the system prompt.
- **The model never emits HTML.** Templates autoescape; only backtick spans become `<code>`.
- **State advances only after a fully successful real run.** Dry runs use `state-dryrun/` only.
- **One row per item**: each article in a reading-list digest, each release note (even several per day), each version, each post.
- **Summaries**: ≤300 chars, neutral, educational, faithful (see `prompts/summary_rules.md`).
- **Stars & verdicts**: the final Sonnet review rates every item 1–5★; 4–5★ ⇔ "Read fully", 1–3★ ⇔ Skip (enforced in
  code). Budget ≈3 must-reads per day of window, hard cap budget+2 (`pipeline.read_limits`). Tables sort by stars.
- **IT news limits are enforced in code** (`run_it_news`) on the model's structured output: stories it flags as
  duplicates of other sections/recent reports are dropped, ≤2 per company (by the model's company name), ~5 per
  normal day, more only for importance ≥4, max 10.
- HTTP: full Chrome UA; 429/5xx/network errors retried 5× with exponential backoff; 403/429 also try system `curl`.
- Open-source hygiene: personal data (`profile.md`, `state*/`, `data/`, `reports/`, `logs/`, `cache/`) is
  git-ignored; `README.md` is the public how-to. Keep personal facts out of code and committed prompts.
- Keep the report self-contained (no CDN): fonts and CSS are inlined. Dark mode is the default.
- Feed URLs only reach `href` through the `safe_url` filter (http/https only).

## Adding a source (the user only gives a link + where it goes in the order)

When the user says e.g. *"add https://example.com/blog, put it after BigQuery"*:

0. **Public or private?** If the user doesn't say, ask. Public sources go in `dailyread/sources/` + `config.yaml`.
   Private ones (not pushed to GitHub) go in `local_sources/<name>.py` (module with `FETCHERS = {"<key>": fetch}`,
   auto-loaded; `RELEASE_NOTE_SECTIONS` optional) + a section in `config.local.yaml` with `position: first` /
   `after: <key>` / `before: <key>`. Steps 3–4 below then apply to those files instead, and the knowledge-base
   entry goes in `CLAUDE.local.md`.

1. **Probe** (don't guess):
   - Look for a feed: `<link rel="alternate" type="application/rss+xml|atom+xml">` in the page HTML, then common
     paths (`/feed/`, `/rss/`, `/atom.xml`, `/feed.xml`, `/index.xml`, `/rss.xml`). Google Cloud release notes:
     `https://docs.cloud.google.com/feeds/<product>-release-notes.xml` (check the page's `<link>`).
   - Fetch it with the project's UA (`curl -sL -A "<user_agent from config.yaml>"`), confirm HTTP 200 and valid XML.
   - Measure: items in feed, dates present (`pubDate`/`updated`), how many days back it reaches, whether entries
     contain full text, and whether article pages are fetchable (403/paywall?).
   - No feed → sitemap (`/sitemap.xml` with `<lastmod>`) + page metadata (`article:published_time` / JSON-LD
     `datePublished`), or HTML parsing of a dated list (see `claude_platform.py`).
2. **Pick the implementation** — reuse before writing new code:
   | Source shape | Reuse |
   |---|---|
   | Plain RSS/Atom blog with dates | `blog_feeds._generic` (add a thin `fetch_<name>` wrapper; `fetch_pages=True` if the feed only has excerpts) |
   | Google Cloud release notes feed | `gcp_release_notes.fetch` (just add the section — no code) |
   | GitHub releases | `claude_code.fetch` pattern (`<repo>/releases.atom`) |
   | Feed without dates | `blog_feeds.fetch_google_dev_blog` pattern (date from page, cached) |
   | No feed, has sitemap | `anthropic_blogs.py` pattern |
   | HTML page with dated headings | `claude_platform.py` pattern |
   | List-of-links digest (one row per link) | parse each post's HTML into one `Item` per link, curator note as fallback content (see `CLAUDE.local.md` if present) |
   | IT-news-style headline feed | add the URL to `it_news` `extra_urls` (feeds into the Top 5–10) |
3. **Register**: add `"<key>": <module>.<fetch>` to `FETCHERS` in `dailyread/sources/__init__.py`; if it's
   release notes, add the key to `RELEASE_NOTE_SECTIONS`.
4. **Configure**: add a section to `config.yaml` → `sections:` **at the position the user asked for** (list order =
   report order): `key`, `title`, `url` (+ `extra_urls` if needed).
5. **Filter if needed**: topic filter (like Dev Blog's AI/Cloud) → pass `filter_mode="ai_cloud"` for that key in
   `pipeline._pipeline` (the `summarize_items` call); URL filter → regex like `CLOUD_BLOG` in `blog_feeds.py`.
6. **Test**: `bin/dailyread run --dry-run --only <key> --since <date a few days back> --no-open -v` and check the
   section in the report; then a full dry run.
7. **Record** it in the knowledge base below (source table + any quirk) and in `PLAN.md` §3.

## Disabling, removing, reordering sources

- **Disable for future runs** (keep everything else): in `config.yaml`, set `enabled: false` on that section:
  ```yaml
  - key: simon_willison
    enabled: false        # ← not fetched, not shown, not in the sidebar
  ```
  Re-enable with `enabled: true`. The source then catches up from its last covered point (`state/sources.json`
  → `covered_until`), at most 14 days back.
- **Remove permanently**: delete the section from `config.yaml` (the fetcher code can stay).
- **Private sources / overrides**: `config.local.yaml` (git-ignored) is merged over `config.yaml`; a section with an
  existing key overrides fields (e.g. `enabled: false` for a public source), a new key is inserted by
  `position`/`after`/`before` (`config.merge_sections`). Private fetchers: `local_sources/*.py`.
- **Reorder**: move the section within `sections:` — list order is report and sidebar order. Sections with no
  items in a report move to the end (`render.order_sections`), keeping config order otherwise.
- **Disable one IT feed only** (e.g. TechCrunch fundraising): remove its URL from `it_news` → `extra_urls`.

## Tuning without code

- Interests / priorities → `prompts/profile.md` (user-owned, git-ignored; ask before rewriting it). A fresh clone
  falls back to `prompts/profile.example.md`. Star rubric + calibration examples → `prompts/verdict_criteria.md`.
- Summary style → `prompts/summary_rules.md`; what earns ✅ → `prompts/verdict_criteria.md`; budget and reason
  format → `prompts/verdict_budget.md` (hard cap in `pipeline.read_limits`);
  IT news selection → `prompts/it_news_rules.md`; stage tasks → `prompts/stages/*.md`.
- Models / thinking per stage, chunk sizes, limits → `config.yaml`.
- Sidebar label and icon per source → `short:` and `icon:` in the section.
- After prompt changes, compare with a dry run and check `bin/dailyread usage --dry-run` for token impact.

## Knowledge base

### Sources (verified 2026-10-02)

| Key | Source | Fetch | Notes / quirks |
|---|---|---|---|
| `gemini_enterprise_rn` | Gemini Enterprise RN | `docs.cloud.google.com/feeds/gemini-enterprise-release-notes.xml` | One entry per day; `<h3>Type</h3>` blocks; first `<p><strong>` = title when present |
| `claude_code_rn` | Claude Code | `github.com/anthropics/claude-code/releases.atom` | One row per version; ~10 versions in feed (~2 weeks) |
| `claude_platform_rn` | Claude Platform RN | HTML `platform.claude.com/docs/en/release-notes/overview` | `<h3 id="september-30-2026">` + `<ul><li>`; heading text contains icon glyphs → parse date from `id` |
| `anthropic_blogs` | anthropic.com news/research/engineering + claude.com/blog | Sitemaps (`lastmod`) → page `article:published_time` / JSON-LD | No RSS (404). claude.com dates are date-only ("Mar 13, 2026"); skip localized `/xx/blog/` |
| `agent_platform_rn` | Agent Platform RN | `cloud.google.com/feeds/gemini-enterprise-agent-platform-release-notes.xml` | Same format as other GCP RN |
| `bigquery_rn` | BigQuery RN | `docs.cloud.google.com/feeds/bigquery-release-notes.xml` | Notes often have no bold title → model writes one |
| `gcloud_blog` | Google Cloud Blog | `cloudblog.withgoogle.com/rss/` | ~6 days in feed; keep only `cloud.google.com/blog/` links |
| `google_dev_blog` | Google Developers Blog | `developers.googleblog.com/feeds/posts/default` | Items have **no dates** → JSON-LD `datePublished` from page, cached in `cache/page_dates.json`; AI/Cloud filter by model |
| `simon_willison` | Simon Willison (long-form) | `simonwillison.net/atom/entries/` | Full text in feed |
| `it_news` | Techmeme river + SiliconANGLE + TechCrunch AI & Fundraising | HTML `techmeme.com/river` + RSS | Techmeme times are US Eastern; river keeps ~5 days, feed.xml only ~11h. Others hold 1–2 days |

### Rejected / blocked (don't re-research)
VentureBeat (429 always), Reuters (no RSS, 401), Bloomberg / FT / The Information / Stratechery (paywall or bot
wall — headline only), Axios (politics, no tech feed), The Verge / Wired (consumer, noisy), Hacker News (hnrss.org
blocked on the user's network by a DNS filter), Anthropic `rss.xml` (404).

### Environment facts
- python.org Python 3.13 has no root certs → use the uv venv (httpx bundles certifi).
- `claude -p` defaults to extended thinking ON; it made Haiku ~10× slower. Set per stage in `config.yaml` → `claude.thinking`.
- Model comparison 2026-10-02 (10 items): Sonnet (no thinking) 10s, 0/10 over 300 chars, best fidelity;
  Haiku (no thinking) 17s, 5/10 over limit; Haiku (thinking) 169s. → summaries use Sonnet.
- IT pre-filter 2026-10-02: Haiku 64s / 5.2k output tokens vs Sonnet 14s / 1.6k (similar list-price cost) → Sonnet.
  All stages now use Sonnet; Haiku remains a per-stage option in `config.yaml`.
- `claude -p` caches prompts by default (shows as cache_creation tokens in runs.jsonl). `DISABLE_PROMPT_CACHING=1`
  did not reliably turn it off in a test (2026-10-02), so it is not set.
- Real runs cover at most 14 days (`MAX_WINDOW_DAYS`); review timeout scales with item count.
- Same-day re-run: a source failing now keeps this morning's items (`carry_over_failed_sections`).
- Scheduler (verified 2026-10-02): `claude -p` works under launchd (keychain login OK) with the plist's PATH and
  `CLAUDE_CONFIG_DIR` (copied from the installing shell). Usage-limit failures and offline checks don't count
  toward `max_failures_per_day`. A real run where no source could be fetched fails instead of making an empty report.
- Headless Chrome screenshots have a ~500px minimum width; test phone layouts inside a 390px iframe.
