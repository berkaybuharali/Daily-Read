# Design decisions

Every non-obvious technical choice in the Read Later / Favorites, report navigation and helper work, with the reason and
how it was checked. Status was re-audited on 2026-10-04 by independent reviewers (code, security, operations, UI) and two
fact-checkers who verified each row against current primary sources; their corrections are applied below. "Verified" means checked against a primary source or by a real run; the date is when. Anything not
marked verified is an assumption and says so. Re-check the browser rows after each Chrome major release.

Goal behind all of them: a **seamless** experience (no clicks to set up or save), **security**, and **no performance or
battery impact** on the Mac.

## Helper and launchd

| # | Decision | Why | Status |
|---|---|---|---|
| H1 | A tiny local HTTP helper writes `state/library.json`; the page `fetch`es it | A static page cannot write files; File System Access needs a picker click (and possibly a permission prompt on later visits) and is Chrome-only | Verified 2026-10-04 (MDN, Chrome docs) |
| H2 | launchd socket activation: launchd holds the port, starts the helper on the first connection | No process and no memory when idle; no KeepAlive | Verified by real launchd runs 2026-10-04 (cold start 0.37 s, ~30 MB) |
| H3 | `launch_activate_socket` via `ctypes` (libSystem) | Current public launchd API, no deprecation in the macOS 26 SDK header | Verified 2026-10-04 (SDK `launch.h`, real run) |
| H4 | Same LaunchAgent as the daily schedule (`com.dailyread.agent`): `StartInterval` + `RunAtLoad` + `Sockets` | One job to install; the helper serves from a thread while the daily check runs so a click never waits | Verified by tests + real run; trade-off: one job shares `ProcessType` |
| H5 | Python started directly (`.venv/bin/python3 -m dailyread.agent_main`), not `uv run` | launchd passes its socket only to the job's own process; `uv run` makes Python a child | Verified (found by a real run that failed) |
| H6 | `ThrottleInterval: 5` | launchd's default (10 s) stalls a click that arrives right after a quick restart; measured 9.1 s. The key caps the extra wait (it does not remove it). A helper that lives >= 3 min is rarely throttled; 5 s also limits a respawn loop if the job cannot start | Verified 2026-10-04 (measured, `man launchd.plist`) |
| H7 | Idle exit after 3 minutes, checked every 5 s; an accepted connection counts as activity | Does not linger; avoids a race that cut off a request at the idle boundary | Verified (regression test + real run); the 3 min figure is a user preference |
| H8 | Port 47821 on 127.0.0.1, configurable (`library: port:`) | Fixed port so reports can embed it | Verified 2026-10-04: IANA-unassigned, below the macOS ephemeral range; known niche users (PersonalJarvis, Akasha OS). `schedule install` checks the port is free and keeps the daily schedule if it is not |
| H9 | `ProcessType: Background`, `LowPriorityIO` | Lower CPU/IO priority so the daily run never slows the Mac | `man launchd.plist`; measured 2026-10-04: +~40 ms on a trivial job (0.124 s vs 0.081 s median); the real cold start (~0.3 s) was not re-measured per setting. Not an Apple-documented latency control. Two labels would allow separate settings |
| H10 | `schedule install` self-tests the helper and removes the socket and token if it fails; refuses while a report is being generated | A socket nothing answers would make launchd respawn the job; reinstalling kills a running report | Failure paths covered by tests; the full failure path was not run against a real launchd |
| H11 | `http.server` (stdlib, `ThreadingMixIn`, 10 s connection timeout) instead of a framework | No dependency, tiny memory; loopback-only; Python docs say "not for production" but this is a local single-user service | Docs checked 2026-10-04; acceptable by audit |
| H12 | Light entry point (`agent_main`) imports no HTTP client / extractor / pipeline on a click | Start-up 58 MB → ~30 MB | Verified (measured twice, including by the operations reviewer: 0.25-0.3 s, 29.8 MB); a test asserts the import set |

## Security

