"""End-to-end hermetic smoke test against the clean-brand fixture.

Invariants:
  * The full pipeline runs offline and emits a schema-valid report.
  * A clean site produces ZERO defect findings (the primary FP guard).
  * Inconclusive checks (e.g. external corroboration) land in limitations.
  * Runtime metadata is present.
"""

import importlib.util
import json
import os

try:
    from _bootstrap import FIXTURE_CLEAN, ROOT
except ImportError:
    ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    FIXTURE_CLEAN = os.path.join(ROOT, "tests", "fixtures", "clean-brand")
    import sys

    for _p in (ROOT, os.path.join(ROOT, "lib")):
        if _p not in sys.path:
            sys.path.insert(0, _p)


def _load_runner():
    import sys

    scripts_dir = os.path.join(ROOT, "skills", "audit-orchestrator", "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    path = os.path.join(scripts_dir, "run_audit.py")
    spec = importlib.util.spec_from_file_location("run_audit_mod", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _run_fixture():
    runner = _load_runner()
    settings = json.load(open(os.path.join(ROOT, "config", "settings.json"), encoding="utf-8"))
    ctx = runner._build_context_fixture(FIXTURE_CLEAN, settings)
    observations = runner.run_detection(ctx)
    report = runner.build_report(ctx, observations)
    return runner, report


def test_pipeline_runs_and_validates():
    from audit_common.schema import validate_report

    _, report = _run_fixture()
    assert validate_report(report) == [], "report must be schema-valid"


def test_clean_site_has_zero_findings():
    _, report = _run_fixture()
    assert report["summary"]["total_findings"] == 0, (
        "clean fixture must produce zero defect findings; got: "
        + json.dumps([f["check_id"] for f in report["findings"]])
    )


def test_inconclusive_becomes_limitation():
    _, report = _run_fixture()
    limitation_checks = {l["check_id"] for l in report["limitations"]}
    assert "freshness.claim_conflict" in limitation_checks


def test_coverage_and_meta_present():
    _, report = _run_fixture()
    assert report["coverage"]["pages_discovered"] >= 2
    assert "runtime_s" in report["meta"]
    assert report["coverage"]["page_classes"]


def test_beyond_defect_recommendations_present():
    _, report = _run_fixture()
    # A clean site has no defects, so proactive suggestions should appear.
    assert isinstance(report["beyond_defect_recommendations"], list)
    assert len(report["beyond_defect_recommendations"]) >= 1
