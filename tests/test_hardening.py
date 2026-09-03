"""Regression tests for the adversarial-review fixes."""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ORCH = os.path.join(ROOT, "skills", "audit-orchestrator", "scripts")
CRAWL = os.path.join(ROOT, "skills", "crawl-access", "scripts")
IDENT = os.path.join(ROOT, "skills", "structured-identity", "scripts")
LIB = os.path.join(ROOT, "lib")
for p in (ORCH, CRAWL, IDENT, LIB):
    if p not in sys.path:
        sys.path.insert(0, p)

import crawler
import detectors
import entitygraph as eg
import presentation
import urlnorm
from audit_common.dom import attrs_hidden, name_mentioned
from audit_common.net import is_public_http_url, looks_like_xml, registrable_domain
from audit_common.safety import SafetyConfig, SafetyGate
from crawl_result import RobotsInfo, SitemapInfo


def test_private_hosts_are_blocked():
    assert not is_public_http_url("http://127.0.0.1/")
    assert not is_public_http_url("http://169.254.169.254/latest/meta-data/")
    assert not is_public_http_url("http://localhost/admin")
    assert is_public_http_url("https://example.com/page")


def test_html_soft_404_is_not_a_sitemap():
    resp = crawler.RawResponse(
        requested_url="https://s.com/sitemap.xml",
        status=200,
        body="<html><title>Not found</title></html>",
        content_type="text/html",
    )
    info = crawler.parse_sitemap("https://s.com/sitemap.xml", resp)
    assert info.kind == "not_xml"
    assert not info.available
    r = type("R", (), {"robots": RobotsInfo(), "sitemaps": [info]})()
    assert detectors.detect_sitemap_validity(r) == []


def test_xml_sitemap_still_parses():
    body = '<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' \
           "<url><loc>https://s.com/</loc></url></urlset>"
    resp = crawler.RawResponse(requested_url="u", status=200, body=body, content_type="application/xml")
    info = crawler.parse_sitemap("https://s.com/sitemap.xml", resp)
    assert info.available and info.well_formed and info.urls


def test_robots_5xx_fail_closed():
    mapping = {
        "https://s.com/robots.txt": {"status": 503, "body": ""},
        "https://s.com/": {"status": 200, "body": "<html><a href='/secret'>x</a></html>",
                           "content_type": "text/html"},
        "https://s.com/secret": {"status": 200, "body": "<html>nope</html>", "content_type": "text/html"},
    }
    c = crawler.Crawler(crawler.DictFetcher(mapping), "https://s.com/", respect_robots=True)
    result = c.run()
    assert not result.robots.reachable
    fetched = [p for p in result.pages.values() if p.fetched and p.ok]
    assert fetched == []


def test_session_and_pagination_not_enqueued():
    assert urlnorm.has_session_param("https://s.com/?sid=abc")
    from audit_common.net import trap_query
    assert trap_query("https://s.com/x?page=2") == "pagination"


def test_shop_subdomain_is_same_registrable():
    assert registrable_domain("shop.brand.com") == registrable_domain("brand.com")


def test_hotel_type_is_not_inappropriate():
    html = '<script type="application/ld+json">{"@type":"Hotel","name":"Harbor Inn"}</script>'
    from identitydetect import detect_type_inappropriate
    g = eg.build_graph([type("R", (), {"url": "https://s/", "raw_html": html, "rendered_html": html})()])
    assert detect_type_inappropriate(g) == []


def test_name_token_not_substring():
    assert name_mentioned("Atlas Wrench", "Buy the Atlas Wrench today")
    assert not name_mentioned("Atlas", "Atlantis resort packages")


def test_hidden_class_detected():
    assert attrs_hidden({"class": "sr-only hidden"})
    assert not attrs_hidden({"class": "card"})


def test_ai_ua_blocked_on_content_path():
    raw = "User-agent: GPTBot\nDisallow: /products/\nUser-agent: *\nAllow: /\n"
    blocked = detectors.compute_ai_blocked(raw, ["GPTBot"])
    assert "GPTBot" in blocked


def test_coverage_cap_blocks_excellent_on_thin_audit():
    report = {
        "site": "x.example",
        "audited_at": "2026-01-01T00:00:00Z",
        "summary": {"total_findings": 0, "critical": 0, "high": 0, "medium": 0, "by_category": {}},
        "findings": [],
        "beyond_defect_recommendations": [],
        "coverage": {"pages_discovered": 1, "pages_sampled": 1, "page_classes": ["home"],
                     "rendered": 0, "external_lookups": 0},
        "limitations": [{"check_id": "render.primary_content_js_only",
                         "reason": "JS-added text was not compared; only curl-fetched HTML was available"}],
        "meta": {},
    }
    out = presentation.enrich_report(report)
    assert out["overall_score"] <= 82
    assert out["overall_grade"] != "excellent"


def test_looks_like_xml():
    assert looks_like_xml("<urlset xmlns='x'></urlset>", "application/xml")
    assert not looks_like_xml("<html>nope</html>", "text/html")