| # | Decision | Why | Status |
|---|---|---|---|
| S1 | Bearer token in `X-DailyRead-Token`, constant-time compare, stored `state/library.token` (0600) | A custom header carrying a secret is a valid CSRF defence (OWASP); the token is the real control | Verified 2026-10-04 (OWASP cheat sheet via audit) |
| S2 | `Origin` must be absent or `"null"`; `Host` must be `127.0.0.1:port` / `localhost:port` | Blocks other websites and DNS rebinding as a speed bump | Verified; note `null` is also sent by sandboxed iframes and `data:` pages, so the token is what protects |
| S3 | Token is embedded in rendered reports | Reports must reach the helper with no setup; reports are git-ignored | Accepted risk, documented: do not share reports |
| S4 | Body limit 5 MB, schema-cleaned records, atomic write (`mkstemp` + `os.replace`, mode 0600), damaged file moved aside | Never corrupt or silently overwrite the user's lists | Verified by tests |
| S5 | `Content-Security-Policy` meta: `default-src 'none'`, `script-src 'unsafe-inline' file:`, `connect-src` = helper origin only | Defence in depth for pages that render third-party text | CSP effect verified in Chrome (blocks `eval`); `unsafe-inline` is required because the report is one self-contained file |
| S6 | Saved text rendered with `textContent`; links only `http(s)`; JSON embedded in `<script>` with `<`, `>`, `&` escaped | Feed text is untrusted | Verified by tests |
| S7 | `Access-Control-Allow-Private-Network: true` still sent | Legacy: Chrome 142+ and Firefox 153+ use a permission model, not this preflight | Verified 2026-10-04: dead weight on current browsers, harmless; kept for older Chrome |

## Browser

| # | Decision | Why | Status |
|---|---|---|---|
| B1 | A `file://` page may `fetch` `http://127.0.0.1` without a prompt | Chromium treats `file:` as loopback and exempts loopback-to-loopback from Local Network Access. **The WICG spec says to treat file URLs as local, so this is empirical, not guaranteed** | Verified in stock Chrome 154 (headless and headed, fresh profile) and Firefox 153.0.4 on 2026-10-04. Safari untested (WebKit is implementing LNA). Re-check each Chrome/Firefox release with `tests/test_stock_chrome.py` |
| B2 | Browser `localStorage` is the instant copy; shared across `file://` pages in Chrome | Works when the helper is down | Chrome verified by UI test. **Firefox scopes it per folder (verified 153.0.4)**, so with the helper down a list saved on today's report is not visible on an archived one; the helper's file fixes that. Safari unverified |
| B3 | File System Access API link as a fallback only when no helper is installed | Optional, Chrome/Edge only; needs a picker click | Support matrix verified 2026-10-04; persistence on `file://` unverified |
| B4 | Export / Import JSON as the universal fallback | Works in every browser | Verified by tests |
| B5 | Fall back to browser storage + reminder bar if the helper does not answer (8 s timeout) | Never lose a click | Verified by tests |
| B6 | Reports navigate via `reports/catalog.js` loaded by `<script src>` | `fetch` of local JSON is blocked on `file://`; script tags work | Verified in Chrome (UI tests) |
| B7 | A report that moves into `archive/<year>/` is re-rendered (relative path to `catalog.js` changes) | Relative links survive moving the project folder (absolute `file://` links do not) | Verified by tests |

## Web platform, accessibility, docs, tooling

