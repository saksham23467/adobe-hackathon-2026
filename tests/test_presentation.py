"""Tests for the report presentation layer.

Covers the deterministic scoring model, the strict observed / inferred /
recommendation separation (an inference is never labeled an observation), the
"why this matters to AI systems" explanation, the reader-facing sections, and
the end-to-end invariants (still schema-valid, still deterministic, additive
only).
"""

import copy
import importlib.util
import json
import os
import sys

import _present_paths as P  # noqa: F401  (sets sys.path)
import presentation
from audit_common.schema import validate_report


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _runner():
    path = os.path.join(P.ORCH_SCRIPTS, "run_audit.py")
    spec = importlib.util.spec_from_file_location("run_audit_present_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run_fixture(name):
    r = _runner()
    settings = json.load(
        open(os.path.join(P.ROOT, "config", "settings.json"), encoding="utf-8")
    )
    ctx = r._build_context_fixture(os.path.join(P.FIXTURES, name), settings)
    return r.build_report(ctx, r.run_detection(ctx))


def _mk_finding(check_id="engagement.blocking_interstitial", concern="engagement",
                severity="high", confidence="high", sentence="X happens, so Y results.",
                metric=None, contested=False):
    return {
        "id": "F-001",
        "check_id": check_id,
        "concern": concern,
        "category": "Engagement & UX",
        "mechanism": "E",
        "title": "A blocking interstitial precedes content",
        "severity": severity,
        "confidence": confidence,
        "contested": contested,
        "affected_urls": ["https://x/"],
        "evidence_type": "rendered DOM",
        "evidence": {"sentence": sentence, "method": "render", "scope": "https://x/",
                     "metric": metric or {"selector": "div.modal"}, "artifacts": []},
        "sources": [{"check_id": check_id}],
        "suggested_action": {
            "summary": "Stop blocking initial content.",
            "priority": severity,
            "what": "Stop blocking initial content.",
            "where": "The modal component.",
            "why": "It hides value.",
            "steps": ["Defer the modal."],
            "patch": None,
            "expected_effect": "Visitors see value immediately.",
            "effort": "low",
            "impact": 0.7,
            "rank": 1,
            "do_first": True,
            "dependencies": [],
        },
        "related_findings": [],
    }


def _report(findings, beyond=None):
    return {
        "site": "x.example",
        "audited_at": "2026-01-01T00:00:00Z",
        "summary": {"total_findings": len(findings),
                    "critical": sum(1 for f in findings if f["severity"] == "critical"),
                    "high": sum(1 for f in findings if f["severity"] == "high"),
                    "medium": sum(1 for f in findings if f["severity"] == "medium"),
                    "by_category": {}},
        "findings": findings,
        "beyond_defect_recommendations": beyond or [],
        "coverage": {"pages_discovered": 5, "pages_sampled": 5, "page_classes": ["home"],
                     "rendered": 5, "external_lookups": 0},
        "limitations": [],
        "meta": {"runtime_s": 0.02, "renderer": True},
    }


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------
def test_scores_present_and_bounded():
    rep = presentation.enrich_report(_report([_mk_finding()]))
    for k in ("overall_score", "discoverability_score", "engagement_score"):
        assert 0 <= rep[k] <= 100
    assert rep["overall_grade"] in ("excellent", "good", "fair", "poor", "critical")


def test_clean_site_scores_100():
    rep = presentation.enrich_report(_report([]))
    assert rep["overall_score"] == 100
    assert rep["discoverability_score"] == 100
    assert rep["engagement_score"] == 100


def test_engagement_defect_does_not_lower_discoverability():
    rep = presentation.enrich_report(_report([_mk_finding(concern="engagement")]))
    assert rep["discoverability_score"] == 100
    assert rep["engagement_score"] < 100


def test_discoverability_defect_does_not_lower_engagement():
    f = _mk_finding(check_id="render.primary_content_js_only", concern="render",
                    severity="critical", confidence="high")
    rep = presentation.enrich_report(_report([f]))
    assert rep["engagement_score"] == 100
    assert rep["discoverability_score"] < 100


def test_contested_finding_is_discounted():
    plain = presentation.enrich_report(_report([_mk_finding()]))["overall_score"]
    cont = presentation.enrich_report(_report([_mk_finding(contested=True)]))["overall_score"]
    assert cont > plain  # contested weighs less, so the score is higher


def test_confidence_caps_penalty():
    hi = presentation.enrich_report(_report([_mk_finding(confidence="high")]))["overall_score"]
    lo = presentation.enrich_report(_report([_mk_finding(confidence="low", severity="medium")]))["overall_score"]
    assert lo > hi


def test_one_critical_does_not_zero_a_crawled_site():
    f = _mk_finding(check_id="render.primary_content_js_only", concern="render",
                    severity="critical", confidence="high")
    rep = presentation.enrich_report(_report([f]))
    assert rep["overall_score"] > 0
    assert rep["overall_score"] < 100


def test_volume_cannot_zero_a_crawled_site():
    findings = [
        _mk_finding(check_id=f"engagement.dead_end_page", concern="engagement",
                    severity="high", confidence="high")
        for _ in range(40)
    ]
    # Distinct ids so schema validation is not the point; scoring is.
    for i, f in enumerate(findings):
        f["id"] = f"F-{i:03d}"
    rep = presentation.enrich_report(_report(findings))
    assert rep["overall_score"] >= 18
    assert rep["overall_score"] < 100


def test_homepage_unreachable_scores_zero():
    f = _mk_finding(check_id="access.homepage_unreachable", concern="crawl_access",
                    severity="critical", confidence="high")
    rep = presentation.enrich_report(_report([f]))
    assert rep["overall_score"] == 0
    assert rep["discoverability_score"] == 0


def test_low_relevance_is_discounted():
    hi = _mk_finding(severity="high", confidence="high")
    lo = _mk_finding(severity="high", confidence="high")
    lo["semantic_relevance"] = "low"
    a = presentation.enrich_report(_report([hi]))["overall_score"]
    b = presentation.enrich_report(_report([lo]))["overall_score"]
    assert b > a


# --------------------------------------------------------------------------
# Observed vs inferred vs recommendation
# --------------------------------------------------------------------------
def _detail(finding):
    return finding.get("evidence_detail") or {}


def test_observed_fact_excludes_implication():
    f = _mk_finding(sentence="An overlay blocks the page, so visitors see a wall.")
    rep = presentation.enrich_report(_report([f]))
    ev = _detail(rep["findings"][0])
    assert ev["observed_fact"] == "An overlay blocks the page"
    assert "so visitors" not in ev["observed_fact"]


def test_handout_floor_evidence_is_a_string():
    rep = presentation.enrich_report(_report([_mk_finding()]))
    ev = rep["findings"][0]["evidence"]
    assert isinstance(ev, str) and ev
    assert isinstance(rep["findings"][0].get("evidence_detail"), dict)


def test_inferred_problem_is_labeled_separately():
    f = _mk_finding(sentence="An overlay blocks the page, so visitors leave.")
    rep = presentation.enrich_report(_report([f]))
    fd = rep["findings"][0]
    # The inference lives in its own field, distinct from the observed fact.
    assert fd["inferred_problem"].startswith("A blocking interstitial precedes content")
    assert "visitors leave" in fd["inferred_problem"]
    assert fd["inferred_problem"] != _detail(fd)["observed_fact"]
    assert isinstance(fd["evidence"], str)


def test_measurement_recorded_as_basis_measured():
    f = _mk_finding(metric={"gap_ratio": 0.9, "tokens": 12})
    rep = presentation.enrich_report(_report([f]))
    ev = _detail(rep["findings"][0])
    assert ev["basis"] == "measured"
    assert "gap_ratio=0.9" in ev["measurement"]


def test_recommendation_is_in_implementation_not_observation():
    rep = presentation.enrich_report(_report([_mk_finding()]))
    sa = rep["findings"][0]["suggested_action"]
    assert "implementation" in sa
    assert sa["implementation"]["steps"] == ["Defer the modal."]
    # summary + priority required floor preserved
    assert sa["summary"] and sa["priority"] in ("critical", "high", "medium")


def test_no_connective_keeps_full_sentence_as_observed():
    f = _mk_finding(sentence="Three nav links point to different labels for one page.")
    rep = presentation.enrich_report(_report([f]))
    ev = _detail(rep["findings"][0])
    assert ev["observed_fact"] == "Three nav links point to different labels for one page."


# --------------------------------------------------------------------------
# Why-it-matters + per-finding fields
# --------------------------------------------------------------------------
def test_why_it_matters_present_per_concern():
    for concern in ("crawl_access", "render", "fact_access", "identity", "freshness", "engagement"):
        f = _mk_finding(concern=concern)
        rep = presentation.enrich_report(_report([f]))
        assert rep["findings"][0]["why_it_matters_to_ai"]


def test_finding_has_required_professional_fields():
    rep = presentation.enrich_report(_report([_mk_finding()]))
    f = rep["findings"][0]
    for k in ("id", "category", "title", "severity", "confidence", "mechanism",
              "evidence", "affected_urls", "impact", "why_it_matters_to_ai",
              "inferred_problem", "suggested_action"):
        assert k in f, k
    assert set(f["impact"].keys()) >= {"level", "axis", "expected_effect"}
    for k in ("summary", "priority", "implementation", "expected_effect"):
        assert k in f["suggested_action"], k


# --------------------------------------------------------------------------
# Report-level sections
# --------------------------------------------------------------------------
def test_all_new_sections_present():
    rep = presentation.enrich_report(_report([_mk_finding()]))
    for k in ("executive_summary", "overall_score", "discoverability_score",
              "engagement_score", "confidence_summary", "top_priorities",
              "proactive_opportunities", "coverage_summary"):
        assert k in rep, k
    assert "crawl_summary" not in rep
    assert "reading_guide" not in rep


def test_top_priorities_are_rank_ordered():
    f1 = _mk_finding()
    f1["id"] = "F-001"
    f1["suggested_action"]["rank"] = 3
    f2 = _mk_finding()
    f2["id"] = "F-002"
    f2["suggested_action"]["rank"] = 1
    rep = presentation.enrich_report(_report([f1, f2]))
    ids = [p["id"] for p in rep["top_priorities"]]
    assert ids[0] == "F-002"


def test_executive_summary_lists_numbered_actions():
    rep = presentation.enrich_report(_report([_mk_finding()]))
    es = rep["executive_summary"]
    assert "1. Stop blocking initial content" in es
    assert "https://x/" in es
    for banned in ("world-class", "cutting-edge", "revolutionary", "best-in-class", "leverage synerg"):
        assert banned not in es.lower()


def test_top_priorities_include_url_and_evidence():
    rep = presentation.enrich_report(_report([_mk_finding()]))
    p = rep["top_priorities"][0]
    assert p["url"] == "https://x/"
    assert p["evidence"]
    assert p["action"]
    assert "do_first" in p


def test_proactive_opportunities_shaped():
    beyond = [{"id": "llms_txt", "summary": "Publish llms.txt.", "why": "helps assistants",
               "priority": "medium", "effort": "low", "steps": ["List facts."],
               "patch": "# llms.txt"}]
    rep = presentation.enrich_report(_report([], beyond=beyond))
    opp = rep["proactive_opportunities"][0]
    assert opp["title"] == "Publish llms.txt."
    assert opp["rationale"] == "helps assistants"
    assert opp["implementation"]["steps"] == ["List facts."]


def test_confidence_summary_counts_and_note():
    rep = presentation.enrich_report(_report([_mk_finding(confidence="high"),
                                              _mk_finding(confidence="low", severity="medium")]))
    cs = rep["confidence_summary"]
    assert cs["by_confidence"]["high"] == 1
    assert cs["by_confidence"]["low"] == 1
    assert cs["note"]


# --------------------------------------------------------------------------
# Additive + idempotent + end-to-end
# --------------------------------------------------------------------------
def test_enrichment_preserves_required_floor():
    rep = presentation.enrich_report(_report([_mk_finding()]))
    assert validate_report(rep) == []


def test_enrichment_is_idempotent():
    rep1 = presentation.enrich_report(_report([_mk_finding()]))
    rep2 = presentation.enrich_report(copy.deepcopy(rep1))
    assert rep2["overall_score"] == rep1["overall_score"]
    assert validate_report(rep2) == []


def test_pipeline_clean_is_100_and_valid():
    report = _run_fixture("clean-brand")
    assert validate_report(report) == []
    assert report["overall_score"] == 100
    assert report["summary"]["total_findings"] == 0
    assert report["executive_summary"]


def test_pipeline_broken_is_professional_and_valid():
    report = _run_fixture("engagement-broken")
    assert validate_report(report) == []
    assert report["overall_score"] < 100
    assert report["top_priorities"]
    for f in report["findings"]:
        assert isinstance(f["evidence"], str) and f["evidence"]
        assert _detail(f).get("observed_fact")
        assert f["why_it_matters_to_ai"]
        assert f["inferred_problem"]
        assert "implementation" in f["suggested_action"]


def test_pipeline_deterministic_scores():
    r1 = _run_fixture("engagement-broken")
    r2 = _run_fixture("engagement-broken")
    keys = ("overall_score", "discoverability_score", "engagement_score",
            "executive_summary", "top_priorities", "confidence_summary")
    assert {k: r1[k] for k in keys} == {k: r2[k] for k in keys}
