---
name: freshness-corroboration
description: >-
  Detect factual claims that are stale, weakly corroborated, inconsistent, or
  hard for an AI system to confidently trust — without blindly demanding external
  mentions. First extracts high-value on-site claims (company description,
  products, location, pricing, leadership, certifications, awards, availability,
  specs, operating status) and content dates, then checks staleness,
  self-contradiction, and unusually strong but unsupported claims on-site. Only
  when a corroboration backend and budget exist does it conservatively
  cross-check the brand entity against independent sources (Wikidata + declared
  sameAs) for conflicts and cross-source ambiguity. Absence of coverage is never
  a finding, so small/local/new brands are not penalized. Every finding is
  classified corroborated / partially_corroborated / inconsistent / stale-risk /
  insufficient_evidence with source URLs and exact evidence. The only skill that
  leaves the origin; strictly bounded and fail-soft. Use when time and
  network remain.
license: MIT
metadata:
  concern: freshness
  mechanism: "D"
compatibility: Network access improves this skill; it degrades to inconclusive offline.
allowed-tools: Bash(python3:*) Bash(curl:*) Read
---

# Freshness & Corroboration

Machines trust facts that are current, self-consistent, and (for prominent
brands) repeated across independent sources. This skill checks all three and is
the only one that leaves the origin — under strict, fail-soft rules.

## When to use
Last detection stage, only if time and network remain. Always fail-soft.

## Inputs
- `ctx.cache` page records (on-site claims and dates).
- Optional corroboration backend and remaining `ctx.gate` network budget.

## Procedure
1. `scripts/claims.py` extracts on-site dates and high-value claims.
2. If budget remains, `scripts/corroborate.py` does at most one fail-soft lookup.
3. `scripts/freshdetect.py` classifies and emits observations.
4. `scripts/detect.py` returns them, or a limitation when the backend is unavailable.

## Engine
- `scripts/claims.py` — extracts content dates (JSON-LD, `<time>`, meta, "last
  updated", copyright) and high-value claims (founding year, leadership, operating
  status, superlatives, brand name, `sameAs` presence). On-site only.
- `scripts/corroborate.py` — the only network component. `LiveCorroborator`
  (Wikidata, budgeted, fail-soft) and `FixtureCorroborator` (hermetic tests)
  behind one tiny `lookup(name) -> CorroborationResult` interface. At most one
  entity lookup per audit.
- `scripts/freshdetect.py` — the tiered detectors + classification.
- `scripts/detect.py` — entrypoint over `ctx.cache`.

## Checks
| check_id | Runs | Problem it proves | Classification |
|---|---|---|---|
| `freshness.content_stale` | on-site | newest dated content older than threshold | stale-risk |
| `freshness.self_contradiction` | on-site | conflicting founding year / leadership / operating status | inconsistent |
| `freshness.unsupported_claim` | on-site | superlative/absolute claim with no attribution or source | insufficient_evidence |
| `freshness.claim_conflict` | external | a specific claim conflicts with an independent source | inconsistent |
| `identity.entity_ambiguous` | external | name matches ≥2 prominent entities and site has no sameAs | inconsistent |

Corroborated / partially-corroborated results are recorded as PASS observations
(never findings); when no backend/budget exists the external check is
`inconclusive` (a limitation, not a finding).

## Evidence produced
"Site founding year ['2010'] conflicts with Northwind Tools
(wikidata.org/wiki/Q123): 2009." Each finding carries `classification`, the
claim category, `site_value` / `external_value`, `source_urls`, `mechanism`, and
`fix`.

## False-positive guards (this is the highest-FP skill)
- **Absence of external coverage is never a finding.** A confident *match* is
  required before any external conflict/ambiguity is even considered — small,
  local, and new brands are not penalized.
- Copyright year is never treated as a content date (staleness needs a real
  dated content signal); a site with no content dates yields no stale finding.
- Superlatives are flagged only when the sentence has no citation, ranking
  source, or year — "voted best by City Times 2025" is *supported*.
- Any network error/timeout → `inconclusive`, never invented corroboration.

## Outputs
Observations for `freshness.*` (and cross-source `identity.entity_ambiguous`).
See `references/corroboration_sources.md`.
