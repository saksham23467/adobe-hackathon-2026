# Engagement & orientation check catalog

The visitor journey is modelled as five stages; each check is tagged with the
stage at which it breaks (`metric.stage`) and a tier (`metric.tier`) that maps to
a confidence value.

```
arrival → orientation → understanding → evaluation → next action
```

## Checks

| check_id | Stage | Tier | Predicate | Evidence | FP guard |
|---|---|---|---|---|---|
| `engagement.blocking_interstitial` | arrival | confirmed | A full-content-blocking overlay/modal precedes content (`aria-modal` dialog, `<dialog open>`, or fixed-position `.modal/.overlay/.popup`) | overlay selector + first text | cookie/consent/privacy banners are excluded |
| `engagement.no_orientation` | orientation | likely | Home has **no** visible `<h1>` **and no** primary CTA | title + presence flags | a clear `<h1>` *or* any CTA passes |
| `engagement.no_ia_navigation` | orientation | likely | Home has no nav, no breadcrumb, and no site search | presence flags | one signal is enough to pass |
| `engagement.no_viewport_meta` | orientation | likely | ≥ half of sampled pages lack `<meta viewport>`, or a page disables pinch-zoom (`user-scalable=no` / `maximum-scale=1`) | page URLs + counts | present, zoomable viewport passes |
| `engagement.content_hidden_interaction` | understanding | likely | Home's only `<h1>` sits inside hidden / interaction-gated DOM (no visible heading) | the hidden heading text | a visible heading passes |
| `engagement.inconsistent_naming` | understanding | confirmed | The same internal destination is linked under ≥ 2 distinct **primary-nav** labels across pages | destination + label set | breadcrumb / CTA / body relabelling is ignored |
| `engagement.unexplained_jargon` | understanding | recommendation | A primary-nav label is a bare acronym never expanded on-site | acronym + page | common acronyms (API, FAQ, CRM…) are allowlisted |
| `engagement.unclear_navigation` | next action | confirmed / likely | Links with empty text & no aria-label (confirmed) or non-descriptive text like "click here"/"read more" (likely) | offending hrefs / labels | icon links with aria-label pass; mailto/tel skipped |
| `engagement.dead_end_page` | next action | likely | A non-home page has no nav, no breadcrumb, no onward content links, and no CTA | dead-end page URLs | any nav or onward link or CTA clears it |
| `engagement.key_info_undiscoverable` | evaluation | likely | A product / pricing / contact page **exists** but is not reachable from the home page's links | page class + URLs | absence of the page class is never flagged |

## The journey stages, and what "breaking" means

1. **Arrival** — the first paint must be usable. A blocking interstitial breaks
   the journey before it starts.
2. **Orientation** — from above-the-fold content a visitor should answer *what is
   this?*, *who is it for?*, *what next?* Missing heading + CTA, missing IA, or a
   non-adaptive mobile layout break orientation.
3. **Understanding** — the message must be present and legible: not hidden behind
   interaction, not mislabelled, not opaque jargon.
4. **Evaluation** — the visitor needs to reach the information that drives a
   decision (products, pricing, contact). Unreachable key info breaks evaluation.
5. **Next action** — every page should offer a clear way forward. Ambiguous links
   and dead-end pages break the final step.

## Evidence model (why findings are trustworthy)

Every Observation carries:
- `urls` — the exact page(s) exhibiting the issue.
- `evidence_sentence` — a human-readable statement of the observed fact.
- `metric` — machine-readable specifics (`stage`, `tier`, counts, examples,
  offending destinations/labels) so the finding is re-runnable and auditable.

No detector flags subjective visual preferences, colour, spacing, or copy tone.
Each fires only on a structural DOM fact that can be re-derived deterministically.
