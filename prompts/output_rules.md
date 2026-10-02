# Output rules

- Return only the JSON object required by the schema. Every input `id` you are asked about must appear
  exactly once in the output, copied verbatim.
- Generated titles (only when `needs_title` is true): factual, ≤ 80 characters, sentence case, no trailing
  period, no launch stage prefix (the stage belongs in the summary). Example: "Security center for BigQuery in the console".
- Identifiers in backticks; no HTML, no markdown other than backticks, no emojis.
