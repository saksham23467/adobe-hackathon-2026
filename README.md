# brand-ai-readiness-audit

Agent Skill Marketplace that audits **any** website for the two Round-2 failure halves:

- **Off-site AI discoverability** — why an assistant cannot find, read, quote, or trust the brand.
- **On-site engagement** — why visitors who do arrive do not stay.

The entrypoint is read-only. It emits one schema-valid **audit report**: evidence-backed findings (severity + suggested action) plus proactive beyond-defect recommendations. No skill modifies a live site.

## Skills and how the entrypoint composes them

| Skill | Concern |
|---|---|
| `audit-orchestrator` | **Entrypoint.** Validates the target, runs the pipeline, reconciles findings, attaches recommendations, emits the report. |
| `crawl-access` | Robots, sitemaps, HTTP status, redirects, broken links, canonicals, AI-UA blocks. Builds the page-class sample. |
| `render-extract` | Acquire-once cache (initial HTML + optional render). Detects JS-only content and text-as-image. |
| `fact-accessibility` | Facts that cannot be quoted: locked in images, vague values, missing alt, unattributed prices. |
| `structured-identity` | schema.org validity, visible vs markup mismatch, entity ambiguity / duplicates. |
| `freshness-corroboration` | Stale or self-contradictory claims; bounded external corroboration (fail-soft). |
| `engagement-orientation` | Orientation, IA, dead ends, blocking interstitials, unclear navigation. |
| `recommendation-engine` | Mechanism-sound, prioritized `suggested_action` plus beyond-defect ideas. |

Composition: crawl → render/cache → fact / identity / engagement / freshness → recommend → compile + present. One network pass; every detector reads the shared cache.

## Run

```bash
python3 skills/audit-orchestrator/scripts/run_audit.py https://example.com
python3 skills/audit-orchestrator/scripts/run_audit.py --fixture tests/fixtures/clean-brand
python3 tests/run_tests.py
```

Stdlib only. Optional headless renderer. Robots respected. Typical site: **< 5 minutes**.

## Report floor (Adobe handout)

Every report includes `site`, `audited_at`, `summary.{total_findings,critical,high,medium}`, and `findings[]` each with `id`, `title`, `severity`, **`evidence` (string)**, and `suggested_action.{summary,priority}`. Extra diagnostic fields (`evidence_detail`, scores, `why_it_matters_to_ai`, …) are allowed extensions.

## Safety

Recommend-only. GET/HEAD only, `robots.txt` honored, no authentication, no destructive actions, hard caps on pages / bytes / time. Zip target: ≤ 50 MB, no model weights.

## License

MIT (see each skill's `SKILL.md`).
