# Security

Daily Read is a local tool: its helper listens only on `127.0.0.1`, everything is stored on your machine, and nothing
is sent anywhere except the feeds it fetches and the headless `claude -p` calls you already pay for.

## Reporting a vulnerability

Please use GitHub's private reporting: **Security tab → "Report a vulnerability"** (private vulnerability reporting
must be enabled by the repository owner under Settings → Code security). Please do not open a public issue for a
vulnerability. Expect an answer within about a week; this is a one-person project.

## Supported versions

Only the latest commit on `main` is supported.

## Scope

- The Read Later helper (`dailyread/library_helper.py`): token, Origin and Host checks, input validation, resource limits.
- Report rendering: escaping and the Content-Security-Policy.
- The locked-down `claude -p` runner (`dailyread/llm.py`).
- The LaunchAgent setup (`dailyread/schedule.py`, `dailyread/agent_main.py`).
- "Add a link" (`dailyread/add_link.py`, `dailyread/safe_proxy.py`): it fetches pages you paste, so it must never reach your own
  network. All its traffic, including the headless Chrome's, goes through a filtering proxy that only allows public websites.

## Things to know

- The helper token (`state/library.token`) is embedded in every report you render, so **do not publish or share your
  reports**. `state/`, `reports/`, `data/` and `logs/` are git-ignored on purpose.
- On a Mac with several user accounts, loopback is shared between accounts, so the token is the only barrier; the token
  file and `state/` are owner-only (0600 / 0700). Reports are written owner-only too.
- Pasted links keep their query string and are stored in `state/library.json` and baked into reports: do not share those.
- The security model is described in the README under "Read Later and Favorites".
