"""Tests for the regression + differential testing framework.

Verifies that:
  * a snapshot is well-formed, deterministic, and scored against fixtures;
  * diffing a snapshot against itself is UNCHANGED and not suspicious;
  * an injected false positive on a clean anchor is flagged SUSPICIOUS;
  * a dropped true positive is flagged as a regression;
  * more findings without a precision/recall gain is flagged as noise;
  * severity inflation and runtime/request growth are surfaced.

Uses a small subset of fixtures for speed; fully hermetic.
"""

import copy
import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_REGR = os.path.join(_HERE, "regression", "regression.py")
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))

_spec = importlib.util.spec_from_file_location("regression_framework", _REGR)
R = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(R)

# A small, representative subset: a precision anchor, a positive-case SaaS
# fixture, and a robots fixture. Keeps the test fast but exercises real audits.
_SUBSET = ["perfect-static", "saas", "blocked-robots"]


def _base():
    return R.build_snapshot(_ROOT, "base", _SUBSET)


# --------------------------------------------------------------------------
def test_snapshot_shape_and_scoring():
    snap = _base()
    assert snap["totals"]["fixtures"] == len(_SUBSET)
    for name in _SUBSET:
        fx = snap["fixtures"][name]
        for key in ("runtime_s", "request_count", "scores", "confusion",
                    "findings", "crawl", "extracted_pages"):
            assert key in fx, f"{name} missing {key}"
    m = snap["metrics"]
    for k in ("precision", "recall", "fp_rate", "fn_rate", "TP", "FP", "FN", "TN"):
        assert k in m
    # our fixtures are known-good: perfect precision on this subset
    assert m["precision"] == 1.0
    assert m["recall"] == 1.0


def test_snapshot_is_deterministic():
    a = _base()
    b = _base()
    # runtime is wall-clock; everything else must be byte-identical.
    for s in (a, b):
        for fx in s["fixtures"].values():
            fx["runtime_s"] = 0.0
        s["created_at"] = ""
        s["git"] = {}
        s["totals"]["runtime_s"] = 0.0
    assert R._sha(a) == R._sha(b), "snapshots must be deterministic"


def test_diff_identical_is_unchanged():
    snap = _base()
    rep = R.diff_snapshots(snap, copy.deepcopy(snap))
    s = rep["summary"]
    assert s["verdict"] == "unchanged"
    assert s["suspicious"] is False
    assert s["added_findings"] == 0
    assert s["removed_findings"] == 0
    assert s["changed_findings"] == 0


def test_injected_false_positive_on_clean_anchor_is_suspicious():
    base = _base()
    cand = copy.deepcopy(base)
    # Inject a forbidden finding onto a precision anchor (perfect-static).
    fake = R._finding_record({
        "check_id": "engagement.dead_end_page",
        "concern": "engagement", "severity": "high", "confidence": "high",
        "affected_urls": ["https://x.example/"], "evidence": {"z": 1},
        "suggested_action": {"summary": "x"},
    })
    cand["fixtures"]["perfect-static"]["findings"].append(fake)
    cand["fixtures"]["perfect-static"]["finding_count"] += 1
    # reflect the new false positive in metrics so the guard has signal
    cand["fixtures"]["perfect-static"]["confusion"]["FP"] += 1
    cand["metrics"]["FP"] += 1
    cand["metrics"].update(R._metrics(cand["metrics"]["TP"], cand["metrics"]["FP"],
                                      cand["metrics"]["FN"], cand["metrics"]["TN"]))
    cand["totals"]["findings"] += 1

    rep = R.diff_snapshots(base, cand)
    assert rep["summary"]["suspicious"] is True
    assert rep["summary"]["added_findings"] == 1
    codes = {f["code"] for f in rep["flags"]}
    assert "new_false_positive" in codes
    assert "finding_on_clean_fixture" in codes
    assert "precision_regression" in codes or "fp_rate_increase" in codes
    added = rep["findings"]["added"][0]
    assert added["ground_truth"] == "forbidden"
    assert added["verdict"] == "regression"
    assert rep["summary"]["verdict"] in ("regressed", "mixed")


