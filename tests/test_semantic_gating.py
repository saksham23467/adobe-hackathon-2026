"""Regression tests for the resource/page semantic gating layer.

Covers the 12 fixture cases plus the classifier, link-role, fetch-outcome,
canonical-interpretation, and severity-cap rules. No hostname-specific logic.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
LIB = os.path.join(ROOT, "lib")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "semantic-gating")
for _p in (HERE, ROOT, LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from audit_common.acquire import FixtureAcquirer, PageRecord
from audit_common.schema import Confidence, Severity, Status
from audit_common.sampler import classify, is_locale_home_path
from audit_common.semantics import (
    apply_severity_gate,
    blocked_access_sentence,
    classify_fetch_outcome,
    classify_href,
    classify_page_type,
    classify_record,
    classify_resource,
    evaluate_canonical,
    in_scope,
    is_confirmed_broken,
    link_role,
    looks_like_html_partial,
    semantic_relevance,
    should_emit_finding,
    worth_crawling,
)


def _records():
    acq = FixtureAcquirer(FIXTURE)
    return [acq.fetch(e) for e in acq.pages()]


def _by_url():
    return {r.url: r for r in _records()}


# --------------------------------------------------------------------------
# 1–7. resource classification (generic, not host-specific)
# --------------------------------------------------------------------------
def test_classifier_svg_png_jpeg_video_pdf_js_css_api_html():
    cases = [
        ("https://x.example/logo.svg", "image/svg+xml", "<svg xmlns='http://www.w3.org/2000/svg'></svg>", "svg"),
        ("https://x.example/hero.jpg", "image/jpeg", "JPEG-PLACEHOLDER", "image"),
        ("https://x.example/hero.png", "image/png", "\x89PNG", "image"),
        ("https://x.example/demo.mp4", "video/mp4", "MP4", "video"),
        ("https://x.example/brochure.pdf", "application/pdf", "%PDF-1.1", "pdf"),
        ("https://x.example/app.js", "application/javascript", "console.log(1)", "javascript"),
        ("https://x.example/styles.css", "text/css", "body{}", "css"),
        ("https://x.example/api/v1/status", "application/json", "{}", "api_endpoint"),
        ("https://x.example/about", "text/html", "<!DOCTYPE html><html><body>Hi</body></html>", "html_document"),
    ]
    for url, ct, body, expect in cases:
        cls = classify_resource(url, content_type=ct, body=body)
        assert cls.resource_type == expect, (url, cls.resource_type, expect)


def test_svg_body_not_html_even_when_tagged_text_html():
    cls = classify_resource(
        "https://x.example/icons/mark.svg",
        content_type="text/html",
        body='<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"></svg>',
    )
    assert cls.resource_type == "svg"
    assert not cls.is_human_facing_page


def test_page_types_homepage_product_error_legal_landing():
    assert classify_resource("https://x.example/", content_type="text/html",
                             body="<!DOCTYPE html><html></html>").page_type == "homepage"
    assert classify_resource("https://x.example/products/caliper", content_type="text/html",
                             body="<!DOCTYPE html><html></html>").page_type == "product"
    assert classify_resource("https://x.example/missing", content_type="text/html",
                             body="<!DOCTYPE html><html></html>",
                             status_code=404).page_type == "error"
    assert classify_resource("https://x.example/legal", content_type="text/html",
                             body="<!DOCTYPE html><html></html>").page_type == "legal"
    assert classify_resource("https://x.example/campaign", content_type="text/html",
                             body="<!DOCTYPE html><html></html>").page_type == "landing_page"


def test_fixture_records_classify_all_twelve_shapes():
    recs = _by_url()
    expect = {
        "https://northwind.example/": "html_document",
        "https://northwind.example/logo.svg": "svg",
        "https://northwind.example/hero.jpg": "image",
        "https://northwind.example/demo.mp4": "video",
        "https://northwind.example/brochure.pdf": "pdf",
        "https://northwind.example/app.js": "javascript",
        "https://northwind.example/styles.css": "css",
        "https://northwind.example/api/v1/status": "api_endpoint",
        "https://northwind.example/campaign": "html_document",
        "https://northwind.example/gone": "html_document",
        "https://northwind.example/portal/secret": "html_document",
        "https://northwind.example/widget": "html_document",
    }
    for url, kind in expect.items():
        cls = classify_record(recs[url])
        assert cls.resource_type == kind, (url, cls.resource_type)
    assert classify_record(recs["https://northwind.example/gone"]).page_type == "error"
    assert classify_record(recs["https://northwind.example/campaign"]).page_type == "landing_page"


def test_dead_end_scope_excludes_assets():
    svg = classify_resource("https://x.example/mark.svg", content_type="image/svg+xml",
                            body="<svg xmlns='n'></svg>")
    assert not in_scope("engagement.dead_end_page", svg)
    assert semantic_relevance("engagement.dead_end_page", svg) == "none"
    html = classify_resource("https://x.example/campaign", content_type="text/html",
                             body="<!DOCTYPE html><html><h1>Sale</h1></html>")
    assert in_scope("engagement.dead_end_page", html)


# --------------------------------------------------------------------------
# Media links are not navigation
# --------------------------------------------------------------------------
def test_worth_crawling_skips_assets_keeps_html():
    assert worth_crawling("https://x.example/about")
    assert worth_crawling("https://x.example/page.html")
    assert worth_crawling("https://x.example/")
    assert not worth_crawling("https://x.example/logo.svg")
    assert not worth_crawling("https://x.example/demo.mp4")
    assert not worth_crawling("https://x.example/app.js")
    assert not worth_crawling("https://x.example/styles.css")
    assert not worth_crawling("https://x.example/hero.jpg")
    assert not worth_crawling("https://x.example/brochure.pdf")
    assert not worth_crawling("")


def test_html_partials_are_utility_full_docs_are_not():
    snippet = "<div class='card'><h2>Promo</h2><p>Included fragment.</p></div>"
    assert looks_like_html_partial(snippet)
    assert classify_page_type("https://x.example/cc-shared/fragments/promo", html=snippet) == "utility"
    assert classify_page_type("https://x.example/includes/header", html=snippet) == "utility"
    full = "<!DOCTYPE html><html><body><h1>Sale</h1></body></html>"
    assert not looks_like_html_partial(full)
    # Path shape, not hostname: /campaign stays a landing page so dead-ends still fire.
    assert classify_page_type("https://x.example/campaign", html=full) == "landing_page"


def test_locale_root_is_homepage():
    assert is_locale_home_path("/in")
    assert is_locale_home_path("https://x.example/us")
    assert not is_locale_home_path("/in-edu")
    assert classify("https://x.example/in") == "home"
    assert classify("https://x.example/in-edu") != "home"
    assert classify_page_type("https://x.example/de") == "homepage"
    # A body-rooted page without nav is still a document, not a fragment.
    assert classify_page_type(
        "https://x.example/about-us",
        html="<body><h1>About</h1><p>We make tools.</p></body>",
    ) == "unknown"


def test_link_role_media_vs_navigation():
    assert link_role("/logo.svg") == "media_asset"
    assert link_role("/demo.mp4") == "media_asset"
    assert link_role("/hero.jpg") == "media_asset"
    assert link_role("/app.js") == "media_asset"
    assert link_role("/styles.css") == "media_asset"
    assert link_role("/about") == "navigation"
    assert link_role("/brochure.pdf") == "download"
    assert link_role("/v/events/event.ics") == "download"


# --------------------------------------------------------------------------
# Fetch outcomes: 403 is not broken
# --------------------------------------------------------------------------
def test_fetch_outcomes_distinguish_403_from_404():
    assert classify_fetch_outcome(404) == "confirmed_404"
    assert classify_fetch_outcome(410) == "gone_410"
    assert classify_fetch_outcome(503) == "server_5xx"
    assert classify_fetch_outcome(403) == "blocked_403"
    assert classify_fetch_outcome(401) == "authentication_required"
    assert classify_fetch_outcome(0, error="timeout") == "transient"
    assert classify_fetch_outcome(0, robots_blocked=True) == "robots_restricted"
    assert is_confirmed_broken("confirmed_404")
    assert is_confirmed_broken("server_5xx")
    assert not is_confirmed_broken("blocked_403")
    sentence = blocked_access_sentence("https://stock.example/", 403, "blocked_403")
    assert "403" in sentence
    assert "could not be confirmed" in sentence
    assert "broken" not in sentence.lower() or "not" in sentence.lower()


# --------------------------------------------------------------------------
# Canonical: same-reg subdomain is review; other domain is conflict
# --------------------------------------------------------------------------
def test_canonical_cross_subdomain_is_review_not_auto_defect():
    ev = evaluate_canonical(
        "https://www.example.com/widget",
        "https://shop.example.com/widget",
    )
    assert ev["relation"] == "cross_subdomain"
    assert ev["verdict"] == "review"
    assert ev["relevance"] == "low"


def test_canonical_cross_origin_is_conflict():
    ev = evaluate_canonical(
        "https://brand.example/about",
        "https://cdn.othernetwork.example/about",
    )
    assert ev["relation"] == "cross_origin"
    assert ev["verdict"] == "conflict"


def test_canonical_same_host_is_ok():
    ev = evaluate_canonical("https://s.example/a", "https://s.example/a")
    assert ev["verdict"] == "ok"


# --------------------------------------------------------------------------
# Severity gate
# --------------------------------------------------------------------------
def test_low_relevance_cannot_be_high_severity():
    sev, conf = apply_severity_gate(Severity.HIGH, Confidence.HIGH, "low")
    assert sev == Severity.MEDIUM
    assert conf.rank <= Confidence.MEDIUM.rank
    assert not should_emit_finding("none")
    assert should_emit_finding("low")


# --------------------------------------------------------------------------
# Engagement detectors on the fixture
# --------------------------------------------------------------------------
def test_fragment_pages_are_not_dead_ends():
    import _engagement_paths as P  # noqa: F401
    import engagedetect as E

    home = PageRecord(
        url="https://x.example/", page_class="home", fetched=True,
        raw_html="<!DOCTYPE html><html><nav><a href='/'>Home</a></nav><h1>Home</h1></html>",
        rendered_html="<!DOCTYPE html><html><nav><a href='/'>Home</a></nav><h1>Home</h1></html>",
        headers={"content-type": "text/html"},
    )
    fragment = PageRecord(
        url="https://x.example/cc-shared/fragments/promo", page_class="other", fetched=True,
        raw_html="<div class='card'><h2>Promo</h2><p>Included fragment.</p></div>",
        rendered_html="<div class='card'><h2>Promo</h2><p>Included fragment.</p></div>",
        headers={"content-type": "text/html"},
    )
    campaign = PageRecord(
        url="https://x.example/campaign", page_class="other", fetched=True,
        raw_html="<!DOCTYPE html><html><h1>Sale</h1></html>",
        rendered_html="<!DOCTYPE html><html><h1>Sale</h1></html>",
        headers={"content-type": "text/html"},
    )
    fails = [o for o in E.run_all([home, fragment, campaign]) if o.status == Status.FAIL
             and o.check_id == "engagement.dead_end_page"]
    urls = set()
    for o in fails:
        urls.update(o.urls)
    assert any("/campaign" in u for u in urls)
    assert not any("fragments" in u for u in urls)


def test_assets_are_not_dead_end_pages_but_landing_html_is():
    import _engagement_paths as P  # noqa: F401
    import engagedetect as E

    recs = _records()
    fails = [o for o in E.run_all(recs) if o.status == Status.FAIL]
    dead = [o for o in fails if o.check_id == "engagement.dead_end_page"]
    assert dead, "genuine HTML landing page /campaign must still be a dead-end"
    urls = set()
    for o in dead:
        urls.update(o.urls)
    assert any("/campaign" in u for u in urls)
    for bad in (".svg", ".jpg", ".mp4", ".js", ".css", ".pdf", "/api/"):
        assert not any(bad in u for u in urls), bad


def test_empty_media_links_are_not_unclear_navigation():
    import _engagement_paths as P  # noqa: F401
    import engagedetect as E

    recs = _records()
    fails = [o for o in E.run_all(recs) if o.status == Status.FAIL
             and o.check_id == "engagement.unclear_navigation"]
    examples = []
    for o in fails:
        examples.extend((o.metric or {}).get("examples") or [])
    blob = " ".join(str(x) for x in examples)
    assert ".svg" not in blob
    assert ".mp4" not in blob
    assert ".jpg" not in blob


def test_inline_svg_page_record_is_not_a_dead_end():
    import _engagement_paths as P  # noqa: F401
    import engagedetect as E
    import journey as J

    home = PageRecord(
        url="https://x.example/", page_class="home", fetched=True,
        raw_html="<nav><a href='/mark.svg'>Logo</a></nav><h1>Home</h1>",
        rendered_html="<nav><a href='/mark.svg'>Logo</a></nav><h1>Home</h1>",
        headers={"content-type": "text/html"},
    )
    svg = PageRecord(
        url="https://x.example/mark.svg", page_class="other", fetched=True,
        raw_html='<svg xmlns="http://www.w3.org/2000/svg"><circle/></svg>',
        rendered_html='<svg xmlns="http://www.w3.org/2000/svg"><circle/></svg>',
        headers={"content-type": "image/svg+xml"},
    )
    model = J.build_model([home, svg])
    assert "https://x.example/mark.svg" not in model.pages
    ids = {o.check_id for o in E.run_all([home, svg]) if o.status == Status.FAIL}
    assert "engagement.dead_end_page" not in ids


# --------------------------------------------------------------------------
# Crawl detectors
# --------------------------------------------------------------------------
def _crawl_mods():
    sys.path.insert(0, os.path.join(ROOT, "skills", "crawl-access", "scripts"))
    import detectors
    import urlnorm
    from crawl_result import CrawlPage, CrawlResult
    return detectors, urlnorm, CrawlPage, CrawlResult


def _page(CrawlPage, urlnorm, url, **kw):
    p = CrawlPage(url=urlnorm.normalize_url(url))
    p.fetched = kw.pop("fetched", True)
    p.is_html = kw.pop("is_html", True)
    p.status_code = kw.pop("status_code", 200)
    p.ok = 200 <= p.status_code < 300
    p.content_type = kw.pop("content_type", "text/html")
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def test_media_only_404s_are_not_broken_pages():
    detectors, urlnorm, CrawlPage, CrawlResult = _crawl_mods()
    home = _page(CrawlPage, urlnorm, "https://s.com/",
                 internal_links=["https://s.com/clip.mp4"], body="<html></html>")
    media = _page(CrawlPage, urlnorm, "https://s.com/clip.mp4", status_code=404,
                  is_html=False, content_type="video/mp4", body="nope")
    r = CrawlResult(site_host="s.com", entry_url="https://s.com/")
    r.pages[home.url] = home
    r.pages[media.url] = media
    findings = detectors.detect_broken_internal_links(r)
    assert findings == []


def test_html_404_stays_high_relevance_broken():
    detectors, urlnorm, CrawlPage, CrawlResult = _crawl_mods()
    home = _page(CrawlPage, urlnorm, "https://s.com/",
                 internal_links=["https://s.com/gone"], body="<html></html>")
    dead = _page(CrawlPage, urlnorm, "https://s.com/gone", status_code=404,
                 body="<!DOCTYPE html><html>missing</html>")
    r = CrawlResult(site_host="s.com", entry_url="https://s.com/")
    r.pages[home.url] = home
    r.pages[dead.url] = dead
    findings = detectors.detect_broken_internal_links(r)
    assert findings[0].semantic_relevance == "high"
    assert findings[0].metric.get("html_targets") == 1
    assert "https://s.com/gone" in findings[0].urls


def test_403_is_inconclusive_not_broken():
    detectors, urlnorm, CrawlPage, CrawlResult = _crawl_mods()
    home = _page(CrawlPage, urlnorm, "https://s.com/",
                 internal_links=["https://s.com/secret", "https://s.com/dead"])
    secret = _page(CrawlPage, urlnorm, "https://s.com/secret", status_code=403)
    dead = _page(CrawlPage, urlnorm, "https://s.com/dead", status_code=404)
    r = CrawlResult(site_host="s.com", entry_url="https://s.com/")
    for p in (home, secret, dead):
        r.pages[p.url] = p
    findings = detectors.detect_broken_internal_links(r)
    by = {f.tier.value: f for f in findings}
    assert "confirmed" in by
    assert "https://s.com/dead" in by["confirmed"].urls
    assert "https://s.com/secret" not in by["confirmed"].urls
    assert "inconclusive" in by
    assert "403" in by["inconclusive"].evidence
    assert "could not be confirmed" in by["inconclusive"].evidence
    obs = by["inconclusive"].to_observation()
    assert obs.status == Status.INCONCLUSIVE


def test_cross_subdomain_canonical_is_review():
    detectors, urlnorm, CrawlPage, CrawlResult = _crawl_mods()
    page = _page(CrawlPage, urlnorm, "https://www.s.com/widget",
                 canonical="https://shop.s.com/widget")
    r = CrawlResult(site_host="www.s.com", entry_url="https://www.s.com/")
    r.pages[page.url] = page
    findings = detectors.detect_canonical(r)
    assert findings
    f = findings[0]
    assert f.check_id == "access.canonical_inconsistent"
    assert f.semantic_relevance == "low"
    assert f.evidence_tier == "review"
    assert "same registrable" in f.evidence or "sibling" in f.evidence


def test_cross_origin_canonical_still_inconsistent():
    detectors, urlnorm, CrawlPage, CrawlResult = _crawl_mods()
    page = _page(CrawlPage, urlnorm, "https://s.com/a",
                 canonical="https://other.com/a")
    r = CrawlResult(site_host="s.com", entry_url="https://s.com/")
    r.pages[page.url] = page
    findings = detectors.detect_canonical(r)
    assert findings[0].check_id == "access.canonical_inconsistent"
    assert findings[0].metric.get("relation") == "cross_origin"
    assert findings[0].semantic_relevance != "low"


# --------------------------------------------------------------------------
# Full pipeline on the fixture
# --------------------------------------------------------------------------
def test_pipeline_keeps_real_defects_and_drops_asset_false_positives():
    scripts = os.path.join(ROOT, "skills", "audit-orchestrator", "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    path = os.path.join(scripts, "run_audit.py")
    spec = importlib.util.spec_from_file_location("run_audit_semantic", path)
    runner = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = runner
    spec.loader.exec_module(runner)

    settings = json.load(open(os.path.join(ROOT, "config", "settings.json"), encoding="utf-8"))
    ctx = runner._build_context_fixture(FIXTURE, settings)
    observations = runner.run_detection(ctx)
    report = runner.build_report(ctx, observations)
    errors = runner.validate_report(report)
    assert not errors, errors

    findings = report.get("findings") or []
    ids = {f["check_id"] for f in findings}
    assert "engagement.dead_end_page" in ids
    assert "access.broken_internal_links" in ids

    for f in findings:
        urls = " ".join(f.get("affected_urls") or [])
        ev = f.get("evidence") or ""
        if isinstance(ev, dict):
            ev = ev.get("observed_fact") or ev.get("sentence") or ""
        ev = str(ev)
        blob = urls + " " + ev
        if f["check_id"] == "engagement.dead_end_page":
            assert "/campaign" in blob
            assert ".svg" not in blob
            assert ".mp4" not in blob
        if f["check_id"] == "access.broken_internal_links":
            assert "/gone" in blob or "/missing" in blob
            assert "/portal/secret" not in blob
            assert "403" not in (f.get("title") or "")

    limitations = report.get("limitations") or []
    lim_text = " ".join(str(x) for x in limitations)
    assert "403" in lim_text
    assert "could not be confirmed" in lim_text
