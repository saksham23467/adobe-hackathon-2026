# Synthetic Adversarial Website Test Suite

A benchmark of **21 realistic, multi-page fixture sites**, each engineered around a
specific AI-readiness failure mode (or engineered to be deliberately clean). Every
fixture declares ground-truth expectations, and the harness scores the *real*
`audit-orchestrator` entrypoint against them to produce **precision, recall,
false-positive rate, and false-negative rate**.

The fixtures are written like real sites — full `<head>`, nav, footers, JSON-LD,
industry-specific copy across coffee, law, SaaS, ecommerce, news, docs, a bakery,
etc. — specifically so they expose brittle, over-fit detector logic instead of
rewarding it. (Two genuine detector bugs were found and fixed this way; see below.)

## Run it

```bash
# Full table + micro-averaged metrics
python3 tests/adversarial/run_suite.py

# List every false positive / false negative
python3 tests/adversarial/run_suite.py --verbose

# Score a subset, or dump machine-readable results
python3 tests/adversarial/run_suite.py --only saas ecommerce
python3 tests/adversarial/run_suite.py --json tests/adversarial/results.json
```

The same evaluation runs inside the main unit suite as a standing regression
guard (`tests/test_adversarial_suite.py`), so a detector change that starts
crying wolf on a clean site, or stops catching a planted defect, fails CI.

## Scoring model

For each fixture, over the **closed catalog** of check ids (`config/check_catalog.json`):

| set | meaning |
|-----|---------|
| `expected`  | check ids a competent auditor **must** report (ground-truth positives) |
| `ignore`    | acceptable either way — not scored (documented, defensible ambiguity) |
| `forbidden` | **every other catalog check id** — ground-truth negatives (default-deny) |

`reported` = the union of each finding's `check_id` **and** its reconciled
`sources[].check_id`, so merged findings still count.

```
TP = |expected  ∩ reported|     FN = |expected  − reported|
FP = |forbidden ∩ reported|     TN = |forbidden − reported|

precision = TP/(TP+FP)   recall  = TP/(TP+FN)
fp_rate   = FP/(FP+TN)   fn_rate = FN/(FN+TP)
```

Default-deny is deliberate: any finding that fires without a planted defect is a
false positive. Expectations are stored per fixture in `pages.json` under
`audit_expectations` (with a human-readable `notes` explaining the mechanism).

## Fixtures and expected findings

| # | Fixture | Failure mode | Expected findings |
|---|---------|--------------|-------------------|
| 1 | `perfect-static` | Perfect static site (precision anchor) | *(none)* |
| 2 | `js-only-app` | Client-side-rendered SPA | `render.empty_initial_shell` |
| 3 | `empty-shell` | Empty shell (curl HTML only) | `render.empty_initial_shell`, `engagement.no_orientation` |
| 4 | `facts-in-images` | Menu/prices only in images | `fact.informational_images_no_alt` |
| 5 | `missing-canonical` | Same content at many URL forms, no canonical | `access.duplicate_url_variants` |
| 6 | `conflicting-canonical` | Canonical → 404 and cross-host canonical | `access.canonical_to_error`, `access.canonical_inconsistent` |
| 7 | `broken-sitemap` | Served but malformed XML sitemap | `access.sitemap_invalid` |
| 8 | `blocked-robots` | `Disallow: /` for all agents | `access.global_disallow_all` |
| 9 | `blocked-ai-crawlers` | AI bots blocked, search allowed | `access.ai_ua_blocked` |
| 10 | `noindex-site` | Site-wide `noindex` | `access.global_noindex` |
| 11 | `orphan-pages` | Page in sitemap, no internal links | `access.orphan_pages` |
| 12 | `contradictory-company-info` | Two founding years for the brand | `freshness.self_contradiction` |
| 13 | `stale-info` | Flagship article dated 2016 | `freshness.content_stale` |
| 14 | `misleading-structured-data` | Schema price ≠ visible price; modified < published | `fact.contradictory_values`, `identity.schema_contradiction` |
| 15 | `ambiguous-identity` | Two conflicting primary orgs | `identity.duplicate_entity` |
| 16 | `discoverable-not-engaging` | Great crawl/schema, broken UX | `engagement.unclear_navigation`, `engagement.inconsistent_naming`, `engagement.dead_end_page` |
| 17 | `engaging-not-discoverable` | Great UX, noindex + AI blocked | `access.ai_ua_blocked`, `access.important_noindex` |
| 18 | `ecommerce` | Faceted-nav crawl trap + unattributed price | `access.crawl_trap`, `access.crawl_budget_risk`, `fact.not_attributed_to_entity` |
| 19 | `saas` | Pricing stated only vaguely | `fact.ambiguous_value` |
| 20 | `local-business` | Thin but correct small business (precision anchor) | *(none)* |
| 21 | `docs-heavy` | Old but evergreen docs (precision test) | *(none — old dates must NOT flag stale)* |

Fixtures 1, 20, and 21 are **precision anchors**: they contain no defects (or only
correctly-excused ones) and must produce zero findings. Fixtures 16/17 test that
the discoverability and engagement axes are scored independently.

## Bugs this suite caught

The suite is not just a pass/fail gate — building it surfaced real detector
weaknesses that were then fixed:

1. **`identity.schema_contradiction` false positive on subtype `@id` reuse.**
   Sharing one `@id` across `Organization` and `LocalBusiness` (a valid
   schema.org subtype relationship) was flagged as a contradiction. Fixed by
   only flagging `@id` reuse across *incompatible* type families
   (`skills/structured-identity/scripts/identitydetect.py`).

2. **`fact.ambiguous_value` phrase-list brittleness.** Vague pricing was only
   detected via a fixed phrase list, so natural copy like "pricing is flexible"
   or "pricing scales with your team" slipped through. Fixed by adding
   pricing-anchored paraphrases that cannot misfire on non-pricing text
   (`skills/fact-accessibility/scripts/factextract.py`).

## Adding a fixture

1. Create `fixtures/<name>/` with realistic `*.html`, an optional `robots.txt` /
   `sitemap.xml`, and a `pages.json` (`pages[]`, optional `aux[]`, and an
   `audit_expectations` block with `mode`, `notes`, `expected`, `ignore`).
2. Run `python3 tests/adversarial/run_suite.py --only <name> --verbose`.
3. Any finding you did not plant shows up as a false positive — either it is a
   real defect you should add to `expected`, a defensible edge case for `ignore`
   (with a note), or a detector bug worth fixing.
