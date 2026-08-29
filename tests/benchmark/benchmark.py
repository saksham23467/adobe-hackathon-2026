#!/usr/bin/env python3
"""Runtime + network efficiency profiler for the marketplace.

Target: a full audit of a typical website in **under 5 minutes**.

Runs the real audit entrypoint and measures, per run and in aggregate:

  * total runtime           (wall clock)
  * crawl time              (bounded BFS over the site)
  * rendering time          (render-extract stage)
  * external search time    (Wikidata corroboration)
  * number of requests      (network requests issued)
  * duplicate requests      (same URL fetched twice -> proves dedup)
  * pages analyzed          (shared cache size)
  * maximum concurrency     (peak in-flight fetches)
  * memory usage            (tracemalloc peak + RSS high-water)

plus a per-phase breakdown (context / detection / per-skill / report / validate)
so the 10 largest bottlenecks can be ranked from evidence, not guesswork.

Two modes:

  Hermetic (default): profiles every adversarial fixture. Deterministic, no
  network — isolates CPU/parsing cost and proves dedup + shared artifacts.

  Live:  `--url https://example.com`  profiles a real audit end-to-end,
  exposing network, politeness-delay, and external-search cost. Needs network.

Usage:
    python3 tests/benchmark/benchmark.py                       # all fixtures
    python3 tests/benchmark/benchmark.py --only ecommerce saas
    python3 tests/benchmark/benchmark.py --repeats 3           # stabilize timings
    python3 tests/benchmark/benchmark.py --url https://example.com
    python3 tests/benchmark/benchmark.py --json out.json
"""

from __future__ import annotations

import argparse
import gc
import importlib.util
import io
import json
import os
import resource
import sys
import time
import tracemalloc
from contextlib import redirect_stdout
from typing import Any, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_FIXTURES = os.path.join(_ROOT, "tests", "adversarial", "fixtures")
_SETTINGS = os.path.join(_ROOT, "config", "settings.json")
_RUN_AUDIT = os.path.join(_ROOT, "skills", "audit-orchestrator", "scripts", "run_audit.py")

_TARGET_S = 300.0  # < 5 minutes


