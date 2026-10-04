# Task: summarize and rate ONE web page the reader added by hand

The reader found this page somewhere (a share link, a newsletter, a chat) and wants it in their Read Later list.
It is not one of their followed sources. You receive one JSON document with the page's `url`, `domain`,
`page_title`, `site_name`, `publisher`, `author`, `description`, `published` (may be empty) and the article `text`
(possibly truncated). Return:

1. `readable` — true when `text` is a real article or post the reader could read. False when it is a login or
   paywall prompt, a cookie or bot-check page, an error page, a bare video/podcast page, or otherwise has no article.
   When false, still fill the other fields with short placeholders; they are ignored.
2. `title` — the article's own title, cleaned: no site name, no "| by Author | Publication | Month, Year | Medium"
   suffixes, no emoji prefixes. At most 140 characters.
3. `source` — the publication or site a reader would recognise, at most 40 characters. Prefer the publication over the
   hosting platform: for a Medium story in the "Google Cloud - Community" publication write
   "Google Cloud Community (Medium)"; for a personal blog write the blog or author name ("Simon Willison");
   for a Substack write the newsletter name ("Latent Space (Substack)"). Use only names present in the data;
   if none is present, use the bare domain without "www.".
4. `summary` — following the Summary rules below exactly: at most 2 sentences and **under 280 characters in total**
   (count them; shorter is better). End on a complete sentence, never on a cut-off phrase.
5. `stars`, `verdict`, `reason` — judged by the Verdict rules and the reader's profile below, exactly as the daily
   review would judge an item from a followed source: `verdict` is "read" only for 4–5★, `reason` is 6–14 words.
   The `published` date, if any, is context only; do not reward or penalise recency by itself.

Base every statement only on the provided data.
