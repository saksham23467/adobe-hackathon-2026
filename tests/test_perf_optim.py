"""Tests for the runtime/network optimizations.

Guards that the performance work is correct and — crucially — accuracy-neutral:
  * bounded concurrency produces byte-identical crawl output to sequential;
  * the per-host rate limiter still spaces dispatch under concurrency;
  * render-only-where-necessary selects shells and honors max_render;
  * shared visible-text artifact matches the canonical stripper;
  * external-search "useful" gate never skips a case that could find a defect;
  * the audit report carries perf telemetry.
"""

import os
import time

import _crawl_paths as P  # noqa: F401  (sets sys.path)
from crawler import Crawler, CrawlLimits, DictFetcher


# --------------------------------------------------------------------------
# Bounded concurrency must not change results (determinism + accuracy)
# --------------------------------------------------------------------------
def _big_site(n: int = 12) -> DictFetcher:
    mapping = {
        "https://s.com/robots.txt": {"status": 200, "body": "User-agent: *\nAllow: /\n",
                                     "content_type": "text/plain"},
    }
    # Home links to n section pages; each section links to the next (a small graph).
    home_links = " ".join(f"<a href='/p{i}'>{i}</a>" for i in range(n))
    mapping["https://s.com/"] = {
        "status": 200, "content_type": "text/html",
        "body": f"<html><body>{home_links}</body></html>",
    }
    for i in range(n):
        nxt = f"<a href='/p{(i + 1) % n}'>next</a> <a href='/'>home</a>"
        mapping[f"https://s.com/p{i}"] = {
            "status": 200, "content_type": "text/html",
            "body": f"<html><body>page {i} {nxt}</body></html>",
        }
    return DictFetcher(mapping)


def _snapshot(result):
    return {
        url: {
            "depth": p.depth, "status": p.status_code, "fetched": p.fetched,
            "ok": p.ok, "canonical": p.canonical, "in_sitemap": p.in_sitemap,
            "internal_links": sorted(p.internal_links),
            "inbound_internal": p.inbound_internal,
        }
        for url, p in result.pages.items()
    }


def test_concurrent_crawl_matches_sequential():
    seq = Crawler(_big_site(), "https://s.com/",
                  limits=CrawlLimits(max_pages=20, max_workers=1)).run()
    con = Crawler(_big_site(), "https://s.com/",
                  limits=CrawlLimits(max_pages=20, max_workers=4)).run()
    assert _snapshot(seq) == _snapshot(con), "concurrency changed crawl output"
    assert con.stats.pages_crawled == seq.stats.pages_crawled
    assert con.stats.max_concurrency >= 2  # concurrency actually engaged


def test_concurrent_respects_page_cap():
    con = Crawler(_big_site(20), "https://s.com/",
                  limits=CrawlLimits(max_pages=5, max_workers=8)).run()
    assert con.stats.pages_crawled <= 5
    assert con.stats.page_cap_hit is True


# --------------------------------------------------------------------------
# Rate limiter: dispatch spacing preserved (politeness) and thread-safe
# --------------------------------------------------------------------------
def test_rate_limiter_spaces_dispatch_under_threads():
    from audit_common.safety import RateLimiter
    import threading

    rl = RateLimiter(0.05)
    stamps = []
    lock = threading.Lock()

    def worker():
        rl.wait("h")
        with lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stamps.sort()
    gaps = [stamps[i + 1] - stamps[i] for i in range(len(stamps) - 1)]
    # Each consecutive dispatch to the same host is spaced ~>= min_delay.
    assert all(g >= 0.03 for g in gaps), gaps


# --------------------------------------------------------------------------
# Render-only-where-necessary
# --------------------------------------------------------------------------
class _Rec:
    def __init__(self, url, raw_html="", raw_text="", rendered_html="", page_class="other"):
        self.url = url
        self.raw_html = raw_html
        self.raw_text = raw_text
        self.rendered_html = rendered_html
        self.rendered_text = ""
        self.page_class = page_class


def test_render_policy_selects_shells_only_and_caps():
    from audit_common.renderpolicy import needs_render, select_for_render

    shell = _Rec("https://x/app", raw_html="<html><body><div id=root></div>"
                 "<script src=a.js></script></body></html>", raw_text="", page_class="home")
    rich = _Rec("https://x/about", raw_html="<html><body>" + ("word " * 200) + "</body></html>",
                raw_text="word " * 200, page_class="about")
    already = _Rec("https://x/r", raw_html="<script></script>", raw_text="",
                   rendered_html="<html>done</html>")
    assert needs_render(shell) is True
    assert needs_render(rich) is False          # substantial static text -> skip
    assert needs_render(already) is False        # already rendered -> skip

    many = [_Rec(f"https://x/s{i}", raw_html="<script></script>", raw_text="", page_class="home")
            for i in range(10)]
    picked = select_for_render(many, max_render=3)
    assert len(picked) == 3                       # honors the cap
    assert select_for_render(many, max_render=0) == []


