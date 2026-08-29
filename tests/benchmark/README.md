# Runtime & network benchmark

Profiles the whole marketplace for runtime and network efficiency against the
**< 5 minute** target, ranks the largest bottlenecks from measured evidence, and
verifies that the optimizations did not reduce audit accuracy.

## Run it

```bash
# Hermetic: profile every adversarial fixture (deterministic, no network)
python3 tests/benchmark/benchmark.py --repeats 3 --json tests/benchmark/reports/hermetic.json

# Live: profile a real end-to-end audit (needs network)
python3 tests/benchmark/benchmark.py --url https://example.com --json tests/benchmark/reports/live.json

# CI gate: non-zero exit if any run exceeds 5 minutes
python3 tests/benchmark/benchmark.py --fail-over-budget
```

## What it measures

Per run and in aggregate: **total runtime, crawl time, rendering time, external
search time, number of requests, duplicate requests, pages analyzed, maximum
concurrency, and memory usage** (tracemalloc peak + RSS high-water), plus a
per-phase and per-skill time breakdown. Telemetry comes from `meta.perf` on the
real report, so the same numbers are available on any production audit.

## Measured results

Full 40-page **live** crawl of `https://www.iana.org` (representative typical site):

| metric | value |
|---|---|
| total runtime | **41.1s** (target 300s → 7× margin) |
| crawl fetch (network) | 26.7s (64.8%) |
| external search | 1.94s (4.7%) |
| requests / duplicates | 42 / **0** |
| URLs deduplicated | **1,043** |
| pages analyzed | 40 |
| max concurrency | 4 |
| peak memory / RSS | 29.8 MB / 103 MB |

Hermetic (21 fixtures): slowest run 40 ms, median 23 ms, duplicates 0, peak mem 0.2 MB.

## The 10 largest bottlenecks (from the 40-page live run) and what was done

| # | Bottleneck | Cost | Fix (accuracy-preserving) |
|---|---|---|---|
| 1 | **Network crawl fetch** | 26.7s (64.8%) | Per-host **politeness delay (0.5s)** is the intentional floor. Mitigated by **bounded concurrency** (overlaps latency), **URL dedup** (1,043 redundant fetches avoided), and hard **crawl limits** + deadline early-stop. |
| 2 | `fact-accessibility` CPU | 4.5s | Reads the shared acquire-once cache (no re-fetch). Per-page DOM scan is inherent; bounded by `max_pages`. |
| 3 | `engagement-orientation` CPU | 4.5s | Same shared cache; single parse pass per page. |
| 4 | `structured-identity` CPU | 2.9s | JSON-LD/microdata parse over cached HTML. |
| 5 | `freshness-corroboration` CPU | 2.1s | Now reuses the **shared visible-text artifact** instead of re-stripping HTML. |
| 6 | **External Wikidata search** | 1.94s | **Gated to useful cases only** — skipped when a brand is already self-disambiguated (`sameAs`) with no claims to cross-check; still runs for every case that could surface a defect. One entity lookup per audit, budgeted. |
| 7 | `crawl-access` detectors | 0.34s | Pure functions over the crawl result. |
| 8 | Report build (compile/reconcile/recommend/present) | 0.01s | Negligible. |
| 9 | Context build / acquire | ~0s | Negligible. |
| 10 | Schema validation | ~0s | Negligible. |

Earlier, a trivial single-page live audit spent **42% of its time on an external
search that could never produce a finding** — that class of waste is what fix #6
removes.

## Optimizations implemented (maps to the brief)

- **URL deduplication** — lossy `dedup_key` BFS; a request ledger proves it (`duplicate_requests` = 0, 1,043 deduped).
- **Caching** — acquire-once `Cache` (fetch once, all six skills read) + a **shared visible-text artifact** computed once per page.
- **Bounded concurrency** — level-synchronous BFS thread pool (`max_concurrency`), a **thread-safe** per-host rate limiter and gate counters; output is **deterministic** (proven: concurrent crawl == sequential crawl, byte-for-byte).
- **Request timeouts** — `request_timeout_s` on every fetch.
- **Crawl limits** — `max_pages`, `max_depth`, `max_redirects`, `deadline_s`, `sitemap_fetch_cap`, with page-cap and deadline early-stop.
- **Rendering only where necessary** — `renderpolicy.select_for_render` picks only script-driven *shells*, prioritized and capped by `max_render`.
- **External search only where useful** — `_worth_external` gate.
- **Early stopping** — crawler stops at deadline/page-cap; orchestrator skips skills once the wall-clock budget is exhausted; per-stage budgets.
- **Shared page artifacts between skills** — the acquire-once cache plus shared text.

## Accuracy was **not** traded for speed

Nothing was sped up by skipping an important check. Proven after the changes:

- `python3 tests/run_tests.py` → **248 passed, 0 failed** (incl. a test that concurrent crawl output equals sequential).
- Adversarial suite → **precision 1.0, recall 1.0, FP-rate 0.0**.
- Regression diff (baseline → optimized) → **UNCHANGED**: 0 added / 0 removed / 0 changed findings.
