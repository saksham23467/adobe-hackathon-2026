# Fact-accessibility check catalog (content extractability)

All checks are tiered. `confirmed` → FAIL/high, `likely` → FAIL/medium,
`informational` → PASS (recorded, never a finding). Every finding carries a
mechanism sentence, an evidence snippet, and a numeric `confidence_score`.

| check_id | Tier | Predicate | Fail evidence | FP guard |
|---|---|---|---|---|
| `fact.ambiguous_value` | likely | A category (pricing/hours/location) has **no concrete value** in text or schema **and** an explicit vague phrase is present | the vague phrase + snippet, per URL | requires the vague phrase to be present, so it is always quotable; skipped if any concrete value exists |
| `fact.facts_locked_in_non_text` | confirmed | A category-labelled section (heading matches pricing/hours/contact/specs/menu/location) contains a meaningful image but `<8` words of body text and no concrete value | heading + image `src` | needs a category heading + real image; sections with textual values pass |
| `fact.contradictory_values` | confirmed | schema.org Offer price disagrees with the price shown in visible text on the same page | `schema=[…] vs displayed=[…]` | only fires when both exist and are disjoint; ≤2 text prices to avoid sale/strike-through noise |
| `fact.not_attributed_to_entity` | likely | A concrete price is shown on a product page (or with "add to cart") but there is **no** Product/Offer schema and no machine-readable product entity | price + URL | requires product intent + a real price; pages with Product/Offer schema pass |
| `fact.behind_interaction` | likely | A fact category (FAQ/specs/policies/features/pricing/hours/contact) exists only inside hidden DOM (`aria-hidden`/`display:none`/`hidden`) with ≥15 words | heading + hidden snippet | needs a category heading + substantial hidden text; visible sections pass |
| `fact.no_quotable_value_prop` | likely | Homepage has no meta description, no Organization description, and no plain "we build/provide/offer…" sentence | title/headline + snippet | homepage only; needs ≥15 visible words; any descriptive sentence passes |
| `fact.informational_images_no_alt` | likely | ≥3 meaningful images on a page and ≥60% lack alt text | per-page image + missing counts | icons/logos/spacers and tiny images excluded first |

## Fact categories evaluated
identity / what the company does, products & services, pricing, location/address,
contact, opening hours, FAQs, policies & guarantees, specifications, features,
product attributes, key claims. Categories are recognised from section headings
(`CATEGORY_KEYWORDS`) and from schema.org types/properties.

## The four representations
Each page is examined as: (A) selectable text of the whole DOM, (C) *visible*
text (hidden/interaction-gated content removed), the semantic layer (headings +
JSON-LD properties), and non-text carriers (images/canvas). A fact is
"machine-extractable" only when it is present as concrete, visible, attributed
text in A/C — not merely visible to a human.

## Informational vs decorative images
Images whose `src` hints at icon/logo/sprite/avatar/spacer, or whose declared
width/height is `<64px`, are treated as decorative and excluded before the
alt-text check.

## Confidence
`confidence_score` (0–1) is stored on every observation and the `Confidence`
enum caps severity in the compiler: `high` → up to critical/high, `medium` → up
to high, `low` → up to medium.
