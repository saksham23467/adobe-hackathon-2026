# Priority function (detail)

Priority is a **multi-factor score**, not a `severity → priority` map. This is
deliberate: the most severe defect is not always the most valuable next action
(a trivial site-wide unblock can beat an expensive single-page rewrite).

## Factors

| Factor | Source | Values |
|---|---|---|
| `impact` | per-check template (fallback: per-concern) | 0.30 – 1.00 |
| `severity_w` | finding.severity | critical 1.0 / high 0.75 / medium 0.5 |
| `confidence_w` | finding.confidence | high 1.0 / medium 0.75 / low 0.5 |
| `scope` | finding.evidence (counts / site-wide check ids) | site_wide 1.0 / class_wide 0.8 / multi_page 0.6-1.0 / single_page 0.5 |
| `effort_blend` | template.effort | `0.55 + 0.45·effort_w` (low 1.0 / medium 0.7 / high 0.45) |
| `dep_mult` | prerequisites unresolved? | 1.0 or 0.6 |

```
priority_score = 100 · impact · severity_w · confidence_w · scope · effort_blend · dep_mult
```

`effort` is blended (not multiplied raw) so a high-impact, high-effort fix is
down-weighted but never buried beneath a trivial cosmetic one.

## Buckets (report `suggested_action.priority`)
```
priority_score ≥ 65  -> critical
priority_score ≥ 42  -> high
otherwise            -> medium
```
Because the bucket derives from the composite score, it is decoupled from the raw
`finding.severity`.

## Ranking & sequencing
`rank` is the global execution order = sort by
`(priority_score desc, easier effort first, severity desc, check_id)`.

`do_first = true` marks foundational quick wins: **no unresolved dependencies**,
effort ∈ {low, medium}, and `priority_score ≥ 42`.

## Dependency model (the appendix A→B→C→D→E chain)
- **Access blockers** — `ai_ua_blocked`, `our_ua_blocked`, `global_noindex`,
  `global_disallow_all`, `homepage_unreachable`, `class_noindex`,
  `important_noindex`, `robots_unreachable`. Every non-access finding depends on
  these (a machine that cannot fetch cannot benefit from any other fix).
- **Render blockers** — `primary_content_js_only`, `empty_initial_shell`,
  `class_js_gap`. Fact / identity / freshness findings depend on these (there is
  no point tuning structured data a fetcher never receives).

Dependents carry their prerequisites in `suggested_action.dependencies` (and
`related_findings`) and are multiplied by `dep_mult = 0.6` until the prerequisite
is fixed, so upstream blockers are recommended first.
