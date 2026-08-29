"""Regression guard for the synthetic adversarial website test suite.

Runs the full fixture suite through the real orchestrator entrypoint and asserts
detection quality stays high. This turns the adversarial suite into a standing
guard: a detector change that starts firing false positives on a realistic clean
site, or stops catching a planted defect, fails CI here.

The rich per-fixture report and precision/recall/FP-rate/FN-rate table live in
`tests/adversarial/run_suite.py` (run it directly for the full breakdown).
"""

import importlib.util
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_RUN_SUITE = os.path.join(_HERE, "adversarial", "run_suite.py")


def _load_suite():
    spec = importlib.util.spec_from_file_location("adversarial_run_suite", _RUN_SUITE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _result():
    return _load_suite().evaluate()


def test_suite_has_enough_fixtures():
    res = _result()
    assert len(res["fixtures"]) >= 15, "expected at least 15 adversarial fixtures"


def test_every_manifest_id_is_a_real_check():
    # An expected/ignore check_id that is not in the closed catalog is a manifest bug.
    for row in _result()["fixtures"]:
        assert not row["unknown_ids"], f"{row['fixture']}: unknown check_ids {row['unknown_ids']}"


def test_no_fixture_errors():
    for row in _result()["fixtures"]:
        assert not row["error"], f"{row['fixture']} errored: {row['error']}"


def test_precision_is_high():
    # Precision is the headline quality bar: the audit must not cry wolf on
    # realistic sites. Allow a tiny margin for future correct-but-unlisted finds.
    agg = _result()["aggregate"]
    assert agg["precision"] >= 0.95, f"precision regressed to {agg['precision']} (FP={agg['FP']})"


def test_recall_is_high():
    agg = _result()["aggregate"]
    assert agg["recall"] >= 0.90, f"recall regressed to {agg['recall']} (FN={agg['FN']})"


def test_false_positive_rate_is_low():
    agg = _result()["aggregate"]
    assert agg["fp_rate"] <= 0.02, f"false-positive rate regressed to {agg['fp_rate']}"


def test_clean_fixtures_are_silent():
    # The precision anchors (a perfect static site, a thin-but-correct local
    # business) must produce zero findings — the strongest false-positive guard.
    silent = {"perfect-static", "local-business"}
    for row in _result()["fixtures"]:
        if row["fixture"] in silent:
            assert not row["fp"], f"{row['fixture']} produced false positives {row['fp']}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
