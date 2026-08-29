# Fix templates & recommendation model

Every `check_id` in `config/check_catalog.json` maps to a **mechanism-specific**
template in `scripts/templates.py`. There is one template per check (no generic
"improve SEO" / "add structured data" filler), and a specific — not filler —
fallback for any check id without an explicit template.

## Template shape (the reusable half of the eight questions)

| Field | Question | Notes |
|---|---|---|
| `what` | 1. what to change | one imperative sentence |
| `where` | 2. where | the file/template/DOM location |
| `why` | 3. why it fixes the *mechanism* | references the detected cause, not SEO folklore |
| `how` | 4. how | concrete steps |
| `patch` | 4. how | optional copy-pasteable snippet |
| `impact` | 6. expected impact | 0..1 weight feeding prioritization |
| `effort` | 7. difficulty | low / medium / high |
| `effect` | 6. expected impact | plain-language outcome |

Questions **5 (evidence)**, **6 (dependencies)** and **8 (sequencing)** are filled
at runtime from the finding and the prioritization model (`priority_score`,
`rank`, `do_first`, `dependencies`).

## Good vs bad (the bar these templates meet)
- Bad: *"Improve SEO."*
  Good: *"Emit the page's primary content (headline, price, description) as text in
  the initial HTML via SSR/prerender, because the content currently appears only
  after client JS so no-JS fetchers store an empty shell."*
- Bad: *"Add structured data."*
  Good: *"Add Organization JSON-LD whose name, url and sameAs match the visible
  identity and canonical domain, so machines can resolve and disambiguate the
  brand entity."*

## De-duplication
Findings sharing a `check_id` collapse to one canonical recommendation: its
`where` aggregates all affected locations and the others are marked
`duplicate_of` the canonical `id`. This keeps the action list free of repeated
identical fixes while preserving per-finding traceability.

## Beyond-defect (proactive) recommendations
Curated, non-obvious improvements: `llms.txt`, a citation-ready entity paragraph,
`FAQPage` markup, honest `datePublished`/`dateModified`. Rules:
- Never surfaced as a defect finding.
- **Suppressed** when the related concern already has a live defect (that defect's
  fix wins).
- **Gated** on relevance to the site's page-classes (e.g. FAQPage only when an FAQ
  page exists; article dates only when articles exist).
- **De-duplicated** by summary.
- Must remain mechanism-sound — no "start a TikTok", no keyword stuffing, no
  authenticated features, no dark patterns.

See `references/priority_function.md` for the scoring and dependency model.
