"""crawl-access detector entrypoint.

Runs the bounded, robots-respecting crawler and the tiered detectors, then
returns normalized Observations for the orchestrator. Chooses a fetcher based on
the audit context:

  * ctx.fixture_dir set  -> FixtureFetcher (hermetic)
  * live gate + entry_url -> LiveFetcher (network, read-only)
  * otherwise            -> serve from ctx.cache

Detection is pure and deterministic; the crawler enforces all safety limits.
"""

from __future__ import annotations

import os
import sys
from typing import List

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_LIB = os.path.join(_ROOT, "lib")
for _p in (_HERE, _LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import detectors  # noqa: E402
from crawler import Crawler, CrawlLimits, DictFetcher, FixtureFetcher, LiveFetcher  # noqa: E402
from audit_common.acquire import PageRecord, extract_embedded_state, _strip_html  # noqa: E402
from audit_common.sampler import build_sample_plan, classify  # noqa: E402
from audit_common.schema import Observation  # noqa: E402


def _limits_from_ctx(ctx) -> CrawlLimits:
    cfg = getattr(ctx, "config", {}) or {}
    crawl_cfg = cfg.get("crawl", {}) if isinstance(cfg, dict) else {}
    # Leave headroom under the global budget for the rest of the pipeline.
    deadline = 120.0
    gate = getattr(ctx, "gate", None)
    stages = cfg.get("stage_budgets_s", {}) if isinstance(cfg, dict) else {}
    if stages:
        deadline = min(deadline, float(stages.get("discovery", deadline)))
    if gate is not None:
        try:
            # Reserve time for downstream skills.
            deadline = min(deadline, max(15.0, gate.time_left_s() * 0.45))
        except Exception:
            pass
    # Concurrency helps live crawls (network latency overlaps) but is pointless
    # for the in-memory FixtureFetcher, so keep fixtures strictly sequential for
    # byte-for-byte deterministic test output.
    is_fixture = bool(getattr(ctx, "fixture_dir", None))
    max_workers = 1 if is_fixture else int(
        crawl_cfg.get("max_workers", cfg.get("max_concurrency", 1)) or 1
    )
    return CrawlLimits(
        max_pages=int(crawl_cfg.get("max_pages", cfg.get("max_pages", 40))),
        max_depth=int(crawl_cfg.get("max_depth", 5)),
        max_redirects=int(crawl_cfg.get("max_redirects", 5)),
        deadline_s=float(crawl_cfg.get("deadline_s", deadline)),
        sitemap_fetch_cap=int(crawl_cfg.get("sitemap_fetch_cap", 5)),
        max_workers=max(1, max_workers),
    )


def _build_fetcher(ctx):
    fixture_dir = getattr(ctx, "fixture_dir", None)
    if fixture_dir:
        return FixtureFetcher(fixture_dir)
    gate = getattr(ctx, "gate", None)
    entry = getattr(ctx, "entry_url", "")
    if gate is not None and entry.startswith(("http://", "https://")):
        max_redirects = 5
        cfg = getattr(ctx, "config", {}) or {}
        if isinstance(cfg, dict):
            max_redirects = int(cfg.get("crawl", {}).get("max_redirects", 5))
        return LiveFetcher(gate, max_redirects=max_redirects)
    # Fallback: serve whatever is already cached (degraded crawl).
    mapping = {}
    cache = getattr(ctx, "cache", None)
    if cache is not None:
        for rec in cache.all():
            mapping[rec.url] = {
                "status": rec.status_code or 200,
                "body": rec.rendered_html or rec.raw_html,
                "headers": rec.headers,
                "content_type": rec.headers.get("content-type", "text/html"),
                "final_url": rec.final_url or rec.url,
            }
    return DictFetcher(mapping)


def run_crawl(ctx):
    """Run the crawler and return the CrawlResult (exposed for reuse/tests)."""
    fetcher = _build_fetcher(ctx)
    entry = getattr(ctx, "entry_url", "") or (getattr(ctx, "site", "") or "")
    ua = "brand-ai-readiness-audit"
    gate = getattr(ctx, "gate", None)
    if gate is not None:
        ua = gate.config.user_agent
    respect = True
    if gate is not None:
        respect = bool(gate.config.respect_robots)
    crawler = Crawler(
        fetcher, entry_url=entry, limits=_limits_from_ctx(ctx),
        user_agent=ua, respect_robots=respect,
    )
    return crawler.run()


def _hydrate_from_crawl(ctx, result) -> None:
    """Put crawled HTML into the shared cache and rebuild the sample plan.

    Fixture runs already have a fully populated cache (including rendered
    snapshots); we only rebuild the sample plan. Live runs replace the
    homepage-only seed list with every successfully fetched HTML page.
    """
    cache = getattr(ctx, "cache", None)
    if cache is None:
        return
    fixture = getattr(ctx, "fixture_dir", None)
    if not fixture:
        for rec in list(cache.all()):
            cache._records.pop(rec.url, None)
        for page in result.pages.values():
            if not page.fetched or not page.ok or not page.body:
                continue
            page_type = ""
            try:
                from audit_common.semantics import classify_resource
                _cls = classify_resource(
                    page.url, content_type=page.content_type, body=page.body,
                    status_code=page.status_code, headers=page.headers,
                )
                is_html = _cls.resource_type == "html_document"
                page_type = _cls.page_type
            except Exception:
                is_html = page.is_html or "html" in (page.content_type or "").lower()
            if not is_html:
                continue
            extra = extract_embedded_state(page.body)
            rendered = page.body
            rendered_text = _strip_html(page.body)
            if extra:
                rendered = (
                    page.body
                    + "<div id='audit-embedded-state'>"
                    + extra.replace("<", " ")
                    + "</div>"
                )
                rendered_text = (rendered_text + " " + extra).strip()
            rec = PageRecord(
                url=page.url,
                page_class=classify(page.url),
                status_code=page.status_code,
                final_url=page.final_url or page.url,
                headers=page.headers,
                raw_html=page.body,
                raw_text=_strip_html(page.body),
                rendered_html=rendered if extra else "",
                rendered_text=rendered_text if extra else "",
                fetched=True,
                resource_type="html_document",
                page_type=page_type,
            )
            cache.put(rec)

    urls = [r.url for r in cache.all()]
    if not urls:
        urls = [p.url for p in result.pages.values() if p.fetched]
    cfg = getattr(ctx, "config", {}) or {}
    try:
        ctx.sample_plan = build_sample_plan(
            urls,
            k=int(cfg.get("sample_k_per_class", 3)),
            max_total=int(cfg.get("sample_max_total", 30)),
        )
    except Exception:
        pass

    gate = getattr(ctx, "gate", None)
    if gate is not None and result.robots:
        status = "ok" if result.robots.available else (
            "unreachable" if not result.robots.reachable else "missing"
        )
        gate.cache_robots(getattr(ctx, "entry_url", result.entry_url), result.robots.raw, status)
        try:
            ctx.robots_txt = result.robots.raw or ctx.robots_txt
        except Exception:
            pass
    try:
        ctx.robots_respected = bool(result.robots.reachable and (
            gate.config.respect_robots if gate is not None else True
        ))
    except Exception:
        pass


def detect(ctx) -> List[Observation]:
    result = run_crawl(ctx)
    try:
        setattr(ctx, "crawl_result", result)
    except Exception:
        pass
    _hydrate_from_crawl(ctx, result)
    findings = detectors.run_all(result, ctx)
    return detectors.to_observations(findings)