| # | Decision | Why | Status |
|---|---|---|---|
| W1 | Tabs: `role=tablist/tab/tabpanel`, roving tabindex, arrows + Home/End, tablist is a `div` directly above its panels | ARIA APG tabs pattern | Reviewed against the APG 2026-10-04; deviations fixed (Home/End, landmark, order) |
| W2 | Reports menu is a native Popover (`popover="auto"`): top layer, Escape, light dismiss, focus return | Popover is Baseline "newly" (2025-01-27); `<dialog>` is widely available | Adopted 2026-10-04 after review; Chrome 154 verified by UI tests |
| W3 | CSS: `light-dark()` single token block, `color-mix()`, `:focus-visible`, `:has()`, `::details-content` (print only), `minmax(0,1fr)`, forced-colors block | All Baseline (a few only "newly") | Verified 2026-10-04 (webstatus data) |
| W4 | Icons are Feather (MIT) inline SVG paths | No icon font or network | Licence notice added to README |
| W5 | No JS framework or build step; three plain JS files inlined | Self-contained report, no CDN | Design rule |
| W6 | `claude -p --effort <level>` per stage; `thinking` setting kept only for Haiku | Sonnet 5.5 cannot turn thinking off; effort is the knob | Verified 2026-10-04 (Claude Code docs + local `claude --help`, `--effort low` run) |
| W7 | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` for headless calls | An unattended job needs no telemetry/updates | Verified a call still works with it (2026-10-04) |
| W8 | Python venv from python.org 3.13.5 | Existing setup | Audit: 3.13.16 / 3.14.8 are newer (patch security fixes). **Not changed: decision for the owner** (needs `brew upgrade uv`, `.python-version`, a rebuilt `.venv`, then `schedule install` again) |
| W9 | Dependency set (httpx, feedparser, trafilatura, jinja2, lxml, bs4, pyyaml) | Small, maintained | 0 advisories in OSV 2026-10-04; trafilatura 2.3.0, htmldate 1.11.0, markupsafe 3.0.4 applied and checked with a real dry run |
| W10 | Tests: pytest + Node `--test` + Playwright driving installed Chrome; UI tests wait with polling (CSP forbids `eval`) | No browser download; real Chrome | Verified; Playwright disables Chrome rollouts, so a separate stock-Chrome test exists |
| W11 | GitHub CI (uv + pytest + Node), Dependabot (uv, github-actions, 7-day cooldown), SECURITY.md | Basic OSS hygiene | Actions pinned to full SHAs: checkout v7.0.1, setup-node v7.0.0, setup-uv v10.2.0 (verified against the GitHub releases API 2026-10-04); not yet run on GitHub |
| W12 | README uses Mermaid and a GitHub alert; CLAUDE.md under 200 lines | Current GitHub / Claude Code conventions | Verified via audit 2026-10-04 (GitHub docs, Claude Code memory docs) |

## Added after the reviews (2026-10-04)

| # | Decision | Why | Status |
|---|---|---|---|
| R1 | Helper reads the body exactly (`Content-Length` must be 0..5 MB, short reads rejected), caps open connections at 64, uses a 5 s per-connection timeout, requires exactly one token header | Found by the security review: a negative length streamed gigabytes into memory | Fixed; regression tests |
| R2 | Token created with `O_NOFOLLOW`, mode 0600, `state/` 0700; reports and catalog written owner-only (`fsutil.write_private`) | Other local accounts share loopback and could read a world-readable report | Fixed; tests |
| R3 | Timestamps validated and clamped (<= now + 5 min), text truncated by code points, lone surrogates dropped, whole-number stars accepted | A bad clock or a cut emoji could pin a record or break auto-save for a browser | Fixed in Python and JS; tests on both |
| R4 | The agent drains waiting connections when it cannot serve (no token, config error) | A queued connection could make launchd restart the job in a loop | Fixed; tests; verified with a real launchd run |
| R5 | `schedule status` reports a stale plist (project moved, Python missing, no helper) and launchd's run count / last exit | A moved project folder silently stops the daily report | Fixed; tests |
| R6 | `StartCalendarInterval` checks at report time + 5 min and 12:00 | `StartInterval` ticks that fall in sleep are skipped (man page) | Added; unit test only (real sleep/wake not tested) |
| R7 | Seeds baked into pages hold only saved items; post-render steps (catalog, re-render, backup) can never fail a finished report | Page size, and a corrupt old data file must not fail a run | Fixed; tests |
| R8 | Open alternative, not built: serve reports from the helper over `http://127.0.0.1` (same origin) | Removes the dependence on browsers exempting `file://` and Firefox's per-folder storage; costs plain-file reports | Decision for the owner if a browser ever blocks the current design |