def test_dropped_true_positive_is_regression():
    base = _base()
    cand = copy.deepcopy(base)
    # saas fixture is expected to fire fact.ambiguous_value; drop it.
    fx = cand["fixtures"]["saas"]
    fx["findings"] = [f for f in fx["findings"]
                      if f["check_id"] != "fact.ambiguous_value"]
    fx["finding_count"] = len(fx["findings"])
    rep = R.diff_snapshots(base, cand)
    codes = {f["code"] for f in rep["flags"]}
    assert "lost_true_positive" in codes
    removed = [r for r in rep["findings"]["removed"]
               if r["check_id"] == "fact.ambiguous_value"]
    assert removed and removed[0]["verdict"] == "regression"
    assert rep["summary"]["suspicious"] is True


def test_more_findings_without_quality_gain_flagged():
    base = _base()
    cand = copy.deepcopy(base)
    # Add an *unscored* (ignore-bucket) finding: not a TP, not a scored FP,
    # but total findings rise with no precision/recall gain -> noise guard.
    fx = cand["fixtures"]["saas"]
    fx.setdefault("ignore", [])
    fx["ignore"].append("access.url_normalization")
    fx["findings"].append(R._finding_record({
        "check_id": "access.url_normalization", "concern": "crawl",
        "severity": "low", "confidence": "low",
        "affected_urls": [], "evidence": {}, "suggested_action": {},
    }))
    fx["finding_count"] += 1
    cand["totals"]["findings"] += 1
    rep = R.diff_snapshots(base, cand)
    codes = {f["code"] for f in rep["flags"]}
    assert "findings_up_no_quality_gain" in codes
    assert rep["summary"]["suspicious"] is True


def test_severity_inflation_flagged():
    base = _base()
    cand = copy.deepcopy(base)
    fx = cand["fixtures"]["blocked-robots"]
    assert fx["findings"], "blocked-robots should have at least one finding"
    f0 = fx["findings"][0]
    if f0["severity"] != "critical":
        f0["severity"] = "critical"
        rep = R.diff_snapshots(base, cand)
        codes = {f["code"] for f in rep["flags"]}
        assert "severity_inflation" in codes
        assert rep["summary"]["severity_changes"] >= 1


def test_request_and_runtime_growth_surfaced():
    base = _base()
    cand = copy.deepcopy(base)
    cand["totals"]["pages_requested"] += 5
    cand["totals"]["runtime_s"] = base["totals"]["runtime_s"] + 10.0
    # force runtime guard floor
    if base["totals"]["runtime_s"] < 0.10:
        base["totals"]["runtime_s"] = 0.20
        cand["totals"]["runtime_s"] = 0.60
    rep = R.diff_snapshots(base, cand)
    codes = {f["code"] for f in rep["flags"]}
    assert "request_count_increase" in codes
    assert "runtime_regression" in codes
    assert rep["summary"]["request_count_delta"] == 5


def test_removed_false_positive_is_improvement():
    # Start from a candidate that has an extra FP, then treat it as baseline and
    # diff toward the clean snapshot: the FP disappears -> improvement.
    clean = _base()
    noisy = copy.deepcopy(clean)
    fake = R._finding_record({
        "check_id": "engagement.dead_end_page", "concern": "engagement",
        "severity": "high", "confidence": "high",
        "affected_urls": ["https://x.example/"], "evidence": {"z": 1},
        "suggested_action": {"summary": "x"},
    })
    noisy["fixtures"]["perfect-static"]["findings"].append(fake)
    noisy["fixtures"]["perfect-static"]["finding_count"] += 1
    rep = R.diff_snapshots(noisy, clean)
    codes = {f["code"] for f in rep["flags"]}
    assert "removed_false_positive" in codes
    removed = rep["findings"]["removed"][0]
    assert removed["verdict"] == "improvement"
