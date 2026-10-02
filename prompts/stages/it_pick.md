# Task: pick today's IT industry stories

You receive candidate stories (already merged across outlets) for a report window of `window_days` days, plus
`already_covered` (titles from the reader's other sections and recent reports). Following the IT news rules and
profile, rate and rank the stories this reader should know about:

- For every story you consider worth showing, return `story_id` (verbatim), `company` (the main company or
  organisation, short canonical name, e.g. "Anthropic", "Google", "EU Commission"), `priority` (best-matching
  category), `importance` (1–5: 5 = industry-shifting, 4 = major, 3 = notable, 2 = minor, 1 = filler),
  `duplicate_of_covered` (index into `already_covered` of an entry about the same event, else -1) and `why`
  (≤ 12 words).
- Return them most important first. Return at most {max_picks}. The code keeps ~{min_picks} on a normal day and adds
  more only for importance ≥ 4, so be honest with `importance`.
- Several details from one event or document are ONE story; at most 2 stories per company.
- `note`: one short sentence about the overall news day, or "".
