# Task: first-pass filter of IT industry headlines

You receive headlines (`id` like "h12", title, outlet, short description, `on_techmeme`) for the report window, plus
`already_covered` (titles from the reader's other sections and from recent reports).

1. Drop everything excluded by the IT news rules (promo, consumer, already covered, trivial).
2. Merge headlines about the same real-world event into one story. `primary_id` = the most informative headline,
   preferring primary reporting over aggregator rewrites. `other_ids` = the other headlines of the same story
   (empty list when the story has one headline).
3. Return at most **{max_candidates}** candidate stories, most important first, with a short factual `title`
   (≤ 100 chars, neutral, based on the headlines) and `why` (≤ 12 words).

Be inclusive at this stage: a stronger model makes the final pick from your candidates. Never drop a story that
touches a priority topic or qualifies as "big".
