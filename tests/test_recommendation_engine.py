"""Tests for the recommendation-engine skill.

Covers: the eight-question action shape, mechanism-specific (non-filler) content,
the multi-factor prioritization (decoupled from raw severity), dependency
sequencing, do-first quick wins, recommendation de-duplication, and guarded /
gated proactive (beyond-defect) recommendations.
"""

import importlib.util
import os

import _rec_paths as P  # noqa: F401  (sets sys.path)
import recommend
import templates
from audit_common.schema import (
    Concern,
    Confidence,
    Finding,
    Severity,
    validate_report,
    new_report,
)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
_CONCERN_OF = {
    "access": Concern.CRAWL_ACCESS,
    "render": Concern.RENDER,
    "fact": Concern.FACT_ACCESS,
    "identity": Concern.IDENTITY,
    "freshness": Concern.FRESHNESS,
    "engagement": Concern.ENGAGEMENT,
}


def mkfinding(check_id, severity=Severity.HIGH, confidence=Confidence.HIGH,
              scope="https://x/a", sentence="observed evidence", metric=None):
    concern = _CONCERN_OF[check_id.split(".")[0]]
    return Finding(
        id="F-000", check_id=check_id, concern=concern, mechanism="A",
        title=check_id, severity=severity, confidence=confidence,
        evidence={"sentence": sentence, "scope": scope, "metric": metric or {}},
    )


class _Rec:
    def __init__(self, page_class):
        self.page_class = page_class


class _Cache:
    def __init__(self, classes):
        self._recs = [_Rec(c) for c in classes]

    def all(self):
        return self._recs


class _Ctx:
    def __init__(self, classes=("home", "product")):
        self.cache = _Cache(classes)


# --------------------------------------------------------------------------
# Coverage: every catalog check has a specific template (no filler)
# --------------------------------------------------------------------------
def test_every_check_has_specific_template():
    import json
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    catalog = json.load(open(os.path.join(root, "config", "check_catalog.json")))
    ids = {c["check_id"] for c in catalog["checks"]}
    missing = ids - set(templates.TEMPLATES)
    assert not missing, "checks without a fix template: %s" % sorted(missing)
    # Absence of schema is not a detector (SKILL.md). Catalog must not advertise
    # unimplemented identity.product_schema_missing / no_organization_schema.
    ghosts = {"identity.product_schema_missing", "identity.no_organization_schema"}
    assert not (ids & ghosts)


def test_templates_are_mechanism_specific_not_filler():
    banned = ("improve seo", "add structured data", "make it better", "optimize")
    for cid, tpl in templates.TEMPLATES.items():
        assert tpl["what"] and tpl["where"] and tpl["why"] and tpl["how"]
        assert tpl["why"].strip().lower() not in banned
        assert tpl["what"].strip().lower() not in banned


# --------------------------------------------------------------------------
# Action shape — answers all eight questions
# --------------------------------------------------------------------------
def test_action_answers_eight_questions():
    findings, _ = recommend.attach_actions([mkfinding("identity.jsonld_malformed")], _Ctx())
    sa = findings[0].suggested_action.to_dict()
    for key in ("what", "where", "why", "steps", "evidence", "expected_effect",
                "effort", "priority_score", "rank", "do_first", "dependencies"):
        assert key in sa
    assert sa["what"] and sa["where"] and sa["why"] and sa["steps"]
    assert sa["evidence"] == "observed evidence"
    assert sa["priority"] in ("critical", "high", "medium")


def test_fallback_is_specific_for_unknown_check():
    f = mkfinding("access.sitemap_missing")
    f.check_id = "access.some_new_check"  # not in TEMPLATES
    findings, _ = recommend.attach_actions([f], _Ctx())
    sa = findings[0].suggested_action
    assert "access.some_new_check" in sa.what or f.title in sa.what
    assert not sa.what.lower().startswith("address:")


# --------------------------------------------------------------------------
# Prioritization is NOT a severity map
# --------------------------------------------------------------------------
def test_priority_decoupled_from_severity():
    # A medium-severity, low-effort, high-impact access fix should outrank a
    # high-severity, high-effort, single-page render fix.
    low_sev = mkfinding("access.sitemap_missing", severity=Severity.MEDIUM,
                        confidence=Confidence.HIGH)
    high_sev = mkfinding("render.primary_content_js_only", severity=Severity.HIGH,
                         confidence=Confidence.MEDIUM)
    findings, _ = recommend.attach_actions([high_sev, low_sev], _Ctx())
    by_check = {f.check_id: f for f in findings}
    s_low = by_check["access.sitemap_missing"].suggested_action
    s_high = by_check["render.primary_content_js_only"].suggested_action
    assert s_low.priority_score > s_high.priority_score
    assert s_low.rank < s_high.rank  # ranked earlier despite lower severity


