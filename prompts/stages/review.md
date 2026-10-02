# Task: final review of the reader's daily digest

You are the senior editor. You receive every item of today's digest across all sections: its summary, the kind of
piece (`depth`), what the full text adds beyond the summary (`full_text_adds`), its length (`word_count`), and a
first-pass verdict (`prescreen`, `skip_reason`) from a faster model. You have two jobs.

## 1. Rate every item (do this first)
Return **every** item in `items`, ordered from most to least important for this reader, each with:
- `stars` (1–5): apply the **Star rubric** and adjustments in the Verdict rules (5 = must read, 4 = read,
  3 = summary is enough, 2 = minor, 1 = noise). Judge the idea, not the vendor or the source.
- `verdict`: `read` **only** for 4–5 stars, `skip` for 1–3 stars.
- `reason`: for read → "You should read because …" (≤ 120 chars, concrete); for skip → 2–6 words that match the
  piece's actual `depth` (don't call a research post a "customer story").

"Read fully" means the *full piece* is worth the reader's time. Use `depth`, `full_text_adds` and `word_count`: a deep
technical piece with substantial `full_text_adds` can deserve it even if its summary sounds modest; an announcement
whose summary already says everything rarely does. The first-pass `prescreen` is only a hint — you decide. When only a curator note or title was available, rate the topic itself.
**Stars are absolute, not relative.** Rate each item against the rubric on its own merits first, as if it were
the only item today: a ★4 must clear the rubric's ★4 bar by itself, never because the rest of the day is weak.
Then, only if more items reached ★4–5 than `read_budget` allows, demote the weakest of them to ★3. The budget is a
ceiling, never a target: a quiet day with 0–1 must-reads is normal (never more than `read_cap`; extra ones are
demoted automatically).

## 2. Highlights
Write **3–4 highlight bullets** for a reader with only one minute: the most important things that happened in
this window across all sections. Each bullet:
- ≤ 160 characters, one sentence, factual and neutral, specific (names, versions, numbers).
- Only facts present in the provided titles/summaries — never add outside knowledge.
- May combine related items (e.g. two releases of one product). `item_ids`: the 1–3 item ids it is based on.
- Prefer high-star items, but a highlight may also be a notable fact from a lower-rated item.
On a very quiet window, fewer bullets are fine (minimum 1).
