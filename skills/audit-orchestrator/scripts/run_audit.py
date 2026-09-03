#!/usr/bin/env python3
"""Entrypoint runner for the brand-ai-readiness-audit marketplace.

Composes the detection skills, compiles findings, attaches recommendations,
and emits one schema-valid audit report. Runs with only the Python standard
library and requires no LLM. Rendering is optional.

Usage:
    run_audit.py https://example.com
    run_audit.py --fixture tests/fixtures/clean-brand
    run_audit.py https://example.com --config config/settings.json --out report.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import time
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Dict, List, Optional

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_SCRIPTS_DIR, "..", "..", ".."))
_LIB = os.path.join(_ROOT, "lib")
for _p in (_SCRIPTS_DIR, _LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from audit_common.acquire import Cache, FixtureAcquirer  # noqa: E402
from audit_common.catalog import load_catalog  # noqa: E402
from audit_common.safety import SafetyConfig, SafetyGate, normalize_url  # noqa: E402
from audit_common.sampler import build_sample_plan  # noqa: E402
from audit_common.schema import Observation, new_report, validate_report  # noqa: E402

# The detection skills, in composition order. recommendation-engine is invoked
# separately because it transforms findings rather than producing observations.
_DETECTION_SKILLS = [
    "crawl-access",
    "render-extract",
    "fact-accessibility",
    "structured-identity",
    "engagement-orientation",
    "freshness-corroboration",
]


@dataclass
class AuditContext:
    site: str
    entry_url: str
    cache: Cache
    sample_plan: Any
    gate: SafetyGate
    config: Dict[str, Any]
    catalog: Any
    ai_agents: Dict[str, Any]
    robots_txt: str = ""
    fixture_dir: Optional[str] = None
    limitations: List[Dict[str, str]] = field(default_factory=list)
    stage_timings: Dict[str, float] = field(default_factory=dict)


def _load_json(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _load_skill_module(skill_id: str, filename: str = "detect.py") -> Optional[ModuleType]:
    path = os.path.join(_ROOT, "skills", skill_id, "scripts", filename)
    if not os.path.exists(path):
        return None
    spec = importlib.util.spec_from_file_location(
        f"skill_{skill_id.replace('-', '_')}_{filename[:-3]}", path
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclasses/typing in the loaded module resolve.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _build_context_live(site_url: str, settings: Dict[str, Any]) -> AuditContext:
    """Seed an empty cache. crawl-access is the sole live acquirer."""
    gate = SafetyGate(SafetyConfig.from_dict(settings))
    cache = Cache()
    plan = build_sample_plan(
        [site_url],
        k=int(settings.get("sample_k_per_class", 3)),
        max_total=int(settings.get("sample_max_total", 30)),
    )
    return AuditContext(
        site=_host(site_url),
        entry_url=site_url,
        cache=cache,
        sample_plan=plan,
        gate=gate,
        config=settings,
        catalog=load_catalog(),
        ai_agents=_load_json(os.path.join(_ROOT, "config", "ai_user_agents.json")),
        robots_txt="",
    )


def _build_context_fixture(fixture_dir: str, settings: Dict[str, Any]) -> AuditContext:
    gate = SafetyGate(SafetyConfig.from_dict(settings))
    cache = Cache()
    acq = FixtureAcquirer(fixture_dir)
    urls: List[str] = []
    for entry in acq.pages():
        rec = acq.fetch(entry)
        cache.put(rec)
        urls.append(rec.url)
    plan = build_sample_plan(
        urls,
        k=int(settings.get("sample_k_per_class", 3)),
        max_total=int(settings.get("sample_max_total", 30)),
    )
    site = acq.index.get("site", _host(urls[0]) if urls else "fixture")
    return AuditContext(
        site=site,
        entry_url=urls[0] if urls else "",
        cache=cache,
        sample_plan=plan,
        gate=gate,
        config=settings,
        catalog=load_catalog(),
        ai_agents=_load_json(os.path.join(_ROOT, "config", "ai_user_agents.json")),
        robots_txt=acq.robots_txt(),
        fixture_dir=fixture_dir,
    )


def _host(url: str) -> str:
    import urllib.parse

    return urllib.parse.urlsplit(url).netloc or url


def validate_target(url: str) -> str:
    """Validate and normalize an audit target. Raises ValueError on bad input."""
    import urllib.parse

    if not url or not str(url).strip():
        raise ValueError("no target provided")
    candidate = normalize_url(url.strip())
    parts = urllib.parse.urlsplit(candidate)
    if parts.scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {parts.scheme or '(none)'}")
    if not parts.netloc or "." not in parts.netloc:
        raise ValueError(f"invalid host: {parts.netloc or '(none)'}")
    return candidate


def _read_robots_text(url: str, gate: SafetyGate) -> str:
    import urllib.parse
    from audit_common.curlfetch import http_get

    parts = urllib.parse.urlsplit(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        gate.assert_readonly("GET")
        got = http_get(
            robots_url,
            user_agent=gate.config.user_agent,
            timeout_s=gate.config.request_timeout_s,
            max_bytes=200_000,
            max_redirects=3,
        )
        return got.body if (got.status and 200 <= got.status < 300) else ""
    except Exception:
        return ""


def run_detection(ctx: AuditContext) -> List[Observation]:
    observations: List[Observation] = []
    for skill_id in _DETECTION_SKILLS:
        if ctx.gate.budget_exhausted():
            ctx.limitations.append(
                {"check_id": skill_id, "reason": "time budget exhausted before skill ran"}
            )
            continue
        module = _load_skill_module(skill_id)
        if module is None or not hasattr(module, "detect"):
            ctx.limitations.append(
                {"check_id": skill_id, "reason": "skill detector not available"}
            )
            continue
        _t0 = time.monotonic()
        try:
            result = module.detect(ctx) or []
            observations.extend(result)
        except Exception as e:  # noqa: BLE001 -- one skill must not sink the audit
            ctx.limitations.append(
                {"check_id": skill_id, "reason": f"skill error: {type(e).__name__}"}
            )
        finally:
            ctx.stage_timings[skill_id] = round(
                ctx.stage_timings.get(skill_id, 0.0) + (time.monotonic() - _t0), 4
            )
    return observations


def build_report(ctx: AuditContext, observations: List[Observation]) -> Dict[str, Any]:
    from compile import compile_findings  # local import; same scripts dir
    from presentation import enrich_report  # local import; same scripts dir
    from reconcile import human_summary, reconcile  # local import; same scripts dir

    # 5. Collect normalized findings.
    findings, limitations = compile_findings(observations, ctx.catalog)

    # 6-9. Reconcile: merge overlaps, resolve contradictions, recompute
    #      confidence + severity. The orchestrator never blindly concatenates.
    recon_stats: Dict[str, Any] = {}
    try:
        findings, recon_stats = reconcile(findings, observations, ctx.catalog)
    except Exception as e:  # noqa: BLE001
        ctx.limitations.append(
            {"check_id": "reconciliation", "reason": f"error: {type(e).__name__}"}
        )

    # 10-11. Prioritized + proactive recommendations.
    rec = _load_skill_module("recommendation-engine", "recommend.py")
    beyond_defect: List[Dict[str, Any]] = []
    if rec is not None and hasattr(rec, "attach_actions"):
        try:
            findings, beyond_defect = rec.attach_actions(findings, ctx)
        except Exception as e:  # noqa: BLE001
            ctx.limitations.append(
                {"check_id": "recommendation-engine", "reason": f"error: {type(e).__name__}"}
            )

    crawl = getattr(ctx, "crawl_result", None)
    discovered = len(crawl.pages) if crawl is not None else len(ctx.cache)
    coverage = {
        "pages_discovered": discovered,
        "pages_sampled": len(ctx.cache),
        "page_classes": ctx.sample_plan.classes,
        "rendered": sum(1 for r in ctx.cache.all() if r.rendered_html),
        "external_lookups": ctx.gate.external_calls,
        "robots_respected": bool(getattr(ctx, "robots_respected", ctx.gate.robots_respected)),
    }
    crawl_stats = getattr(crawl, "stats", None) if crawl is not None else None
    perf = {
        "total_requests": ctx.gate.total_requests,
        "duplicate_requests": ctx.gate.duplicate_requests,
        "pages_requested": int(getattr(crawl_stats, "pages_requested", 0) or 0),
        "pages_crawled": int(getattr(crawl_stats, "pages_crawled", 0) or 0),
        "pages_analyzed": len(ctx.cache),
        "fetch_time_s": ctx.gate.fetch_time_s,
        "external_time_s": ctx.gate.external_time_s,
        "external_calls": ctx.gate.external_calls,
        "crawl_time_s": round(getattr(crawl_stats, "elapsed_s", 0.0) or 0.0, 3),
        "urls_deduped": int(getattr(crawl_stats, "urls_deduped", 0) or 0),
        "urls_skipped_nondocument": int(getattr(crawl_stats, "urls_skipped_nondocument", 0) or 0),
        "max_concurrency": int(getattr(crawl_stats, "max_concurrency", 1) or 1),
        "stage_timings_s": dict(ctx.stage_timings),
    }
    meta = {
        "runtime_s": round(ctx.gate.elapsed_s(), 3),
        "renderer": any(r.rendered_html for r in ctx.cache.all()),
        "catalog_version": ctx.catalog.version,
        "reconciliation": recon_stats,
        "robots_respected": coverage["robots_respected"],
        "perf": perf,
    }
    all_limitations = ctx.limitations + limitations
    report = new_report(
        site=ctx.site,
        findings=findings,
        beyond_defect=beyond_defect,
        coverage=coverage,
        limitations=all_limitations,
        meta=meta,
    )
    # 12. Make the report useful to a non-expert: headline + per-category counts.
    report["summary"].update(human_summary(findings))

    # 13. Presentation: scores, executive summary, observed/inferred/recommendation
    #     separation, "why this matters to AI systems", and reader sections. This
    #     is additive — the required Adobe schema is untouched.
    report = enrich_report(report)
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Brand AI-readiness audit (read-only).")
    parser.add_argument("site", nargs="?", help="website URL or domain")
    parser.add_argument("--fixture", help="path to a local fixture directory (hermetic)")
    parser.add_argument(
        "--config", default=os.path.join(_ROOT, "config", "settings.json")
    )
    parser.add_argument("--out", help="write report JSON to this path instead of stdout")
    args = parser.parse_args(argv)

    if not args.site and not args.fixture:
        parser.error("provide a site URL or --fixture PATH")

    settings = _load_json(args.config)

    started = time.monotonic()
    if args.fixture:
        ctx = _build_context_fixture(args.fixture, settings)
    else:
        try:
            target = validate_target(args.site)
        except ValueError as e:
            parser.error(f"invalid target: {e}")
        ctx = _build_context_live(target, settings)

    observations = run_detection(ctx)
    report = build_report(ctx, observations)

    errors = validate_report(report)
    if errors:
        # Emit diagnostics to stderr but still return the (invalid) report so
        # failures are visible in CI rather than silent.
        sys.stderr.write("REPORT VALIDATION ERRORS:\n" + "\n".join(errors) + "\n")

    report.setdefault("meta", {})["wall_clock_s"] = round(time.monotonic() - started, 3)
    payload = json.dumps(report, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload)
    else:
        print(payload)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
