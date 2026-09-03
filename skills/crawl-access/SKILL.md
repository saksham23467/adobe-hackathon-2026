---
name: crawl-access
description: >-
  Audit whether AI/search crawlers can reliably discover and reach a website's
  important content. Runs a bounded, robots-respecting, read-only crawler from
  the homepage and evaluates robots.txt availability and directives, sitemap
  discovery and validity, HTTP status codes, redirect chains, canonical
  consistency, noindex / X-Robots-Tag, broken internal links, orphan pages,
  depth, crawl traps, URL normalization, duplicate variants, pagination, and
  crawl-budget risk. Every finding is tiered (confirmed defect / likely issue /
  informational) and carries exact re-runnable evidence. Use as the first stage
  of an AI-discoverability audit to determine why a brand cannot be found.
license: MIT
metadata:
  concern: crawl_access
  mechanism: "A,B"
allowed-tools: Bash(python3:*) Bash(curl:*) Read
---

# Crawl & Access

Answers the first question in the discovery pipeline: can a machine be let in,
reach the important pages, and read a clean set of URLs?

## When to use
As the first detection stage, before rendering. It owns the crawl and produces a
normalized `CrawlResult` plus tiered observations for the `access.*` checks.

## Inputs
- `ctx.entry_url`, `ctx.gate`, crawl settings from `ctx.config`.
- Optional `ctx.fixture_dir` for hermetic runs.

## Procedure
1. Choose a fetcher (live / fixture).
2. `scripts/crawler.py` runs a bounded, robots-respecting crawl
   (`scripts/html_parse.py`, `scripts/urlnorm.py`).
3. Build a `scripts/crawl_result.py` `CrawlResult`; `scripts/detectors.py` emit
   tiered observations.
4. `scripts/detect.py` returns observations and attaches the crawl to `ctx`.

## The engine (`scripts/`)
- `scripts/urlnorm.py` — URL normalization, lossy dedup keys, variant/trap detection.
- `scripts/html_parse.py` — lightweight HTML link/canonical/meta extraction.
- `scripts/crawl_result.py` — the normalized `CrawlResult` (pages, robots, sitemaps,
  link graph, stats). All detectors are pure functions over this structure.
- `scripts/crawler.py` — the bounded crawler and pluggable fetchers (`LiveFetcher`,
  `FixtureFetcher`, `DictFetcher`) plus robots and sitemap parsing.
- `scripts/detectors.py` — the 20 tiered detectors and the tier→Observation mapping.
- `scripts/detect.py` — orchestrator entrypoint: picks a fetcher, runs the crawl,
  returns Observations.

## Safety & limits (enforced by the crawler)
- **Read-only**: GET only, credentials never attached, `robots.txt` honored for
  our user-agent.
- **Bounded**: hard caps on pages, depth, redirects, and a wall-clock deadline;
  URLs deduplicated by lossy key so the crawl can never loop infinitely.
- **Polite**: per-host rate limiting via the shared safety gate.
- **Degrades cleanly**: an unreachable robots.txt or a missing sitemap becomes an
  informational note, never a fabricated defect.

## Three-tier findings (precision over recall)
- **confirmed** — provably broken (e.g. homepage 5xx, `Disallow: /`, a canonical
  that points to a 404, a linked URL returning 4xx). Emitted as a high-confidence
  finding.
- **likely** — a strong signal with some benign explanations (e.g. orphan pages,
  long redirect chains, duplicate variants). Emitted as a capped finding.
- **informational** — noteworthy but not a defect (e.g. no robots.txt, no
  sitemap, inconsistent URL forms). Recorded, never a finding.

We never emit a finding merely because a best practice is absent.

## Evidence produced
Per check: exact URLs, HTTP statuses, redirect hops, robots stanzas, sitemap
URLs, and counts — all re-runnable. Example: "1 internal link target returns 404
(`/missing`), linked from `/`."

## Outputs
Observations for the `access.*` checks (see `references/access_catalog.md`) and a
`CrawlResult` attached to the audit context for downstream reuse.
See also `references/ai_user_agents.md`.
