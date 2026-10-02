# IT industry news rules

Input: headlines (+ short descriptions) from Techmeme, SiliconANGLE and TechCrunch (AI + Fundraising) for the
report window. Output: the stories this reader should know about today.

## Story, not article
- One story = one real-world event. Many outlets covering it are one story; merge them.
- Several details from the **same underlying event or document** are one story (e.g. revenue, loans and
  valuation all disclosed in one IPO filing → one story about the filing).
- **At most 2 stories per company** in the final list, unless a third is a separate major event.
- Coverage by several outlets and presence on Techmeme (`on_techmeme: true`) are signals of importance.

## Exclude
- Stories already covered by the reader's other sections (list given as `already_covered`), unless the IT
  coverage adds a clearly different angle (e.g. market reaction to a launch is still the same story → exclude).
  Match on the underlying event, not exact wording: "Product X 2.0: our next era of…" in `already_covered` means
  every headline about the Product X 2.0 launch is excluded — but a genuinely new event about Product X
  (e.g. a later price change) is not.
- Promotional content: event tickets, sponsored posts, webinars, "theCUBE" event previews, listicles.
- Consumer gadgets, gaming, crypto prices, personal-finance, celebrity/personality drama, rumors without sourcing.
- Small funding rounds (< ~$50M) unless in a priority topic or strategically notable (e.g. AI agents infra in Europe).

## Priority (from profile)
1. Google Cloud / GCP · 2. Anthropic / Claude · 3. Netherlands · 4. Europe (incl. EU regulation) · 5. Türkiye
A priority-topic story beats a non-priority story of similar weight.

## "Big" — qualifies regardless of topic
- Acquisitions ≥ ~$1B, or strategically important for the cloud/AI/data market.
- Funding ≥ ~$500M, or any notable round in AI infrastructure / agents / data platforms.
- Frontier model releases; major platform/strategy shifts by hyperscalers or AI labs.
- Major outages, major security breaches, critical vulnerabilities with broad exposure.
- Significant regulation, antitrust action, export controls, sovereignty decisions.
- Large layoffs/reorgs or leadership changes at major tech companies.

## Sizing
- Normal day: **5** stories. Up to **10** only when there are genuinely more important stories.
- Fewer than 5 is allowed on a quiet day — never pad with weak stories.
- Whole report window (multi-day windows are still one list, scale toward the upper end only if warranted).
