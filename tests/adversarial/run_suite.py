#!/usr/bin/env python3
"""Adversarial website test suite — precision / recall / FP-rate / FN-rate.

Runs the *real* single entrypoint pipeline (audit-orchestrator) against a set of
synthetic-but-realistic fixtures, each of which declares ground-truth
expectations, then scores detection quality against the closed check catalog.

Scoring model (per fixture, over the closed catalog of check_ids):

    expected   = check_ids that a competent auditor MUST report  (ground-truth positives)
    ignore     = check_ids that are acceptable either way        (not scored)
    forbidden  = every other catalog check_id                    (ground-truth negatives)

    reported   = union of finding.check_id and each finding.sources[].check_id

    TP = |expected  & reported|      FN = |expected  - reported|
    FP = |forbidden & reported|      TN = |forbidden - reported|

    precision = TP / (TP + FP)       recall              = TP / (TP + FN)
    fp_rate   = FP / (FP + TN)       fn_rate             = FN / (FN + TP)

Aggregate metrics are micro-averaged (sum the confusion cells across fixtures).

Usage:
    python3 tests/adversarial/run_suite.py               # full suite, table + summary
    python3 tests/adversarial/run_suite.py --json out.json
    python3 tests/adversarial/run_suite.py --only perfect-static ecommerce
    python3 tests/adversarial/run_suite.py --verbose     # list every FP / FN
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import sys
from contextlib import redirect_stdout
from typing import Any, Dict, List, Set, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_FIXTURES = os.path.join(_HERE, "fixtures")
_RUN_AUDIT = os.path.join(_ROOT, "skills", "audit-orchestrator", "scripts", "run_audit.py")
_CATALOG = os.path.join(_ROOT, "config", "check_catalog.json")
_SETTINGS = os.path.join(_ROOT, "config", "settings.json")


def _load_module(path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def catalog_check_ids() -> Set[str]:
    with open(_CATALOG, "r", encoding="utf-8") as fh:
        cat = json.load(fh)
    return {c["check_id"] for c in cat["checks"]}


def reported_check_ids(report: Dict[str, Any]) -> Set[str]:
    """Every check_id the report surfaces — canonical findings plus the merged
    source checks that reconciliation folded into them."""
    out: Set[str] = set()
    for f in report.get("findings", []):
        if f.get("check_id"):
            out.add(f["check_id"])
        for s in f.get("sources", []) or []:
            if isinstance(s, dict) and s.get("check_id"):
                out.add(s["check_id"])
    return out


def run_fixture(run_audit, settings: Dict[str, Any], fixture_dir: str) -> Dict[str, Any]:
    ctx = run_audit._build_context_fixture(fixture_dir, settings)
    # Silence any incidental prints from skills so the table stays clean.
    with redirect_stdout(io.StringIO()):
        observations = run_audit.run_detection(ctx)
        report = run_audit.build_report(ctx, observations)
    return report


def score(expected: Set[str], ignore: Set[str], reported: Set[str],
          catalog: Set[str]) -> Dict[str, Any]:
    forbidden = catalog - expected - ignore
    tp = sorted(expected & reported)
    fn = sorted(expected - reported)
    fp = sorted(forbidden & reported)
    tn_n = len(forbidden - reported)
    return {
        "tp": tp, "fn": fn, "fp": fp,
        "counts": {"TP": len(tp), "FN": len(fn), "FP": len(fp), "TN": tn_n},
    }


def _ratio(num: int, den: int) -> float:
    return round(num / den, 3) if den else 1.0


def metrics(TP: int, FP: int, FN: int, TN: int) -> Dict[str, float]:
    return {
        "precision": _ratio(TP, TP + FP),
        "recall": _ratio(TP, TP + FN),
        "fp_rate": _ratio(FP, FP + TN),
        "fn_rate": _ratio(FN, FN + TP),
    }


def discover_fixtures(only: List[str]) -> List[str]:
    names = []
    for name in sorted(os.listdir(_FIXTURES)):
        d = os.path.join(_FIXTURES, name)
        if os.path.isfile(os.path.join(d, "pages.json")):
            if not only or name in only:
                names.append(name)
    return names


def evaluate(only: List[str] = None) -> Dict[str, Any]:
    """Run every fixture and return {'fixtures': [...], 'aggregate': {...}}.

    Importable so the main test suite can assert on precision/recall as a
    regression guard. No network: fixture mode is fully hermetic.
    """
    run_audit = _load_module(_RUN_AUDIT, "run_audit_suite")
    with open(_SETTINGS, "r", encoding="utf-8") as fh:
        settings = json.load(fh)
    catalog = catalog_check_ids()

    rows: List[Dict[str, Any]] = []
    agg = {"TP": 0, "FP": 0, "FN": 0, "TN": 0}
    for name in discover_fixtures(only or []):
        fdir = os.path.join(_FIXTURES, name)
        with open(os.path.join(fdir, "pages.json"), "r", encoding="utf-8") as fh:
            index = json.load(fh)
        exp = index.get("audit_expectations", {}) or {}
        expected = set(exp.get("expected", []))
        ignore = set(exp.get("ignore", []))
        unknown = (expected | ignore) - catalog
        try:
            report = run_fixture(run_audit, settings, fdir)
            reported = reported_check_ids(report)
            err = ""
        except Exception as e:  # noqa: BLE001
            reported, err = set(), f"{type(e).__name__}: {e}"
        s = score(expected, ignore, reported, catalog)
        for k in agg:
            agg[k] += s["counts"][k]
        rows.append({
            "fixture": name, "mode": exp.get("mode", ""),
            "expected": sorted(expected), "reported": sorted(reported),
            **s, "unknown_ids": sorted(unknown), "error": err,
        })
    agg_m = metrics(agg["TP"], agg["FP"], agg["FN"], agg["TN"])
    return {"fixtures": rows, "aggregate": {**agg, **agg_m}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Adversarial audit test suite.")
    ap.add_argument("--only", nargs="*", default=[], help="run only these fixtures")
    ap.add_argument("--json", help="write full results to this path")
    ap.add_argument("--verbose", action="store_true", help="list every FP and FN")
    args = ap.parse_args(argv)

    result = evaluate(args.only)
    rows = result["fixtures"]
    agg = {k: result["aggregate"][k] for k in ("TP", "FP", "FN", "TN")}

    # ---- table -----------------------------------------------------------
    print(f"\n{'FIXTURE':28} {'TP':>3} {'FN':>3} {'FP':>3} {'TN':>4}  "
          f"{'PREC':>5} {'RECALL':>6}  MODE")
    print("-" * 96)
    for r in rows:
        c = r["counts"]
        m = metrics(c["TP"], c["FP"], c["FN"], c["TN"])
        flag = "  <-- ERROR" if r["error"] else ""
        print(f"{r['fixture']:28} {c['TP']:>3} {c['FN']:>3} {c['FP']:>3} {c['TN']:>4}  "
              f"{m['precision']:>5.2f} {m['recall']:>6.2f}  {r['mode']}{flag}")

    agg_m = metrics(agg["TP"], agg["FP"], agg["FN"], agg["TN"])
    print("-" * 96)
    print(f"{'AGGREGATE (micro)':28} {agg['TP']:>3} {agg['FN']:>3} {agg['FP']:>3} {agg['TN']:>4}  "
          f"{agg_m['precision']:>5.2f} {agg_m['recall']:>6.2f}")
    print(f"\n  precision = {agg_m['precision']:.3f}   recall = {agg_m['recall']:.3f}   "
          f"false-positive rate = {agg_m['fp_rate']:.3f}   false-negative rate = {agg_m['fn_rate']:.3f}")
    print(f"  fixtures = {len(rows)}   confusion = {agg}\n")

    if args.verbose:
        for r in rows:
            if r["fn"] or r["fp"] or r["error"]:
                print(f"# {r['fixture']}")
                if r["error"]:
                    print(f"    ERROR: {r['error']}")
                if r["unknown_ids"]:
                    print(f"    UNKNOWN check_ids in manifest: {r['unknown_ids']}")
                for x in r["fn"]:
                    print(f"    MISS (false negative): {x}")
                for x in r["fp"]:
                    print(f"    OVER (false positive): {x}")
                print()

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"fixtures": rows, "aggregate": {**agg, **agg_m}}, fh, indent=2)
        print(f"  wrote {args.json}\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
