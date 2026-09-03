---
name: audit-orchestrator
description: >-
  Entrypoint skill for the brand-ai-readiness-audit marketplace. Given a
  website URL, it safely audits the site for AI-discoverability and on-site
  engagement problems by composing the detection skills (crawl-access,
  render-extract, fact-accessibility, structured-identity,
  freshness-corroboration, engagement-orientation), compiles their evidence
  into normalized findings, attaches prioritized fixes via recommendation-engine,
  and emits one structured audit report (findings + suggested actions). Use when
  asked to audit, diagnose, or improve why a brand is missing or misrepresented
  in AI assistants, or why visitors who arrive do not engage.
license: MIT
metadata:
  role: entrypoint
  version: "1.0.0"
allowed-tools: Bash(python3:*) Bash(curl:*) Read
---

# Audit Orchestrator (entrypoint)

Composes the marketplace's detection skills into a single, read-only audit and
emits one report against the fixed schema. It is the only skill that produces
the final report, enforces the time budget, and guarantees safety and schema
validity.

## When to use
- The user gives a website (URL or domain) and wants an AI-readiness audit.
- The user asks why a brand isn't found/cited by AI assistants, or why
  arriving visitors don't stay.

## Inputs
- `site`: a URL or bare domain (e.g. `example.com`).
- Optional: path to a config JSON (defaults to `config/settings.json`).

## Procedure (deterministic)
1. **Validate the target** and normalize the URL (`validate_target`: http(s)
   only, real host), then initialize the read-only safety gate (GET-only,
   **robots-respecting**, capped, time-budgeted).
2. **Initialize a bounded crawl** — run `crawl-access` to check access and
   discover page-classes; build a representative sample plan.
3. Run `render-extract` to acquire each sampled URL once (HTTP + optional
   render) into the shared cache and detect JS-render gaps.
4. **Invoke the relevant audit skills** — the cache-consuming detectors
   `fact-accessibility`, `structured-identity`, `engagement-orientation`, and,
   if time and network remain, `freshness-corroboration` (fail-soft).
5. **Collect normalized findings** (`scripts/compile.py`) — drop anything not
   in the closed check catalog, keep only `fail` (inconclusive → `limitations`),
   assign severity by rule (capped by confidence), stable IDs.
6-9. **Reconcile** (`scripts/reconcile.py`) — never a blind concatenation:
   **deduplicate/merge** overlapping findings across skills (strongest evidence
   kept, sources recorded), **resolve contradictions** (disagreement → lower
   confidence, mark `contested`, never fabricate certainty), and **recalculate
   confidence and severity**.
10-11. Call `recommendation-engine` to attach mechanism-specific, prioritized
   `suggested_action`s and generate relevant **proactive** (beyond-defect)
   recommendations.
12. **Produce the final report**: enrich with a non-expert `summary.headline` and
   `by_category`, then run the presentation layer (`scripts/presentation.py`) to
   add scores (`overall_score`, `discoverability_score`, `engagement_score`), an
   `executive_summary`, `top_priorities`, `confidence_summary`,
   `proactive_opportunities`, `coverage_summary`, and — per
   finding — a strict **observed fact / inferred problem / recommendation**
   separation plus a "why this matters to AI systems" explanation. Validate
   against `config/report_schema.json` and emit it. Even on partial runs
   (timeout / missing renderer), emit a valid report with a populated
   `limitations` array.

## Scripts
- `scripts/run_audit.py` — marketplace entrypoint (validate, compose, emit).
- `scripts/compile.py` — catalog-gate observations into findings.
- `scripts/reconcile.py` — merge overlaps and mark contested findings.
- `scripts/presentation.py` — scores, headline, observed/inferred/recommendation.

## Running it
```bash
python3 skills/audit-orchestrator/scripts/run_audit.py https://example.com
# hermetic fixture mode (no network):
python3 skills/audit-orchestrator/scripts/run_audit.py --fixture tests/fixtures/clean-brand
```
The script prints a report to stdout that satisfies the required schema and runs
even without an LLM present.

## Outputs
A single JSON audit report. Required floor (Adobe handout): `site`,
`audited_at`, `summary` (counts by severity), `findings[]` each with `id`,
`title`, `severity`, **`evidence` as a non-empty string**, and
`suggested_action.{summary,priority}`. Extensions (allowed): `evidence_detail`
(structured observed-fact / measurement), `executive_summary`, scores,
`confidence_summary`, `top_priorities`, `proactive_opportunities`,
`coverage_summary`, and per-finding `category`, `confidence`,
`mechanism`, `affected_urls`, `impact`, `inferred_problem`,
`why_it_matters_to_ai`. Observed fact stays distinct from the inference and
the fix. See `references/report_contract.md`.

## Safety
Read-only. GET/HEAD only, robots.txt respected, no authentication, no
site-altering actions, hard caps on pages/bytes/time. See
`references/composition.md`.
