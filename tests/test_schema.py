"""Validate the normalized schema and report contract."""

import os

try:
    from _bootstrap import ROOT  # noqa: F401
except ImportError:
    ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    import sys

    for _p in (ROOT, os.path.join(ROOT, "lib")):
        if _p not in sys.path:
            sys.path.insert(0, _p)

from audit_common.schema import (
    Concern,
    Confidence,
    Finding,
    Severity,
    SuggestedAction,
    new_report,
    validate_report,
)


def _sample_finding():
    return Finding(
        id="F-001",
        check_id="identity.product_schema_missing",
        concern=Concern.IDENTITY,
        mechanism="A",
        title="Product pages lack Product/Offer structured data",
        severity=Severity.HIGH,
        confidence=Confidence.HIGH,
        evidence={"sentence": "0/12 product pages contain Product schema.", "metric": {"with": 0, "checked": 12}},
        suggested_action=SuggestedAction(summary="Add Product JSON-LD.", priority=Severity.HIGH),
    )


def test_valid_report_passes():
    report = new_report("example.com", [_sample_finding()])
    assert validate_report(report) == []
    assert report["summary"]["total_findings"] == 1
    assert report["summary"]["high"] == 1


def test_handout_string_evidence_is_valid():
    report = new_report("example.com", [_sample_finding()])
    report["findings"][0]["evidence"] = "0/12 product pages contain Product schema."
    assert validate_report(report) == []


def test_empty_report_is_valid():
    report = new_report("example.com", [])
    assert validate_report(report) == []
    assert report["summary"]["total_findings"] == 0


def test_missing_required_field_fails():
    report = new_report("example.com", [_sample_finding()])
    del report["findings"][0]["evidence"]
    errors = validate_report(report)
    assert any("evidence" in e for e in errors)


def test_bad_severity_fails():
    report = new_report("example.com", [_sample_finding()])
    report["findings"][0]["severity"] = "urgent"
    errors = validate_report(report)
    assert any("severity" in e for e in errors)


def test_summary_count_mismatch_fails():
    report = new_report("example.com", [_sample_finding()])
    report["summary"]["total_findings"] = 5
    errors = validate_report(report)
    assert any("total_findings" in e for e in errors)


def test_confidence_caps_severity_import():
    # Ensure the compile module's cap constant is consistent with schema ranks.
    from importlib import util

    path = os.path.join(ROOT, "skills", "audit-orchestrator", "scripts", "compile.py")
    spec = util.spec_from_file_location("compile_mod", path)
    mod = util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod._cap_severity(Severity.CRITICAL, Confidence.LOW) == Severity.MEDIUM
    assert mod._cap_severity(Severity.CRITICAL, Confidence.HIGH) == Severity.CRITICAL
    assert mod._cap_severity(Severity.HIGH, Confidence.MEDIUM) == Severity.HIGH
