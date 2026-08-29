---
name: render-extract
description: >-
  Detect where JavaScript materially reduces machine accessibility by comparing
  four representations of each sampled page: the initial HTTP response, the
  browser-rendered DOM, the visible rendered text, and the semantic HTML
  (headings, JSON-LD). Flags empty app shells, primary content or headings that
  appear only after JS, structured data injected post-hydration, content hidden
  behind interactions, and meaningful text trapped in images or canvas — always
  with initial-vs-rendered evidence. JavaScript itself is never flagged: a
  server-rendered React page whose text is already in the raw HTML produces no
  findings. Degrades to inconclusive when no renderer is available.
license: MIT
metadata:
  concern: render
  mechanism: "C,F"
allowed-tools: Bash(python3:*) Read
---

# Render & Extract

Detects the gap between what a machine sees with and without JavaScript, using
four representations per page:

- **A. initial HTTP response** — `record.raw_html` (no JS run).
- **B. browser-rendered DOM** — `record.rendered_html`, full DOM text.
- **C. visible rendered text** — B minus hidden / accessibility-tree-removed nodes.
- **D. semantic representation** — heading outline (h1..h3) and schema.org JSON-LD.

## The engine (`scripts/`)
- `textextract.py` — derives A/B/C/D from any HTML (stdlib `html.parser`);
  separates visible from hidden text, extracts headings, JSON-LD, images, canvas,
  script and SPA-mount signals.
- `renderdetect.py` — the tiered detectors and their mutual-exclusion logic.
- `detect.py` — entrypoint: reads the acquire-once cache, or returns
  `inconclusive` when no rendered DOM exists.

## Core principle
A finding exists **only when JS materially reduces machine accessibility**.
Heavy scripts with the content already present in raw HTML are recorded as
*informational*, never a defect.

## Budget & safety
Rendering is the runtime tent-pole, so it is bounded: at most `max_render` URLs,
a per-URL timeout, capped concurrency, and no site-altering actions. The renderer
is optional — without it the skill degrades to raw-HTTP-only and marks render
checks `inconclusive`. See `references/browser_budget.md`.

## Findings (tiered)
- **confirmed** (FAIL/high): `render.empty_initial_shell`,
  `render.primary_content_js_only`, `render.class_js_gap`, `render.headings_js_only`.
- **likely** (FAIL/medium): `render.content_behind_interaction`,
  `render.schema_injected_post_hydration`, `render.text_as_image`,
  `render.canvas_content`.
- **informational** (PASS): `render.excessive_script_dependence`.

## Evidence per finding
URL, initial-HTML snippet + token count, rendered visible snippet + token count,
the extracted-text difference (gap ratio, tokens added by JS), the affected
content sample (and missing headings), severity, confidence, and a mechanism
explanation of why a no-JS fetcher is harmed.

## False-positive guards
- Server-rendered content (React/Vue SSR) → no finding.
- Source-empty AND render-empty = thin content, not a JS gap.
- Empty shell and primary-content gap are mutually exclusive per URL.
- A single lazy widget ≠ a page-wide gap (require material token thresholds).
- No renderer installed → `inconclusive`, never `fail`.

## Outputs
Observations for `render.*` checks. See `references/render_catalog.md`.
