# Example source list: the author's reading list

> **This is an example, not a recommendation or a config file.** It is the author's personal reading list,
> published so you can see what a good set of sources looks like and borrow from it. The author keeps it up to date
> as their own list changes. Your own sources go in `config.yaml` (public) or `config.local.yaml` (private,
> git-ignored); nothing in this file is read by the program.

To follow one of these, tell Claude Code *"add <feed URL>, put it after <source>"*, or follow
[Adding a source](README.md#adding-a-source).

**Status values:** `built-in` ships in `config.yaml` · `private` used by the author, fetcher not shipped ·
`candidate` evaluated, not added · `rejected` checked, not worth it.
**Fetch types:** `rss` plain RSS/Atom (reuses the generic fetcher) · `digest` list of links, one row per link ·
`gcp-rn` Google Cloud release-notes feed · `github` releases.atom · `sitemap` no feed, sitemap + page dates ·
`html` dated HTML page.

Keep this file up to date when you add, drop or evaluate a source (CLAUDE.md step 7 does this for Claude Code).

## Release notes

| Source | Feed | Type | Status | Notes |
|---|---|---|---|---|
| Gemini Enterprise | `docs.cloud.google.com/feeds/gemini-enterprise-release-notes.xml` | gcp-rn | built-in | One entry per day, several rows |
| Claude Code | `github.com/anthropics/claude-code/releases.atom` | github | built-in | One row per version |
| Claude Platform | `platform.claude.com/docs/en/release-notes/overview` | html | built-in | Dated headings, no feed |
| Agent Platform | `cloud.google.com/feeds/gemini-enterprise-agent-platform-release-notes.xml` | gcp-rn | built-in | |
| BigQuery | `docs.cloud.google.com/feeds/bigquery-release-notes.xml` | gcp-rn | built-in | |

## Vendor blogs

| Source | Feed | Type | Status | Notes |
|---|---|---|---|---|
| Anthropic + Claude blogs | `anthropic.com/sitemap.xml`, `claude.com/sitemap.xml` | sitemap | built-in | No RSS |
| Google Cloud Blog | `cloudblog.withgoogle.com/rss/` | rss | built-in | |
| Google Developers Blog | `developers.googleblog.com/feeds/posts/default` | rss | built-in | No dates in feed, read from pages; AI/Cloud filter |

## Curated digests

| Source | Feed | Type | Status | Notes |
|---|---|---|---|---|
| SRE Weekly | `sreweekly.com/feed/` | digest | built-in | ~8 links per weekly issue; postmortems, reliability |
| Seroter Daily Reading List | `seroter.com/category/daily-reading-list/feed/` | digest | private | Daily; leans towards Google announcements |

## Independent writers (grouped)

Individual bloggers who post now and then are kept together in one report section, `independent_writers`, as a
`writers:` list in the config (a name and a feed URL each). Adding a person is a one-line change.

| Writer | Feed | Focus | Status |
|---|---|---|---|
| Simon Willison | `simonwillison.net/atom/entries/` | Practical LLM use, tools, daily notes | built-in |
| Joe Shirey | `joe-shirey.com/feed.xml` | Agents, GEO experiments, Google Cloud | built-in |
| Marc Brooker | `brooker.co.za/blog/rss.xml` | Queueing, tail latency, database design | built-in |
| Martin Alderson | `martinalderson.com/feed.xml` | AI economics, agent security | built-in |
| Max Woolf | `minimaxir.com/index.xml` | Agent benchmarks, sceptical of hype | built-in |
| Martin Fowler | `martinfowler.com/feed.atom` | Software architecture, agentic engineering (excerpt feed) | built-in |
| Gregor Hohpe | `architectelevator.com/feed.xml` | Architecture and strategy (excerpt feed, last post Feb 2026) | built-in |
| Kent Beck | `tidyfirst.substack.com/feed` | Design economics, augmented coding (excerpt feed) | built-in |
| Mark Callaghan | `smalldatum.blogspot.com/feeds/posts/default` | Database benchmarking methodology | built-in |
| Dan Luu | `danluu.com/atom.xml` | Measurement, benchmark scepticism, empirical engineering | built-in |
| Lorin Hochstein | `surfingcomplexity.blog/feed` | Incident analysis and resilience of cloud systems (2–3 posts a week) | built-in |
| Murat Demirbas | `muratbuffalo.blogspot.com/feeds/posts/default` | Distributed systems, TLA+, data agents (about weekly) | built-in |
| Matt Rickard | `blog.matt-rickard.com/feed` | Agent infrastructure, short posts (last post July 2026) | built-in |
| Alex Ewerlöf | `blog.alexewerlof.com/feed` | Reliability engineering for AI systems | built-in |

## News

| Source | Feed | Type | Status | Notes |
|---|---|---|---|---|
| Techmeme river | `techmeme.com/river` | html | built-in | Feeds the Top 5–10 IT news with SiliconANGLE and TechCrunch AI/Fundraising |

## Candidates

Evaluated 2026-10, from recent posts only. (Brooker, Alderson and Woolf, evaluated the same way, are now built in under Independent writers.) "Posts/week" is what you would actually see.

| Source | Feed | Posts/week | Worth reading | Verdict |
|---|---|---|---|---|
| Mark Williams-Cook | `markwilliamscook.substack.com/feed` | ~0.2 | ~40–50% | Sceptical GEO experiments. Niche, bursty |

## Rejected

VentureBeat (429 always), Reuters (no RSS), Bloomberg / FT / The Information / Stratechery (paywall or bot wall),
Axios, The Verge / Wired (consumer, noisy), Hacker News via hnrss.org (blocked on the author's network).