# --------------------------------------------------------------------------
# Shared visible-text artifact == canonical stripper (accuracy-neutral)
# --------------------------------------------------------------------------
def test_shared_strip_matches_acquire_and_prefers_precomputed():
    from audit_common.artifacts import shared_strip, visible_text
    from audit_common.acquire import _strip_html

    html = "<html><head><style>x{}</style></head><body><h1>Hi</h1>" \
           "<script>var a=1;</script><p>Body  text</p></body></html>"
    assert shared_strip(html) == _strip_html(html)

    rec = _Rec("https://x/", raw_html=html, raw_text="PRECOMPUTED")
    assert visible_text(rec) == "PRECOMPUTED"     # reuses the once-computed text
    rec2 = _Rec("https://x/2", raw_html=html, raw_text="")
    assert visible_text(rec2) == shared_strip(html)  # falls back and matches


# --------------------------------------------------------------------------
# External-search "useful" gate never skips a defect-capable case
# --------------------------------------------------------------------------
def test_worth_external_gate():
    import importlib.util

    fd_path = os.path.join(P.ROOT, "skills", "freshness-corroboration", "scripts", "freshdetect.py")
    claims_dir = os.path.dirname(fd_path)
    import sys
    if claims_dir not in sys.path:
        sys.path.insert(0, claims_dir)
    spec = importlib.util.spec_from_file_location("freshdetect_perf", fd_path)
    fd = importlib.util.module_from_spec(spec)
    sys.modules["freshdetect_perf"] = fd
    spec.loader.exec_module(fd)
    import claims as C

    # No brand -> nothing to resolve -> skip.
    assert fd._worth_external(C.SiteClaims(brand_name="")) is False
    # Unresolved brand (no sameAs) -> ambiguity possible -> MUST run.
    assert fd._worth_external(C.SiteClaims(brand_name="Apollo")) is True
    # sameAs + a dated claim -> conflict possible -> MUST run.
    sc = C.SiteClaims(brand_name="Apollo", has_sameas=True)
    sc.founding_years.add("2001")
    assert fd._worth_external(sc) is True
    # Fully self-disambiguated + nothing to cross-check -> safe to skip.
    assert fd._worth_external(C.SiteClaims(brand_name="Apollo", has_sameas=True)) is False


# --------------------------------------------------------------------------
# Report carries perf telemetry
# --------------------------------------------------------------------------
def test_report_has_perf_block():
    import importlib.util

    ra_path = os.path.join(P.ROOT, "skills", "audit-orchestrator", "scripts", "run_audit.py")
    spec = importlib.util.spec_from_file_location("run_audit_perf", ra_path)
    ra = importlib.util.module_from_spec(spec)
    import sys
    sys.modules["run_audit_perf"] = ra
    spec.loader.exec_module(ra)

    import json
    with open(os.path.join(P.ROOT, "config", "settings.json"), encoding="utf-8") as fh:
        settings = json.load(fh)
    fdir = os.path.join(P.ROOT, "tests", "adversarial", "fixtures", "ecommerce")
    ctx = ra._build_context_fixture(fdir, settings)
    obs = ra.run_detection(ctx)
    report = ra.build_report(ctx, obs)
    perf = report["meta"]["perf"]
    for key in ("total_requests", "duplicate_requests", "pages_analyzed",
                "crawl_time_s", "urls_deduped", "urls_skipped_nondocument",
                "max_concurrency", "stage_timings_s"):
        assert key in perf
    assert perf["duplicate_requests"] == 0        # dedup: no URL fetched twice
    assert set(perf["stage_timings_s"]) == {
        "crawl-access", "render-extract", "fact-accessibility",
        "structured-identity", "engagement-orientation", "freshness-corroboration",
    }


# --------------------------------------------------------------------------
# The benchmark script itself runs and stays under budget (hermetic)
# --------------------------------------------------------------------------
def test_benchmark_runs_and_is_under_budget():
    import importlib.util

    bpath = os.path.join(P.ROOT, "tests", "benchmark", "benchmark.py")
    spec = importlib.util.spec_from_file_location("benchmark_selftest", bpath)
    bench = importlib.util.module_from_spec(spec)
    import sys
    sys.modules["benchmark_selftest"] = bench
    spec.loader.exec_module(bench)

    report = bench.run_benchmark(only=["perfect-static", "ecommerce", "saas"])
    agg = report["aggregate"]
    assert agg["runs"] == 3
    assert agg["within_target"] is True
    assert agg["all_schema_valid"] is True
    assert agg["total_duplicate_requests"] == 0
    assert len(report["bottlenecks"]) >= 1
    for b in report["bottlenecks"]:
        assert set(b) == {"component", "total_s", "pct_of_wall"}
