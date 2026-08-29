# Report contract

The entrypoint emits one JSON object. Required floor (from the Adobe handout):

- `site` (string)
- `audited_at` (ISO-8601 string)
- `summary` with `total_findings`, `critical`, `high`, `medium`
- `findings[]`, each with `id`, `title`, `severity`, `evidence` (**string**), `suggested_action`

The full schema (required + permitted extensions) is
`config/report_schema.json`, validated on every run by
`audit_common.schema.validate_report`.

## Extensions we add (all optional per the handout)
- `findings[].check_id`, `concern`, `mechanism` (A–F), `confidence`
- `findings[].category` — non-expert label (e.g. "Structured Identity")
- `findings[].contested` — true when skills disagreed (confidence was lowered)
- `findings[].affected_urls` — every page the issue touches
- `findings[].evidence_type` — friendly method ("initial HTML", "rendered DOM", …)
- `findings[].evidence`: **string** (handout floor — the observed fact)
- `findings[].evidence_detail`: `{sentence, observed_fact, method, scope, metric, artifacts, measurement, basis}`
- `findings[].sources[]` — which skills/checks/methods contributed (after merge)
- `findings[].suggested_action`: `{summary, priority, what, where, why, steps,
  patch, evidence, expected_effect, effort, impact, priority_score, rank,
  do_first, dependencies, duplicate_of}`
- `beyond_defect_recommendations[]`: proactive, non-defect improvements
- `summary.headline`, `summary.by_category`: a plain-language overview
- `coverage`: what was discovered/sampled/rendered
- `limitations[]`: checks that could not be completed (inconclusive)
- `meta`: runtime, renderer availability, catalog version, `reconciliation`
  (`{input, output, merged, contested}`)

## Presentation layer (professional AI-readiness diagnostic)
After reconciliation and recommendations, `scripts/presentation.py` re-expresses
the report for a non-expert reader. It is **additive and never invents findings
or changes severity/confidence** — it only surfaces what was already established.

Added top-level sections:
- `executive_summary` — one plain-language paragraph (no marketing).
- `overall_score`, `discoverability_score`, `engagement_score` (0–100) and
  `overall_grade`. Deterministic penalty model: per finding
  `severity_penalty × confidence_weight × (0.6 if contested)`, subtracted from
  100 and clamped. Discoverability aggregates crawl/render/fact/identity/
  freshness; engagement is scored separately so one axis never masks the other.
- `confidence_summary` — counts by confidence + contested count + a note.
- `top_priorities` — the ranked shortlist (id, title, category, severity,
  `do_first`, action, expected_effect).
- `proactive_opportunities` — reader-facing form of
  `beyond_defect_recommendations`.
- `crawl_summary`, `coverage_summary` — what was fetched/sampled/rendered.
- `reading_guide` — how to read the three epistemic tiers below.

Every finding gains a strict **observed fact / inferred problem / recommendation**
separation so an inference is never presented as an observation:
- `evidence` — the observed-fact sentence (handout-required string).
- `evidence_detail.observed_fact` — the same literal measured/observed clause.
- `evidence_detail.measurement` + `evidence_detail.basis` (`measured`|`observed`).
- `inferred_problem` — the defect *concluded* from the observation.
- `suggested_action.implementation` (`{where, steps, patch}`) — a proposed fix.
- `impact` (`{level, axis, expected_effect}`) and `why_it_matters_to_ai` — the
  retrieval/extraction mechanism the defect breaks.

## Reconciliation (never a blind concatenation)
Between compilation and recommendation, `scripts/reconcile.py`:
- **merges overlapping findings** — several skills detecting the same underlying
  problem on the same page(s) collapse into one finding that keeps the strongest
  evidence, unions `affected_urls`, records all `sources`, and — for independent
  corroboration — raises confidence one level (never above `high`);
- **resolves contradictions** — a FAIL disputed by another skill's PASS on the
  same page keeps the defect but marks it `contested`, lowers confidence, and
  notes the disagreement. Certainty is never fabricated;
- **recomputes** confidence (corroboration ↑ / contradiction ↓) and severity
  (always capped by the final confidence).

## Determinism
For a fixed crawl snapshot the findings, summary and recommendations are
deterministic (total sort orders, stable ids). Only `audited_at` /
`meta.wall_clock_s` vary.

## Evidence sentence pattern
`scope + measurement + implication`, e.g.:
"Fetched 12 `/products/*` URLs; 0/12 contain schema.org Product in HTML source
or rendered DOM — assistants have no structured fact to quote."
