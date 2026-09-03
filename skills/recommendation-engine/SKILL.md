---
name: recommendation-engine
description: >-
  Transform compiled audit findings into concrete, mechanism-specific
  engineering / content / UX actions, prioritized by a multi-factor model, and
  add genuinely-useful proactive recommendations. Every action answers eight
  questions: what to change, where, why it fixes the detected mechanism, how to
  implement it, what evidence supports it, expected impact, implementation
  effort, and what to do first. Prioritization blends impact, confidence,
  severity, scope, effort and dependencies — it deliberately does NOT map high
  severity to high priority. Recommendations that share a fix are de-duplicated,
  and proactive suggestions are emitted only where relevant (never generic
  filler). Use as the final composition step before emitting the report.
license: MIT
metadata:
  concern: recommendation
  mechanism: "A-F"
allowed-tools: Bash(python3:*) Read
---

# Recommendation Engine

The only place actions are minted. Consumes normalized findings, attaches a
mechanism-specific, prioritized `suggested_action` to each, de-duplicates shared
fixes, and adds relevant proactive recommendations.

## When to use
After compilation, before the orchestrator emits the report.

## Inputs
- Compiled `findings[]` from the orchestrator (catalog-valid).
- Audit context (`ctx`) for page-classes and coverage used by beyond-defect gates.

## Procedure
1. `scripts/recommend.py` attaches a `suggested_action` to each finding from
   `scripts/templates.py`.
2. Rank by the multi-factor score; flag `do_first` for critical items or
   low-effort high-score items with no blockers.
3. De-duplicate shared fixes; emit relevant `beyond_defect_recommendations`.

## The eight questions each action answers
1. **what** — exactly what should change
2. **where** — where it should change
3. **why** — why it solves the *detected mechanism* (not generic SEO)
4. **how** — `steps` (+ optional `patch`)
5. **evidence** — the observed evidence from the finding
6. **expected_effect** — expected impact (with an `impact` weight)
7. **effort** — implementation difficulty (low | medium | high)
8. **first** — sequencing via `priority_score`, `rank`, `do_first`, `dependencies`

## Prioritization (multi-factor — NOT severity → priority)
```
score = 100 · impact · severity_w · confidence_w · scope · effort_blend · dep_mult
priority_bucket = critical (≥65) | high (≥42) | medium (else)
```
- **impact** — per-check benefit to machine-readability / trust (0..1).
- **severity_w / confidence_w** — from the finding.
- **scope** — site-wide > class-wide > multi-page > single-page (from evidence).
- **effort_blend** — lower effort ranks higher, but never dominates.
- **dep_mult** — a finding blocked by an unresolved prerequisite is deferred.

A low-effort, high-impact, site-wide *medium*-severity fix can therefore outrank
a high-effort, single-page *high*-severity one.

## Dependencies & sequencing
Access blockers (bot/index blocks, dead home page) gate everything; render
blockers (JS-only content, empty shell) gate fact/identity/freshness fixes.
Dependents list their prerequisites in `dependencies` and are penalized until
resolved; foundational quick wins are flagged `do_first`.

## De-duplication
Findings that share a fix (same `check_id` across pages) collapse to one
canonical recommendation whose `where` aggregates every affected location; the
rest are marked `duplicate_of` the canonical id.

## Beyond-defect (proactive) recommendations
Emitted only when (a) the related concern has no live defect, (b) the item is
relevant to this site's page-classes (e.g. FAQPage only when an FAQ page exists),
and (c) it is not a duplicate. No generic filler.

## Outputs
`findings[]` with `suggested_action` attached, plus
`beyond_defect_recommendations[]`. See `references/fix_templates.md` and
`references/priority_function.md`.
