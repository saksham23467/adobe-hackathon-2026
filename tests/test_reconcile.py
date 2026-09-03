"""Tests for the orchestrator's finding reconciliation layer.

Covers enrichment, cross-skill merge (corroboration), same-check consolidation,
contradiction resolution (uncertainty preserved, confidence lowered), severity
recalculation, determinism, the non-expert summary, target validation, and the
end-to-end orchestrator invariants (schema-valid, deterministic, clean = zero).
"""

import copy
import importlib.util
import json
import os

import _reconcile_paths as P  # noqa: F401  (sets sys.path)
import reconcile
from audit_common.catalog import load_catalog
from audit_common.schema import (
    Concern,
    Confidence,
    Finding,
    Method,
    Observation,
    Severity,
    Status,
    validate_report,
)

CATALOG = load_catalog()

_CONCERN_OF = {
    "access": Concern.CRAWL_ACCESS, "render": Concern.RENDER,
    "fact": Concern.FACT_ACCESS, "identity": Concern.IDENTITY,
    "freshness": Concern.FRESHNESS, "engagement": Concern.ENGAGEMENT,
}
_METHOD_OF = {
    "access": Method.HTTP, "render": Method.RENDER, "fact": Method.HTTP,
    "identity": Method.SCHEMA, "freshness": Method.EXTERNAL, "engagement": Method.RENDER,
}


def mkfind(check_id, sev=Severity.HIGH, conf=Confidence.MEDIUM, urls=("https://x/a",),
           sentence="observed", metric=None):
    concern = _CONCERN_OF[check_id.split(".")[0]]
    spec = CATALOG.get(check_id)
    return Finding(
        id="F-" + check_id, check_id=check_id, concern=concern,
        mechanism=spec.mechanism if spec else "A", title=(spec.title if spec else check_id),
        severity=sev, confidence=conf,
        evidence={"sentence": sentence, "method": _METHOD_OF[check_id.split(".")[0]].value,
                  "scope": ", ".join(urls), "metric": metric or {}, "artifacts": []},
    )


def mkpass(check_id, urls=("https://x/a",)):
    concern = _CONCERN_OF[check_id.split(".")[0]]
    return Observation(check_id=check_id, concern=concern,
                       method=_METHOD_OF[check_id.split(".")[0]], status=Status.PASS,
                       urls=list(urls))


# --------------------------------------------------------------------------
# Enrichment
# --------------------------------------------------------------------------
def test_enrichment_adds_category_type_urls_sources():
    out, _ = reconcile.reconcile([mkfind("identity.jsonld_malformed")], [], CATALOG)
    f = out[0]
    assert f.category == "Structured Identity"
    assert f.evidence_type == "structured data"
    assert f.affected_urls == ["https://x/a"]
    assert f.sources and f.sources[0]["check_id"] == "identity.jsonld_malformed"


# --------------------------------------------------------------------------
# Cross-skill merge (corroboration)
# --------------------------------------------------------------------------
def test_overlapping_findings_merge_with_corroboration():
    fs = [
        mkfind("render.content_behind_interaction", urls=("https://x/p",)),
        mkfind("fact.behind_interaction", urls=("https://x/p",)),
        mkfind("engagement.content_hidden_interaction", urls=("https://x/p",)),
    ]
    out, stats = reconcile.reconcile(fs, [], CATALOG)
    assert len(out) == 1
    f = out[0]
    assert stats["merged"] == 2
    # Three distinct skills => at least two concerns => confidence bumped to high.
    assert f.confidence == Confidence.HIGH
    checks = {s["check_id"] for s in f.sources}
    assert {"render.content_behind_interaction", "fact.behind_interaction",
            "engagement.content_hidden_interaction"} <= checks
    assert "corroborated_by" in f.evidence["metric"]
    assert "Corroborated by" in f.evidence["sentence"]


def test_no_merge_when_urls_disjoint():
    fs = [
        mkfind("render.content_behind_interaction", urls=("https://x/one",)),
        mkfind("fact.behind_interaction", urls=("https://x/two",)),
    ]
    out, stats = reconcile.reconcile(fs, [], CATALOG)
    assert len(out) == 2 and stats["merged"] == 0


def test_same_check_duplicates_consolidated_without_false_corroboration():
    fs = [
        mkfind("engagement.key_info_undiscoverable", urls=("https://x/",),
               metric={"page_class": "product"}),
        mkfind("engagement.key_info_undiscoverable", urls=("https://x/",),
               metric={"page_class": "contact"}),
    ]
    out, stats = reconcile.reconcile(fs, [], CATALOG)
    assert len(out) == 1 and stats["merged"] == 1
    f = out[0]
    # No confidence bump for same-check consolidation.
    assert f.confidence == Confidence.MEDIUM
    assert set(f.evidence["metric"].get("page_classs", [])) == {"product", "contact"}
    assert "Also affects" in f.evidence["sentence"]


# --------------------------------------------------------------------------
# Contradiction resolution
# --------------------------------------------------------------------------
def test_contradiction_lowers_confidence_and_preserves_finding():
    f = mkfind("render.content_behind_interaction", conf=Confidence.MEDIUM,
               urls=("https://x/p",))
    # A different skill in the same group PASSED on the same URL.
    passes = [mkpass("fact.behind_interaction", urls=("https://x/p",))]
    out, stats = reconcile.reconcile([f], passes, CATALOG)
    assert len(out) == 1  # never dropped
    g = out[0]
    assert g.contested is True
    assert g.confidence == Confidence.LOW  # lowered from medium
    assert stats["contested"] == 1
    assert "Contested" in g.evidence["sentence"]
    # Severity capped by the lowered confidence.
    assert g.severity == Severity.MEDIUM