## "Add a link" (2026-10-04)

| # | Decision | Why | Status |
|---|---|---|---|
| A1 | Reading and summarizing run in the helper (`POST /add`), not in the page | A static page cannot fetch other sites (CORS), run Chrome or call Claude; the helper already has the token, the file and launchd on-demand start | Verified end to end (real network, Chrome and Claude): 5-8 s for the Medium example |
| A2 | Plain fetch first, then the installed Chrome headless (`--dump-dom`, throwaway profile, killed as soon as `</html>` is printed) | Medium returns a Cloudflare 403 to scripts but serves a real browser; verified 2026-10-04 (plain: 403 block page; Chrome: full article). No third-party proxy or archive, so the link and text go nowhere new | Verified; `--virtual-time-budget` hangs and is not used |
| A3 | Every connection of the add (plain fetch AND headless Chrome) goes through `safe_proxy.FilteringProxy`: public sites only, ports 80/443, the validated IP is pinned | The independent review showed Chrome follows redirects, iframes and images to private addresses unchecked, and DNS can change between check and connect | Verified with the real Chrome against a hostile local site (redirect, meta refresh, JS, img/iframe/script/fetch, direct loopback): the internal server saw 0 requests; a control without the filter reaches it |
| A4 | Unreadable pages are refused with a plain reason and nothing is saved (block page, login wall, member-only preview, too little text) | Never summarize a preview or an error page as if it were the article | Unit-tested with synthetic pages and the real Medium page |
| A5 | The record is dated the day it was ADDED (local date), `manual: true`, with an "ADDED" tag | The list sorts newest first by date; the post date would bury it | Tested (timezone edge: local date vs UTC) |
| A6 | One Claude call (`add_link` stage, Sonnet, effort medium, the daily review's star rules and profile) derives title, source, summary, stars | Same judgement as the daily digest; the source (publication) is only on the page, so a model derives it | Verified on the real example; schema limits keep the summary under 300 characters |
| A7 | One link at a time (semaphore); the helper's idle timer counts from the end of the request | Each add starts Chrome and Claude; a flood must not exhaust the Mac | Tested |
| A8 | `Accept-Encoding: identity`, compressed answers refused (left to Chrome), reads capped (2 MB), parse capped (1 MB and 25,000 tags), overall plain-fetch deadline (40 s) | Review: a 0.3 MB gzip inflated to 33 MB, parsing 4 MB of tiny elements cost 9.6 s and 576 MB, a trickling server held the helper open | Worst case now 0.5 s CPU and +44 MB; tests replay each attack |
| A9 | Chrome: `nice`, downloads redirected into the throwaway profile, minimal environment, no QUIC/WebRTC leaks, stale-profile sweep, process group killed repeatedly | Review: drive-by file written to ~/Downloads; Chrome inherited the helper's environment | Real-Chrome test: nothing lands in ~/Downloads |
| A10 | httpx ignores proxy environment variables (`trust_env=False`) and uses only our filter | A corporate proxy variable would make the local check meaningless | Tested |
| A11 | Control and bidi-override characters removed from saved text (Python and JS) | A title could be disguised with a right-to-left override | Tested on both sides |
| A12 | Known limits, accepted: a hostile page can bias Claude's rating, title or source name; links keep their query string | Output is schema-bounded, shown as text, Claude has no tools; stripping unknown query parameters would break links | Documented in the README and SECURITY.md |
| A13 | The add flow is a button in the Read Later header that opens a native `<dialog>` (modal): one big "Paste link & add" button reads the clipboard and starts at once; ⌘V anywhere in the dialog does the same; a small text box is the fallback; progress bar while it runs (the dialog cannot be dismissed meanwhile); a result preview, then Done / Add another | An always-open text box looked cluttered; the modal gives focus trapping, Escape and an inert background for free (`<dialog>` is widely available) | Tested in real Chrome: clipboard button (stubbed), denied clipboard fallback, paste event, busy lock, preview, highlight on close, phone layout |
