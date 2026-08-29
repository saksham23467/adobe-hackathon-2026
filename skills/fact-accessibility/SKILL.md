---
name: fact-accessibility
description: >-
  Determine whether the important facts about a brand are easy for a machine or
  AI assistant to identify, extract, attribute, and quote. Runs a deterministic +
  heuristic pipeline over the acquire-once cache: it parses each page into headed
  sections (tracking hidden/interaction-gated content), mines schema.org JSON-LD
  properties, and evaluates fact categories (identity, products, pricing, hours,
  location, contact, specs, policies, FAQs, claims). Flags facts stated only with
  a vague qualifier, facts locked in images, structured/displayed contradictions,
  facts not attributable to a named entity, facts hidden behind interactions, and
  homepages with no quotable value proposition. Every finding names the mechanism
  and carries an evidence snippet plus a confidence score. Not generic SEO
  scoring — a page whose facts are concrete, textual, attributed, and visible
  produces no findings.
license: MIT
metadata:
  concern: fact_access
  mechanism: "C,D,F"
allowed-tools: Bash(python3:*) Read
---

# Fact Accessibility (content extractability)

The content renders fine for a human — but can a machine pull out a *specific,
correct, quotable* fact and attribute it to the brand? This skill answers that
per fact category, never with generic "content could be improved" claims.

## When to use
After acquisition. Reads the shared cache; never re-fetches.

## Engine
- `scripts/factextract.py` — parses HTML into a `Doc`: headed `Section`s (with a
  `hidden` flag derived from `aria-hidden` / `display:none` / `hidden`), global
  images, fully-parsed JSON-LD nodes (`@graph` flattened), and concrete/vague
  matchers for price, hours, address, email, phone, and identity sentences.
- `scripts/factdetect.py` — the tiered detectors, one per extractability failure.
- `scripts/detect.py` — entrypoint; runs the detectors over `ctx.cache`.

## The ten questions, mapped to detectors
1. Explicitly stated? → `fact.no_quotable_value_prop` (homepage identity)
2. Readable text? → `fact.facts_locked_in_non_text`, `fact.informational_images_no_alt`
3. Correct entity? → `fact.not_attributed_to_entity`
4. Unambiguous wording? → `fact.ambiguous_value`
6. Extractable without visual design? → `fact.facts_locked_in_non_text`
7. Contradictory? → `fact.contradictory_values`
9. Only after interaction? → `fact.behind_interaction`
10. Only in image/PDF? → `fact.facts_locked_in_non_text`

(Questions 5 "appropriate page" and 8 "surrounded by low-value content" are
tracked in the section model but not emitted as findings, to keep precision high.)

## Evidence produced
Each finding stores the mechanism sentence, an evidence snippet (the offending
phrase / conflicting values / hidden text), the affected URLs, and a numeric
`confidence_score`. Example:

"The price in the structured data (Offer 149.0) disagrees with the price shown
in the visible text (199.0) on `/products/widget`, so an assistant may quote a
wrong value."

## False-positive guards
- JavaScript / images are never flagged for existing — only when a fact is
  *materially* harder to extract because of them.
- Ambiguity fires only when a concrete value is absent **and** an explicit vague
  phrase is present (e.g. "contact us for pricing"), so it is quoted as evidence.
- Contradiction requires structured data to disagree with visible text (a real
  data-integrity bug), not merely two numbers on a page.
- Identity fires only on homepages that have no meta description, no Organization
  description, and no plain "we build/provide/offer" sentence.
- Decorative/icon images are excluded before the alt-text check.

## Outputs
Observations for `fact.*` checks. See `references/fact_catalog.md`.
