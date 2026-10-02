# Task: summarize and pre-screen items for a personal morning digest

You receive items from one section of the reader's daily digest (e.g. a release-notes feed or a blog).
For **every** item, return:

1. `summary` — following the Summary rules below exactly (≤ 300 characters).
2. `title` — only if `needs_title` is true, write one following the Output rules; otherwise return "".
3. `relevant` — only meaningful when the input says `filter: "ai_cloud"`: true if the item is about AI/ML,
   agents, LLMs, cloud platforms or data platforms; false otherwise. In all other cases return true.
4. `prescreen` — a first-pass verdict that a stronger reviewer will check later:
   - `"candidate"` if the item *might* deserve "Read fully" under the Verdict rules.
   - `"skip"` if it clearly does not.
   Be **lenient**: when unsure, choose `"candidate"`. The reviewer can only rescue what you pass on.
5. `skip_reason` — when prescreen is "skip": 2–6 words per the Verdict rules (e.g. "minor fix"). Else "".
6. `depth` — what kind of piece it is, judged from the full content you see:
   announcement | release_note | overview | how_to | deep_technical | research | opinion | customer_story | news.
7. `full_text_adds` — ≤ 100 characters: what the full piece offers beyond your summary
   (e.g. "architecture diagrams and benchmark method", "migration steps with code"); "" if nothing substantial.
   This helps a later reviewer decide whether reading the full piece is worth it.

Work item by item. Base every statement only on that item's `content`.
