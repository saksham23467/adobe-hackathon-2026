# Render check catalog (render-extract)

Each check maps to a `check_id` in `config/check_catalog.json` and is emitted by a
detector in `scripts/renderdetect.py`. Findings are tiered:

- **confirmed** → `Status.FAIL`, `confidence=high`
- **likely** → `Status.FAIL`, `confidence=medium`
- **informational** → `Status.PASS` (recorded, never a finding)

| Requirement | check_id | Tier | Predicate | FP guard |
|---|---|---|---|---|
| empty initial HTML shell | `render.empty_initial_shell` | confirmed | raw ≤15 words + mount node/scripts AND rendered ≥40 words | rendered must actually have content |
| major content only after JS / injected facts | `render.primary_content_js_only` | confirmed | gap ratio ≥0.6 AND ≥40 tokens added by JS (on individual pages) | source-empty AND render-empty = thin content, not a gap |
| a content class has a large render gap | `render.class_js_gap` | confirmed | an entire page-class (≥2 sampled pages, all gapping) has the gap | single lazy page ≠ class gap |
| important headings absent from initial HTML | `render.headings_js_only` | confirmed | raw has body text but the h1..h3 outline is only in the rendered DOM | needs ≥half the headings missing; excluded if a broader gap already fired |
| content hidden behind interactions / inaccessible UI | `render.content_behind_interaction` | likely | ≥40 tokens AND ≥40% of DOM text is hidden from the a11y tree | small menus/modals are below threshold |
| structured data injected post-hydration | `render.schema_injected_post_hydration` | likely | JSON-LD absent in raw, present in rendered | valid raw JSON-LD suppresses it |
| meaningful text represented only as images | `render.text_as_image` | likely | rendered visible text <20 words AND ≥1 meaningful (non-icon) image | pages with real text are ignored |
| canvas-based content | `render.canvas_content` | likely | `<canvas>` present AND rendered visible text <20 words | text-complete pages ignored |
| excessive script dependence | `render.excessive_script_dependence` | informational | ≥12 scripts BUT content already in raw HTML | never a defect — JS itself is not flagged |

## The four representations
`scripts/textextract.py` derives, for any HTML:
- **A** initial HTTP response (applied to `raw_html`),
- **B** full DOM text, **C** visible text (hidden nodes removed), **D** headings + JSON-LD
  (applied to `rendered_html`).

Detectors compare A against B/C/D. The core signal is
`gap = 1 - raw_tokens / rendered_visible_tokens`; a defect requires BOTH a low
absolute raw token count and a high gap, so text-complete pages never trip it.

## Evidence stored per finding
URL, initial-HTML snippet + token count, rendered visible snippet + token count,
gap ratio, tokens added by JS, affected-content sample (+ missing headings),
severity (catalog, capped by confidence), confidence (from tier), and a mechanism
explanation.

## Degrade-without-browser
If no rendered DOM is available, the render gap returns `inconclusive` and is
recorded under `limitations` — never `fail`.