def test_same_check_pass_elsewhere_is_not_a_contradiction():
    f = mkfind("identity.jsonld_malformed", urls=("https://x/a",))
    passes = [mkpass("identity.jsonld_malformed", urls=("https://x/b",))]
    out, _ = reconcile.reconcile([f], passes, CATALOG)
    assert out[0].contested is False


# --------------------------------------------------------------------------
# Severity recalculation
# --------------------------------------------------------------------------
def test_severity_capped_by_confidence():
    # low confidence must cap severity at medium regardless of catalog default.
    f = mkfind("access.homepage_unreachable", sev=Severity.CRITICAL, conf=Confidence.LOW)
    out, _ = reconcile.reconcile([f], [], CATALOG)
    assert out[0].severity == Severity.MEDIUM


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------
def test_reconcile_is_deterministic():
    fs = [mkfind("identity.jsonld_malformed", urls=("https://x/1",)),
          mkfind("access.redirect_loop", urls=("https://x/2",)),
          mkfind("fact.behind_interaction", urls=("https://x/3",))]
    out1, _ = reconcile.reconcile(copy.deepcopy(fs), [], CATALOG)
    out2, _ = reconcile.reconcile(copy.deepcopy(fs), [], CATALOG)
    assert [f.to_dict() for f in out1] == [f.to_dict() for f in out2]


# --------------------------------------------------------------------------
# Non-expert summary
# --------------------------------------------------------------------------
def test_human_summary_headline_and_categories():
    fs = [mkfind("access.homepage_unreachable", sev=Severity.CRITICAL, conf=Confidence.HIGH),
          mkfind("engagement.dead_end_page", sev=Severity.MEDIUM)]
    out, _ = reconcile.reconcile(fs, [], CATALOG)
    hs = reconcile.human_summary(out)
    assert "issue(s) found" in hs["headline"]
    assert hs["by_category"]
    empty = reconcile.human_summary([])
    assert "No blocking" in empty["headline"]


def test_headline_matches_do_first_presence():
    # Old failure mode: the headline always told the reader to "start with the
    # items marked 'do first'", even when no finding carried that marker.
    from audit_common.schema import SuggestedAction

    with_marker = mkfind("access.homepage_unreachable", sev=Severity.HIGH)
    with_marker.suggested_action = SuggestedAction(
        summary="s", priority=Severity.HIGH, do_first=True)
    assert "do first" in reconcile.human_summary([with_marker])["headline"]

    without_marker = mkfind("engagement.dead_end_page", sev=Severity.MEDIUM)
    without_marker.suggested_action = SuggestedAction(
        summary="s", priority=Severity.MEDIUM, do_first=False)
    hs = reconcile.human_summary([without_marker])
    assert "do first" not in hs["headline"]
    assert "top_priorities" in hs["headline"]


# --------------------------------------------------------------------------
# Target validation
# --------------------------------------------------------------------------
def _runner():
    path = os.path.join(P.ORCH_SCRIPTS, "run_audit.py")
    spec = importlib.util.spec_from_file_location("run_audit_recon_test", path)
    mod = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_validate_target_accepts_and_normalizes():
    r = _runner()
    assert r.validate_target("example.com").startswith("http")
    assert r.validate_target("https://example.com/x").startswith("https://")


def test_validate_target_rejects_bad_input():
    r = _runner()
    for bad in ("", "   ", "ftp://example.com", "not a url"):
        try:
            r.validate_target(bad)
            assert False, "expected ValueError for %r" % bad
        except ValueError:
            pass


# --------------------------------------------------------------------------
# End-to-end orchestrator invariants
# --------------------------------------------------------------------------
def _run_fixture(name):
    r = _runner()
    settings = json.load(open(os.path.join(P.ROOT, "config", "settings.json"), encoding="utf-8"))
    ctx = r._build_context_fixture(os.path.join(P.FIXTURES, name), settings)
    return r, r.build_report(ctx, r.run_detection(ctx))


def test_pipeline_clean_zero_and_schema_valid():
    _, report = _run_fixture("clean-brand")
    assert validate_report(report) == []
    assert report["summary"]["total_findings"] == 0
    assert "headline" in report["summary"]
    assert "reconciliation" in report["meta"]


def test_pipeline_findings_are_enriched_and_valid():
    _, report = _run_fixture("engagement-broken")
    assert validate_report(report) == []
    for f in report["findings"]:
        assert f["category"] and f["evidence_type"]
        assert "affected_urls" in f and "sources" in f
        assert f["suggested_action"]["what"]


def test_pipeline_is_deterministic_for_same_snapshot():
    _, r1 = _run_fixture("engagement-broken")
    _, r2 = _run_fixture("engagement-broken")
    # Ignore timestamps / wall clock; compare the deterministic core.
    def core(r):
        return {"findings": r["findings"], "summary": {k: v for k, v in r["summary"].items()},
                "beyond": r["beyond_defect_recommendations"]}
    assert core(r1) == core(r2)
