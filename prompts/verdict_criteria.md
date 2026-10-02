# "Read fully?" verdict rules

The verdict answers one question: **should this reader spend time reading the full piece?**
Default answer is **Skip**. The summary already gives the gist; ✅ is only for items where the full text
adds substantial value *for this reader* (see profile).

## The core test
Ask: **does this change what the reader builds, recommends, or how they think about agents and data?**
Judge the *idea*, not the vendor. A Google or Anthropic launch gets no credit just for being in a followed
product; give Google, Anthropic, OpenAI, Microsoft, AWS and others the same bar.

## Star rubric
- **5★: must read.** One of:
  - a new or changed cross-vendor standard or protocol (spec release, major version, move to neutral governance);
  - original research or data on how agents and AI are built or used (adoption stats, eval results, security audits);
  - a first-of-its-kind pattern or architecture backed by evidence;
  - a breaking change, retirement or security issue that directly affects a followed product.
- **4★: read.** One of:
  - a deep practitioner write-up with trade-offs (how a team built or ran X, a post-mortem);
  - a clear explainer of an emerging standard (A2UI, AG-UI, MCP Apps, AP2/ACP/UCP, Agent Skills);
  - a launch that creates a new *class* of capability or changes the architecture or economics of agents and data;
  - a substantial research or engineering post from a frontier lab.
- **3★: good to know, the summary is enough.** Incremental launches in followed products (GA after preview, new
  connector, region, model version bump), solid tutorials on known patterns, industry news that doesn't change
  practice, opinion with some evidence.
- **2★: minor.** Routine release notes, narrow previews, getting-started posts, event recaps, opinion without
  evidence, rehashes of something already covered.
- **1★: noise for this reader.** Marketing and customer stories, listicles, SKU or pricing tweaks with no wider
  impact, docs fixes, anything outside the profile.

## Adjustments (after choosing the base tier)
- **Up one star:** primary source (the spec, the paper, the dataset, the team that built it); relevant across
  vendors; lessons that transfer to other stacks; security implications for agents.
- **Down one star:** a single vendor's feature announcement with no new idea; preview-only or limited regions;
  marketing tells ("unlock", "seamless", customer-logo quotes, no limitations mentioned, sign-up call to action);
  duplicates a better item in the same digest.
- **At most one pure product launch per day** may be a must-read, and only if it clears the core test.
- **Only the title or curator's note available** (article not reachable): rate the *topic*. If the topic is
  high-value (e.g. a new protocol), it can still be 4★; the reader opens the link. Don't mark it down just
  because the text is missing.

## Calibration examples
- "State of agent skills": registry adoption data for a cross-vendor standard → 5★.
- Using A2UI to give an agent a generated UI → 4★ (emerging agent ↔ UI protocol), even from a curator note.
- An agentic commerce / agent payments protocol update or explainer → 4–5★.
- "Gemini Enterprise adds connector Y" / "BigQuery feature X in preview" → 2★.
- A new frontier model from a followed lab → 3★ by default; 4★ only if it changes capability, pricing or
  architecture choices in a way the summary can't convey.
- A vendor's remote MCP server or agent toolkit GA → 3★, unless it introduces a new pattern (then 4★).

## Skip: typical reasons
customer story · marketing recap · event/webinar promo · minor fix · small feature · incremental launch ·
region/availability expansion · opinion without new information · summary is sufficient · niche product ·
not in reader's interests · duplicate of another ✅ item
