# Verdict budget and reason format

## Budget
- The payload gives `read_budget` (normally ~3 per day of window) and `read_cap` (hard maximum, enforced in code).
  Stay within `read_budget`; go up to `read_cap` only on genuinely exceptional days.
- Zero ✅ is a perfectly good day. Never fill the budget for its own sake: the budget only limits how many items
  that already earned ★4–5 on their own may keep it. Typical: 1–3 on a normal day, 0–1 on a quiet one.
- When two items cover the same news, ✅ at most one (the more informative source).

## Reason format
- ✅: `You should read because …` — one line, ≤120 characters, concrete: name the specific thing to learn or act on.
  Good: "You should read because the Sonnet 4.5 retirement on 30 Nov needs a migration plan."
  Bad: "You should read because it is interesting and relevant."
- Skip: 2–6 words, from the typical Skip reasons or similar. Example: "minor fix", "customer story".
