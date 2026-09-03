---
name: render-extract
description: >-
  Detect where the HTTP HTML a no-JS fetcher stores is machine-unreadable:
  empty app shells, text locked in images or canvas, and content hidden from
  the accessibility tree. Live audits fetch with curl (no Playwright or
  Selenium). JavaScript itself is never flagged: a server-rendered page whose
  text is already in the curl body produces no findings. Use when comparing
  what a no-JS fetcher stored to what a visitor sees.
license: MIT
metadata:
  concern: render
  mechanism: "C,F"
allowed-tools: Bash(python3:*) Bash(curl:*) Read
---

# Render & Extract

Detects pages that look complete in a browser but are blank or non-textual to
curl / other no-JS fetchers.

## When to use
After crawl-access has written the acquire-once cache, before fact / identity /
engagement detectors. Read-only; never re-fetches.

## Inputs
- `ctx.cache` page records (`raw_html` from curl GET).
- Optional fixture `rendered_html` for hermetic JS-gap comparison tests.

## Procedure
1. `scripts/textextract.py` derives text, headings, JSON-LD, and SPA mount signals.
2. `scripts/renderdetect.py` runs empty-shell / canvas / image / hidden-text detectors on the curl HTML.
3. `scripts/detect.py` returns observations. No browser is started.

## The engine (`scripts/`)
- `scripts/textextract.py` — stdlib `html.parser`.
- `scripts/renderdetect.py` — `run_raw_http` (live) and `run_all` (fixture comparison).
- `scripts/detect.py` — entrypoint.

## Core principle
A finding exists **only when the HTTP HTML is not machine-readable**. Heavy
scripts with the content already present in the curl body are informational.

## Budget & safety
GET via curl only. Timeouts, byte caps, robots, no site-altering actions.
See `references/browser_budget.md`.

## Findings (tiered)
- **confirmed** (FAIL/high): `render.empty_initial_shell` (and, on fixtures with a second HTML snapshot, primary-content / heading / class JS gaps).
- **likely** (FAIL/medium): `render.content_behind_interaction`,
  `render.text_as_image`, `render.canvas_content`.
- **informational** (PASS): `render.excessive_script_dependence`.

## False-positive guards
- Server-rendered content (React/Vue SSR) → no finding.
- Thin page with no scripts and no SPA mount node → not an empty shell.
- Embedded Next/Nuxt JSON state counts as text.

## Outputs
Observations for `render.*` checks. See `references/render_catalog.md`.
