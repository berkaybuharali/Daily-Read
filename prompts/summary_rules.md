# Summary rules

Goal: a reader who only reads the summary learns what the item is and what it changes, accurately.

- **Length: at most 40 words and 2 sentences** (the hard limit is 300 characters; 40 words stays under it).
  Prefer the 2–4 most important specifics over listing everything.
- **Lead with the substance**: what was released / changed / argued / found. No preamble.
- **Educational**: name the concept or mechanism so the reader learns something
  (e.g. "…uses speculative decoding to…", "…moves from per-seat to usage-based pricing…").
- **Neutral**: no opinion, no recommendation, no "worth reading", no "important", no hype words
  (revolutionary, game-changing, exciting, powerful, seamless, cutting-edge, unlock, supercharge).
- **Faithful**: use only facts present in the provided content. Never add context from memory, never
  generalize beyond the source, never guess numbers. If the content is thin, write a shorter summary.
- **Keep specifics exactly**: product names, versions, model names, numbers, dates, regions,
  launch stage (GA / Preview / Public Preview / Deprecated).
- **No filler openers**: never start with "This article", "The author", "In this post", "This release",
  "This update", "Google announces", "Anthropic announces". Start with the subject itself.
- **Truncated content** (`truncated: true`, text ends with "[…truncated: N words in full]"): you only see the beginning. Never state
  totals or counts for the whole piece ("50+ fixes", "12 features"); describe what you can see.
- **Title only** (`source_type: title_only`): write one cautious sentence about what the title says the piece covers.
- **Curator-note fallback** (`source_type: curator_note`): the article text was unavailable; you only have the
  title and the curator's comment. Summarize what the title + note say the piece is about; drop the curator's
  opinion ("worth reading", "I liked", "I disagree"); do not invent details.
- **Release notes**: describe the change itself; keep the launch stage; mention the affected
  feature/API. For a Claude Code release with many changes, cover the 2–3 most significant changes
  (new capabilities and behavior changes before bug fixes).
- Identifiers (API names, CLI flags, model IDs, SQL functions) go in backticks: `AI.KEY_DRIVERS`.
- English, plain text otherwise. No markdown bold/italics, no links, no emojis.

## Examples
Good: "`AI.KEY_DRIVERS` is now GA in BigQuery: it ranks data segments by how much they explain a change in a metric, letting you run key-driver analysis in SQL without exporting data."
Bad: "This exciting update brings a powerful new function to BigQuery that you should definitely check out!"  (filler opener, hype, opinion, no substance)
Bad: "BigQuery adds AI functions, continuing Google's push to dominate data analytics."  (vague, adds unsourced claim)
