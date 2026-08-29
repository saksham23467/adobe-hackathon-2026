# Composition & safety model

The orchestrator is a compiler, not a detector. It owns budget, composition,
false-positive suppression, schema validity, and the final report.

## Dependency order
```
crawl-access        -> access checks + page-class sample plan
render-extract      -> acquire-once cache (raw HTTP + optional render) + render-gap
fact-accessibility  -> reads cache
structured-identity -> reads cache
engagement-orient.  -> reads cache
freshness-corrob.   -> reads cache + bounded external (fail-soft)
recommendation-eng. -> transforms findings -> prioritized actions + beyond-defect
```
Each cache-consuming skill declares a data dependency on the cache produced by
`render-extract`. No skill re-fetches; the network is hit once.

## Budget
Total wall-clock target < 5 minutes. `config/settings.json.total_budget_s` is
270s with margin. Each stage checks `SafetyGate.budget_exhausted()`; when the
budget is gone the remaining skills are skipped and recorded in `limitations`,
and a valid (partial) report is still emitted.

## False-positive defenses (enforced in compile.py)
1. Closed taxonomy — a finding is dropped unless its `check_id` is in
   `config/check_catalog.json`.
2. Three-valued detectors — only `fail` becomes a finding; `inconclusive`
   becomes a `limitation`; `pass` / `not_applicable` are dropped.
3. Confidence caps severity — `low` ≤ medium, `medium` ≤ high, `high` ≤ critical.
4. Deterministic ordering and stable IDs.

## Reconciliation (enforced in reconcile.py)
The orchestrator is not a concatenator. After compilation every finding passes
through the reconciliation layer:

- **Merge overlaps** — check_ids that describe the *same underlying problem*
  (e.g. `render.content_behind_interaction` + `fact.behind_interaction` +
  `engagement.content_hidden_interaction`) are grouped; findings whose
  `affected_urls` intersect collapse into one. The strongest evidence is kept,
  `sources`/`affected_urls` are unioned, and the recommendation engine therefore
  emits a single fix (no duplicate recommendations).
- **Corroboration** — when ≥2 *independent concerns* agree, confidence is raised
  one level (capped at `high`). Same-check duplicates are consolidated without a
  spurious confidence bump.
- **Contradiction** — a FAIL disputed by another skill's PASS on the same URL is
  kept but marked `contested`, its confidence lowered, and the disagreement
  recorded. Uncertainty is preserved; certainty is never fabricated.
- **Recompute** — confidence then severity (always capped by confidence).

Overlap groups live in `_OVERLAP_GROUP`; processing is fully sorted so the output
is deterministic for a fixed crawl snapshot.

## Non-expert output
The final `summary` carries a plain-language `headline` and `by_category` counts;
each finding carries a friendly `category`, `evidence_type`, `affected_urls`, and
a `suggested_action` answering what/where/why/how/impact/effort/first.

## Safety guarantees
- GET/HEAD only (enforced in `SafetyGate.assert_readonly`).
- robots.txt honored for our own fetches.
- No Authorization/Cookie credentials; no authenticated areas.
- Caps on pages, bytes, concurrency, external calls, and total time.
- No writes to the target; the marketplace only reads and reports.