def _load_run_audit():
    spec = importlib.util.spec_from_file_location("bench_run_audit", _RUN_AUDIT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bench_run_audit"] = mod
    spec.loader.exec_module(mod)
    return mod


def _rss_mb() -> float:
    # ru_maxrss is bytes on macOS, kilobytes on Linux.
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        return round(raw / (1024 * 1024), 1)
    return round(raw / 1024, 1)


def _profile_once(ra, settings: Dict[str, Any], *, fixture: str = "",
                  url: str = "") -> Dict[str, Any]:
    """Run one audit with phase timers + memory tracking."""
    gc.collect()
    tracemalloc.start()
    t_wall0 = time.perf_counter()

    t0 = time.perf_counter()
    if fixture:
        ctx = ra._build_context_fixture(fixture, settings)
    else:
        target = ra.validate_target(url)
        ctx = ra._build_context_live(target, settings)
    t_context = time.perf_counter() - t0

    t0 = time.perf_counter()
    with redirect_stdout(io.StringIO()):
        observations = ra.run_detection(ctx)
    t_detection = time.perf_counter() - t0

    t0 = time.perf_counter()
    with redirect_stdout(io.StringIO()):
        report = ra.build_report(ctx, observations)
    t_report = time.perf_counter() - t0

    t0 = time.perf_counter()
    errors = ra.validate_report(report)
    t_validate = time.perf_counter() - t0

    total_wall = time.perf_counter() - t_wall0
    cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    perf = (report.get("meta", {}) or {}).get("perf", {}) or {}
    stages = perf.get("stage_timings_s", {}) or {}
    # In hermetic mode the gate is bypassed by the FixtureFetcher, so the true
    # request count is the crawler's own tally.
    requests = perf.get("total_requests", 0) or perf.get("pages_requested", 0)

    return {
        "label": os.path.basename(fixture) if fixture else url,
        "mode": "fixture" if fixture else "live",
        "schema_valid": not errors,
        "phases_s": {
            "context_build": round(t_context, 4),
            "detection": round(t_detection, 4),
            "report": round(t_report, 4),
            "validate": round(t_validate, 4),
        },
        "stage_timings_s": stages,
        "crawl_time_s": perf.get("crawl_time_s", 0.0),
        "fetch_time_s": perf.get("fetch_time_s", 0.0),
        "render_time_s": round(stages.get("render-extract", 0.0), 4),
        "external_time_s": perf.get("external_time_s", 0.0),
        "total_runtime_s": round(total_wall, 4),
        "requests": requests,
        "duplicate_requests": perf.get("duplicate_requests", 0),
        "urls_deduped": perf.get("urls_deduped", 0),
        "pages_analyzed": perf.get("pages_analyzed", 0),
        "external_calls": perf.get("external_calls", 0),
        "max_concurrency": perf.get("max_concurrency", 1),
        "peak_memory_mb": round(peak / (1024 * 1024), 2),
        "rss_high_water_mb": _rss_mb(),
        "findings": len(report.get("findings", [])),
    }


def _discover(only: List[str]) -> List[str]:
    out = []
    for name in sorted(os.listdir(_FIXTURES)):
        if os.path.isfile(os.path.join(_FIXTURES, name, "pages.json")):
            if not only or name in only:
                out.append(name)
    return out


def _median(xs: List[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else round((s[n // 2 - 1] + s[n // 2]) / 2, 4)


def _bottlenecks(runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Rank the largest time/resource sinks across all runs, with evidence."""
    total_wall = sum(r["total_runtime_s"] for r in runs) or 1e-9
    comp: Dict[str, float] = {}

    def add(name: str, secs: float) -> None:
        comp[name] = comp.get(name, 0.0) + secs

    for r in runs:
        add("network:crawl_fetch", r["fetch_time_s"])
        add("network:external_search", r["external_time_s"])
        add("report:compile+reconcile+recommend+present", r["phases_s"]["report"])
        add("context:build+acquire", r["phases_s"]["context_build"])
        add("validate:schema", r["phases_s"]["validate"])
        for skill, secs in r["stage_timings_s"].items():
            add(f"skill:{skill}", secs)
    # Crawl-access stage time already includes fetch; subtract to avoid double count.
    if "skill:crawl-access" in comp:
        comp["skill:crawl-access"] = max(
            0.0, comp["skill:crawl-access"] - comp.get("network:crawl_fetch", 0.0)
        )

    ranked = sorted(comp.items(), key=lambda kv: kv[1], reverse=True)
    out = []
    for name, secs in ranked[:10]:
        out.append({
            "component": name,
            "total_s": round(secs, 4),
            "pct_of_wall": round(100.0 * secs / total_wall, 1),
        })
    return out


def run_benchmark(only: Optional[List[str]] = None, url: str = "",
                  repeats: int = 1) -> Dict[str, Any]:
    ra = _load_run_audit()
    with open(_SETTINGS, "r", encoding="utf-8") as fh:
        settings = json.load(fh)

    runs: List[Dict[str, Any]] = []
    if url:
        for _ in range(max(1, repeats)):
            runs.append(_profile_once(ra, settings, url=url))
    else:
        for name in _discover(only or []):
            fdir = os.path.join(_FIXTURES, name)
            samples = [_profile_once(ra, settings, fixture=fdir)
                       for _ in range(max(1, repeats))]
            # Report the median run for each fixture (timing is noisy at ms scale).
            samples.sort(key=lambda s: s["total_runtime_s"])
            runs.append(samples[len(samples) // 2])

    wall = [r["total_runtime_s"] for r in runs]
    aggregate = {
        "runs": len(runs),
        "target_s": _TARGET_S,
        "slowest_run_s": max(wall) if wall else 0.0,
        "slowest_run": max(runs, key=lambda r: r["total_runtime_s"])["label"] if runs else "",
        "median_run_s": _median(wall),
        "mean_run_s": round(sum(wall) / len(wall), 4) if wall else 0.0,
        "within_target": (max(wall) < _TARGET_S) if wall else True,
        "total_requests": sum(r["requests"] for r in runs),
        "total_duplicate_requests": sum(r["duplicate_requests"] for r in runs),
        "total_urls_deduped": sum(r["urls_deduped"] for r in runs),
        "total_external_calls": sum(r["external_calls"] for r in runs),
        "max_pages_analyzed": max((r["pages_analyzed"] for r in runs), default=0),
        "max_concurrency": max((r["max_concurrency"] for r in runs), default=1),
        "peak_memory_mb": max((r["peak_memory_mb"] for r in runs), default=0.0),
        "rss_high_water_mb": max((r["rss_high_water_mb"] for r in runs), default=0.0),
        "all_schema_valid": all(r["schema_valid"] for r in runs),
    }
    return {
        "target_seconds": _TARGET_S,
        "aggregate": aggregate,
        "bottlenecks": _bottlenecks(runs),
        "runs": runs,
    }


def render(report: Dict[str, Any]) -> None:
    a = report["aggregate"]
    print("\n" + "=" * 78)
    print("  MARKETPLACE RUNTIME + NETWORK BENCHMARK")
    print("=" * 78)
    ok = "PASS" if a["within_target"] else "FAIL"
    print(f"  target < {int(a['target_s'])}s   slowest run {a['slowest_run_s']}s "
          f"({a['slowest_run']})   [{ok}]")
    print(f"  runs={a['runs']}  median={a['median_run_s']}s  mean={a['mean_run_s']}s")
    print(f"  requests={a['total_requests']}  duplicates={a['total_duplicate_requests']}"
          f"  urls_deduped={a['total_urls_deduped']}  external_calls={a['total_external_calls']}")
    print(f"  max_pages_analyzed={a['max_pages_analyzed']}  max_concurrency={a['max_concurrency']}"
          f"  peak_mem={a['peak_memory_mb']}MB  rss_hwm={a['rss_high_water_mb']}MB")
    print(f"  schema_valid_all={a['all_schema_valid']}")
    print("-" * 78)
    print("  TOP BOTTLENECKS (summed across runs)")
    for i, b in enumerate(report["bottlenecks"], 1):
        print(f"  {i:>2}. {b['component']:<48} {b['total_s']:>8.4f}s  {b['pct_of_wall']:>5.1f}%")
    print("-" * 78)
    print("  SLOWEST RUNS")
    for r in sorted(report["runs"], key=lambda x: x["total_runtime_s"], reverse=True)[:8]:
        print(f"  {r['label']:<26} {r['total_runtime_s']:>7.4f}s  "
              f"crawl={r['crawl_time_s']:>6}s render={r['render_time_s']:>6}s "
              f"ext={r['external_time_s']:>5}s mem={r['peak_memory_mb']:>6}MB "
              f"reqs={r['requests']} pages={r['pages_analyzed']}")
    print("=" * 78 + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Marketplace runtime/network benchmark.")
    ap.add_argument("--only", nargs="*", default=[], help="limit to these fixtures")
    ap.add_argument("--url", default="", help="profile a live site end-to-end (needs network)")
    ap.add_argument("--repeats", type=int, default=1, help="samples per target (median reported)")
    ap.add_argument("--json", help="write the machine-readable report here")
    ap.add_argument("--fail-over-budget", action="store_true",
                    help="exit non-zero if any run exceeds the 5-minute target")
    args = ap.parse_args(argv)

    report = run_benchmark(only=args.only, url=args.url, repeats=args.repeats)
    if args.json:
        os.makedirs(os.path.dirname(os.path.abspath(args.json)), exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, ensure_ascii=False, sort_keys=True)
    render(report)
    if args.json:
        print(f"  machine-readable report: {args.json}\n")
    if args.fail_over_budget and not report["aggregate"]["within_target"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