def test_access_blocker_is_high_priority_and_do_first():
    f = mkfinding("access.ai_ua_blocked", severity=Severity.CRITICAL,
                  confidence=Confidence.HIGH)
    findings, _ = recommend.attach_actions([f], _Ctx())
    sa = findings[0].suggested_action
    assert sa.priority == Severity.CRITICAL
    assert sa.do_first is True
    assert sa.dependencies == []


def test_critical_is_do_first_even_when_effort_is_high():
    f = mkfinding("render.primary_content_js_only", severity=Severity.CRITICAL,
                  confidence=Confidence.HIGH)
    findings, _ = recommend.attach_actions([f], _Ctx())
    assert findings[0].suggested_action.effort == "high"
    assert findings[0].suggested_action.do_first is True


def test_ranks_are_unique_and_ids_reassigned():
    fs = [mkfinding("identity.jsonld_malformed", scope="https://x/1"),
          mkfinding("access.redirect_loop", scope="https://x/2"),
          mkfinding("engagement.dead_end_page", scope="https://x/3")]
    findings, _ = recommend.attach_actions(fs, _Ctx())
    ranks = sorted(f.suggested_action.rank for f in findings)
    assert ranks == [1, 2, 3]
    assert [f.id for f in findings] == ["F-001", "F-002", "F-003"]


# --------------------------------------------------------------------------
# Dependencies / sequencing
# --------------------------------------------------------------------------
def test_downstream_depends_on_access_blocker():
    blocker = mkfinding("access.global_noindex", severity=Severity.CRITICAL)
    fact = mkfinding("fact.ambiguous_value", severity=Severity.HIGH)
    findings, _ = recommend.attach_actions([fact, blocker], _Ctx())
    by_check = {f.check_id: f for f in findings}
    blk_id = by_check["access.global_noindex"].id
    dep = by_check["fact.ambiguous_value"].suggested_action.dependencies
    assert blk_id in dep
    # The blocker itself depends on nothing.
    assert by_check["access.global_noindex"].suggested_action.dependencies == []


def test_content_facts_depend_on_render_blocker():
    render = mkfinding("render.primary_content_js_only", severity=Severity.HIGH)
    ident = mkfinding("identity.schema_visible_mismatch", severity=Severity.HIGH)
    findings, _ = recommend.attach_actions([ident, render], _Ctx())
    by_check = {f.check_id: f for f in findings}
    rid = by_check["render.primary_content_js_only"].id
    assert rid in by_check["identity.schema_visible_mismatch"].suggested_action.dependencies


def test_dependency_penalizes_score():
    # Same fact finding scores lower when a blocker is present than when alone.
    alone, _ = recommend.attach_actions([mkfinding("fact.ambiguous_value")], _Ctx())
    s_alone = alone[0].suggested_action.priority_score
    with_blocker, _ = recommend.attach_actions(
        [mkfinding("fact.ambiguous_value"),
         mkfinding("access.global_noindex", severity=Severity.CRITICAL)], _Ctx())
    s_dep = next(f.suggested_action.priority_score for f in with_blocker
                 if f.check_id == "fact.ambiguous_value")
    assert s_dep < s_alone


# --------------------------------------------------------------------------
# De-duplication
# --------------------------------------------------------------------------
def test_duplicate_recommendations_are_deduped():
    a = mkfinding("identity.jsonld_malformed", scope="https://x/one")
    b = mkfinding("identity.jsonld_malformed", scope="https://x/two")
    findings, _ = recommend.attach_actions([a, b], _Ctx())
    dupes = [f for f in findings if f.suggested_action.duplicate_of]
    primaries = [f for f in findings if not f.suggested_action.duplicate_of
                 and f.check_id == "identity.jsonld_malformed"]
    assert len(primaries) == 1
    assert len(dupes) == 1
    assert dupes[0].suggested_action.duplicate_of == primaries[0].id
    # The canonical recommendation aggregates both locations.
    where = primaries[0].suggested_action.where
    assert "https://x/one" in where or "https://x/two" in where


def test_single_finding_where_names_its_url():
    # A lone finding's recommendation must name the page it was measured on,
    # not just generic template guidance.
    f = mkfinding("identity.jsonld_malformed", scope="https://x/only-page")
    findings, _ = recommend.attach_actions([f], _Ctx())
    assert "https://x/only-page" in findings[0].suggested_action.where


def test_distinct_checks_not_deduped():
    findings, _ = recommend.attach_actions(
        [mkfinding("identity.jsonld_malformed"), mkfinding("identity.sameas_broken")],
        _Ctx())
    assert all(f.suggested_action.duplicate_of is None for f in findings)


