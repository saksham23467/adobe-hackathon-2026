# Freshness & corroboration — rules and catalog

## The pipeline
1. **Extract on-site claims** (`claims.py`): content dates + high-value claims
   (company description, products/services, location, pricing, leadership,
   certifications, awards, availability, specifications, operating status).
2. **On-site checks** (no network): staleness, self-contradiction, unusually
   strong-but-unsupported claims.
3. **External corroboration** (only if a backend + budget exist): one
   conservative brand-entity lookup, compared for conflicts and cross-source
   ambiguity.

## Classification (stored on every observation as `classification`)
- `corroborated` — claim agrees with an independent source (PASS).
- `partially_corroborated` — matched entity, some claims unverified (PASS).
- `inconsistent` — a real conflict (self-contradiction or vs external) → FAIL.
- `stale-risk` — newest dated content older than the threshold → FAIL.
- `insufficient_evidence` — strong unbacked claim (FAIL), or no confident match
  (PASS/inconclusive — never a finding on its own).

## Sources (bounded, cached, GET-only)
- Wikidata `wbsearchentities` + `wbgetentities` claims (P571 inception, etc.).
- The site's own declared `sameAs` targets.
Total external calls capped by `settings.max_external_calls` (default 8) and the
external stage time budget. Every call is fail-soft.

## Checks
| check_id | Predicate | Evidence | FP guard |
|---|---|---|---|
| `freshness.content_stale` | newest **content** date older than `freshness_stale_days` (default 1095) | date + kind + URL | copyright year is not a content date; no dates ⇒ no finding |
| `freshness.self_contradiction` | conflicting founding year / same-role leadership / opposing operating status | the conflicting values + URLs | requires ≥2 distinct concrete values |
| `freshness.unsupported_claim` | superlative/absolute claim with no citation/source/year | the sentence + URL | supported claims (ranked/voted/year) are not flagged; confidence `low` |
| `freshness.claim_conflict` | a specific claim disagrees with a **matched** external source | site vs external value + source URL | requires a confident match and a real conflict, never absence |
| `identity.entity_ambiguous` | name matches ≥2 prominent external entities and site declares no `sameAs` | candidate count + source | needs a prominent collision + missing disambiguators |

## The cardinal rule
Absence of external coverage is **not** evidence of a problem. Only a **conflict**,
a **self-contradiction**, an **isolated unsupported strong claim**, or a genuine
**stale date** can produce a finding. When in doubt: `inconclusive` → limitation.
Small, local, and new businesses are never penalized for having fewer web mentions.