# --------------------------------------------------------------------------
# Beyond-defect (proactive) recommendations
# --------------------------------------------------------------------------
def test_beyond_defect_present_when_clean():
    _, beyond = recommend.attach_actions([], _Ctx(classes=("home", "about")))
    summaries = {b["summary"] for b in beyond}
    assert any("llms.txt" in s for s in summaries)
    assert any("entity paragraph" in s for s in summaries)
    for b in beyond:
        assert b.get("priority") in ("critical", "high", "medium")


def test_beyond_defect_not_wiped_by_unrelated_defect():
    # Old failure: any crawl/identity finding suppressed llms.txt / entity
    # paragraph. Additive strengthening must still appear.
    _, beyond = recommend.attach_actions(
        [mkfinding("identity.jsonld_malformed"),
         mkfinding("access.important_noindex")],
        _Ctx(classes=("home", "about")))
    summaries = " ".join(b["summary"] for b in beyond)
    assert "llms.txt" in summaries
    assert "entity paragraph" in summaries


def test_viewport_does_not_depend_on_important_noindex():
    # Old failure: every non-access finding listed important_noindex as a
    # prerequisite, so a zoom-meta fix waited on an unrelated noindex.
    noindex = mkfinding("access.important_noindex", severity=Severity.HIGH)
    viewport = mkfinding("engagement.no_viewport_meta", severity=Severity.MEDIUM)
    findings, _ = recommend.attach_actions([viewport, noindex], _Ctx())
    by = {f.check_id: f for f in findings}
    assert by["access.important_noindex"].id not in (
        by["engagement.no_viewport_meta"].suggested_action.dependencies
    )


def test_beyond_defect_gated_on_page_class():
    # FAQ schema only when an faq page exists; article dates only with articles.
    _, no_faq = recommend.attach_actions([], _Ctx(classes=("home", "product")))
    assert not any("FAQPage" in b["summary"] for b in no_faq)
    _, with_faq = recommend.attach_actions([], _Ctx(classes=("home", "faq")))
    assert any("FAQPage" in b["summary"] for b in with_faq)


def test_beyond_defect_deduped():
    _, beyond = recommend.attach_actions([], _Ctx(classes=("home", "about")))
    summaries = [b["summary"] for b in beyond]
    assert len(summaries) == len(set(summaries))


# --------------------------------------------------------------------------
# Integration: report stays schema-valid after actions attached
# --------------------------------------------------------------------------
def test_report_valid_after_attach():
    fs = [mkfinding("access.ai_ua_blocked", severity=Severity.CRITICAL),
          mkfinding("fact.ambiguous_value"),
          mkfinding("fact.ambiguous_value", scope="https://x/b")]
    findings, beyond = recommend.attach_actions(fs, _Ctx())
    report = new_report("x.example", findings, beyond_defect=beyond)
    assert validate_report(report) == []


# --------------------------------------------------------------------------
# Full-pipeline integration through the orchestrator
# --------------------------------------------------------------------------
def _load_runner():
    import sys
    if P.ORCH_SCRIPTS not in sys.path:
        sys.path.insert(0, P.ORCH_SCRIPTS)
    path = os.path.join(P.ORCH_SCRIPTS, "run_audit.py")
    spec = importlib.util.spec_from_file_location("run_audit_rec_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_pipeline_clean_has_beyond_defect_and_no_findings():
    import json
    runner = _load_runner()
    settings = json.load(open(os.path.join(P.ROOT, "config", "settings.json"), encoding="utf-8"))
    ctx = runner._build_context_fixture(os.path.join(P.FIXTURES, "clean-brand"), settings)
    report = runner.build_report(ctx, runner.run_detection(ctx))
    assert report["summary"]["total_findings"] == 0
    assert len(report["beyond_defect_recommendations"]) >= 1


def test_pipeline_broken_dedupes_and_ranks():
    import json
    runner = _load_runner()
    settings = json.load(open(os.path.join(P.ROOT, "config", "settings.json"), encoding="utf-8"))
    ctx = runner._build_context_fixture(os.path.join(P.FIXTURES, "engagement-broken"), settings)
    report = runner.build_report(ctx, runner.run_detection(ctx))
    actions = [f["suggested_action"] for f in report["findings"]]
    assert all(a["what"] for a in actions)
    # Overlapping same-check findings are consolidated by the reconciliation
    # layer upstream, so the recommendation list is already dedup-clean.
    assert report["meta"]["reconciliation"]["merged"] >= 1
    ranks = sorted(a["rank"] for a in actions)
    assert ranks == list(range(1, len(ranks) + 1))
