#!/usr/bin/env python3
"""Regression + differential testing framework for the marketplace.

Goal: stop us from accidentally making the detector *noisier* (or *blinder*)
while adding features. A change that produces more findings is NOT automatically
an improvement — every finding delta is scored against fixture ground truth.

Two commands:

  snapshot   Run every adversarial fixture through the real audit entrypoint and
             record a full, deterministic snapshot: crawl snapshot, extracted
             pages (fingerprinted), findings + severity + confidence,
             recommendations, runtime, and request counts. Also computes
             precision / recall / FP-rate / FN-rate against fixture expectations.

  diff       Compare two snapshots (baseline vs candidate) and emit a
             machine-readable regression report: added / removed / changed
             findings, severity / confidence / recommendation changes, runtime
             and request-count changes, metric deltas, and *flagged suspicious
             regressions* (new false positives, precision drops, FP-rate rises,
             finding-count rises without precision gains, lost true positives).

Typical loop (same fixtures, different detector code):

    python3 tests/regression/regression.py snapshot --label baseline
    # ...do feature work...
    python3 tests/regression/regression.py snapshot --label candidate
    python3 tests/regression/regression.py diff baseline candidate --fail-on-suspicious

Compare two git versions directly (runs each version's own code):

    python3 tests/regression/regression.py snapshot --ref HEAD~1 --label prev
    python3 tests/regression/regression.py snapshot --label now
    python3 tests/regression/regression.py diff prev now

Fully hermetic (fixture mode, no network) and deterministic.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from typing import Any, Dict, List, Optional, Set, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_DEFAULT_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
_SNAP_DIR = os.path.join(_HERE, "snapshots")
_REPORT_DIR = os.path.join(_HERE, "reports")

# Fixtures whose correct output is *nothing*. A new finding here is almost
# certainly a false positive and is always treated as suspicious.
_PRECISION_ANCHORS = {"perfect-static", "local-business", "docs-heavy"}

_SEV_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0, "": -1}
_CONF_RANK = {"high": 2, "medium": 1, "low": 0, "": -1}


# ==========================================================================
# Harness — load a specific marketplace root and drive its audit entrypoint
# ==========================================================================
class Harness:
    """Loads run_audit + settings + catalog + fixtures from one marketplace root."""

    _counter = 0

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(root)
        self.run_audit = self._load_run_audit()
        with open(os.path.join(self.root, "config", "settings.json"), "r", encoding="utf-8") as fh:
            self.settings = json.load(fh)
        self.catalog = self._load_catalog()
        self.fixtures_dir = os.path.join(self.root, "tests", "adversarial", "fixtures")

    def _load_run_audit(self):
        path = os.path.join(self.root, "skills", "audit-orchestrator", "scripts", "run_audit.py")
        Harness._counter += 1
        name = f"regr_run_audit_{Harness._counter}"
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    def _load_catalog(self) -> Set[str]:
        with open(os.path.join(self.root, "config", "check_catalog.json"), "r", encoding="utf-8") as fh:
            cat = json.load(fh)
        return {c["check_id"] for c in cat["checks"]}

    def fixtures(self, only: List[str]) -> List[str]:
        if not os.path.isdir(self.fixtures_dir):
            return []
        out = []
        for name in sorted(os.listdir(self.fixtures_dir)):
            if os.path.isfile(os.path.join(self.fixtures_dir, name, "pages.json")):
                if not only or name in only:
                    out.append(name)
        return out


# ==========================================================================
# Scoring (inline so a snapshot is self-describing against its own catalog)
# ==========================================================================
def _reported_check_ids(report: Dict[str, Any]) -> Set[str]:
    out: Set[str] = set()
    for f in report.get("findings", []):
        if f.get("check_id"):
            out.add(f["check_id"])
        for s in f.get("sources", []) or []:
            if isinstance(s, dict) and s.get("check_id"):
                out.add(s["check_id"])
    return out


def _score(expected: Set[str], ignore: Set[str], reported: Set[str],
           catalog: Set[str]) -> Dict[str, Any]:
    forbidden = catalog - expected - ignore
    tp, fn = sorted(expected & reported), sorted(expected - reported)
    fp, tn = sorted(forbidden & reported), len(forbidden - reported)
    return {"TP": len(tp), "FN": len(fn), "FP": len(fp), "TN": tn,
            "fp_ids": fp, "fn_ids": fn, "tp_ids": tp}


def _ratio(n: int, d: int) -> float:
    return round(n / d, 4) if d else 1.0


def _metrics(TP: int, FP: int, FN: int, TN: int) -> Dict[str, float]:
    return {"precision": _ratio(TP, TP + FP), "recall": _ratio(TP, TP + FN),
            "fp_rate": _ratio(FP, FP + TN), "fn_rate": _ratio(FN, FN + TP)}


def _sha(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# ==========================================================================
# Snapshot construction
# ==========================================================================
def _finding_record(f: Dict[str, Any]) -> Dict[str, Any]:
    urls = sorted(f.get("affected_urls") or [])
    sa = f.get("suggested_action") or {}
    rec = {
        "summary": sa.get("summary", ""),
        "priority": sa.get("priority", ""),
        "effort": sa.get("effort", ""),
        "do_first": bool(sa.get("do_first", False)),
        "rank": sa.get("rank", 0),
        "steps_sha": _sha(sa.get("steps") or sa.get("implementation") or []),
    }
    return {
        "key": f.get("check_id", "") + "|" + "&".join(urls),
        "check_id": f.get("check_id", ""),
        "concern": f.get("concern", ""),
        "category": f.get("category", ""),
        "severity": f.get("severity", ""),
        "confidence": f.get("confidence", ""),
        "contested": bool(f.get("contested", False)),
        "title": f.get("title", ""),
        "mechanism": f.get("mechanism", ""),
        "affected_urls": urls,
        "evidence_sha": _sha(f.get("evidence")),
        "recommendation": rec,
        "recommendation_sha": _sha(rec),
    }


def _crawl_snapshot(crawl) -> Dict[str, Any]:
    if crawl is None:
        return {}
    pages = []
    for p in sorted(crawl.pages.values(), key=lambda x: x.url):
        pages.append({
            "url": p.url, "status": p.status_code, "fetched": p.fetched,
            "is_html": p.is_html, "page_class": None, "canonical": p.canonical,
            "noindex": p.noindex, "in_sitemap": p.in_sitemap,
            "depth": p.depth, "inbound_internal": p.inbound_internal,
        })
    return {
        "pages_discovered": len(crawl.pages),
        "pages_fetched": len(crawl.fetched_pages()),
        "html_pages": len(crawl.html_pages()),
        "robots": {"available": crawl.robots.available, "reachable": crawl.robots.reachable,
                   "status": crawl.robots.status_code,
                   "disallow_all_agents": sorted(crawl.robots.disallow_all_agents)},
        "sitemaps": [{"url": s.url, "available": s.available, "kind": s.kind,
                      "well_formed": s.well_formed, "url_count": s.url_count}
                     for s in crawl.sitemaps],
        "stats": {"pages_requested": crawl.stats.pages_requested,
                  "pages_crawled": crawl.stats.pages_crawled,
                  "budget_hit": crawl.stats.budget_hit,
                  "page_cap_hit": crawl.stats.page_cap_hit},
        "pages": pages,
    }


def _extracted_pages(cache) -> List[Dict[str, Any]]:
    out = []
    for r in sorted(cache.all(), key=lambda x: x.url):
        out.append({
            "url": r.url, "page_class": r.page_class, "status": r.status_code,
            "raw_len": len(r.raw_html or ""), "rendered_len": len(r.rendered_html or ""),
            "raw_text_len": len(r.raw_text or ""), "rendered_text_len": len(r.rendered_text or ""),
            "raw_text_sha": _sha(r.raw_text or ""),
            "rendered_text_sha": _sha(r.rendered_text or ""),
        })
    return out


def snapshot_fixture(h: Harness, name: str) -> Dict[str, Any]:
    fdir = os.path.join(h.fixtures_dir, name)
    with open(os.path.join(fdir, "pages.json"), "r", encoding="utf-8") as fh:
        index = json.load(fh)
    exp = index.get("audit_expectations", {}) or {}
    expected = set(exp.get("expected", []))
    ignore = set(exp.get("ignore", []))

    ra = h.run_audit
    ctx = ra._build_context_fixture(fdir, h.settings)
    t0 = time.perf_counter()
    with redirect_stdout(io.StringIO()):
        observations = ra.run_detection(ctx)
        report = ra.build_report(ctx, observations)
    runtime_s = round(time.perf_counter() - t0, 4)

    crawl = getattr(ctx, "crawl_result", None)
    gate = getattr(ctx, "gate", None)
    reported = _reported_check_ids(report)
    conf = _score(expected, ignore, reported, h.catalog)

    findings = [_finding_record(f) for f in report.get("findings", [])]
    findings.sort(key=lambda r: r["key"])

    return {
        "mode": exp.get("mode", ""),
        "expected": sorted(expected),
        "ignore": sorted(ignore),
        "runtime_s": runtime_s,
        "request_count": {
            "pages_requested": (crawl.stats.pages_requested if crawl else 0),
            "pages_crawled": (crawl.stats.pages_crawled if crawl else 0),
            "gate_pages_fetched": (getattr(gate, "pages_fetched", 0) if gate else 0),
            "external_calls": (getattr(gate, "external_calls", 0) if gate else 0),
        },
        "scores": {
            "overall": report.get("overall_score"),
            "discoverability": report.get("discoverability_score"),
            "engagement": report.get("engagement_score"),
        },
        "confusion": {k: conf[k] for k in ("TP", "FN", "FP", "TN")},
        "fp_ids": conf["fp_ids"], "fn_ids": conf["fn_ids"], "tp_ids": conf["tp_ids"],
        "finding_count": len(findings),
        "findings": findings,
        "crawl": _crawl_snapshot(crawl),
        "extracted_pages": _extracted_pages(ctx.cache),
        "beyond_defect_count": len(report.get("proactive_opportunities", []) or report.get("beyond_defect", []) or []),
    }


def _git_info(root: str) -> Dict[str, Any]:
    def _run(*a):
        return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True, timeout=10)
    info = {"commit": "", "branch": "", "dirty": None}
    try:
        r = _run("rev-parse", "HEAD")
        if r.returncode == 0:
            info["commit"] = r.stdout.strip()
        r = _run("rev-parse", "--abbrev-ref", "HEAD")
        if r.returncode == 0:
            info["branch"] = r.stdout.strip()
        r = _run("status", "--porcelain")
        if r.returncode == 0:
            info["dirty"] = bool(r.stdout.strip())
    except Exception:  # noqa: BLE001
        pass
    return info


def build_snapshot(root: str, label: str, only: List[str]) -> Dict[str, Any]:
    h = Harness(root)
    fixtures = {name: snapshot_fixture(h, name) for name in h.fixtures(only)}
    agg = {"TP": 0, "FP": 0, "FN": 0, "TN": 0}
    total_runtime = 0.0
    total_requests = 0
    for fx in fixtures.values():
        for k in agg:
            agg[k] += fx["confusion"][k]
        total_runtime += fx["runtime_s"]
        total_requests += fx["request_count"]["pages_requested"]
    return {
        "label": label,
        "created_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "root": root,
        "git": _git_info(root),
        "catalog_size": len(h.catalog),
        "metrics": {**agg, **_metrics(agg["TP"], agg["FP"], agg["FN"], agg["TN"])},
        "totals": {
            "fixtures": len(fixtures),
            "findings": sum(fx["finding_count"] for fx in fixtures.values()),
            "runtime_s": round(total_runtime, 4),
            "pages_requested": total_requests,
        },
        "fixtures": fixtures,
    }


# ==========================================================================
# Diff / regression report
# ==========================================================================
def _ground_truth(check_id: str, expected: Set[str], ignore: Set[str]) -> str:
    if check_id in expected:
        return "expected"
    if check_id in ignore:
        return "ignore"
    return "forbidden"


def _index_findings(fx: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {rec["key"]: rec for rec in fx.get("findings", [])}


def _reconcile_url_shift(removed: Dict[str, Dict], added: Dict[str, Dict]
                         ) -> List[Tuple[Dict, Dict]]:
    """Pair a removed and added finding that share a check_id but differ only in
    affected_urls, so a URL change reads as 'changed', not churn."""
    pairs = []
    from collections import defaultdict
    rem_by_check = defaultdict(list)
    add_by_check = defaultdict(list)
    for k, r in removed.items():
        rem_by_check[r["check_id"]].append(k)
    for k, a in added.items():
        add_by_check[a["check_id"]].append(k)
    for check, rkeys in rem_by_check.items():
        akeys = add_by_check.get(check, [])
        while rkeys and akeys and len(rkeys) == 1 and len(akeys) == 1:
            rk, ak = rkeys.pop(), akeys.pop()
            pairs.append((removed.pop(rk), added.pop(ak)))
    return pairs


def _compare_pair(base_f: Dict, cand_f: Dict) -> Dict[str, Any]:
    changes: Dict[str, Any] = {}
    if base_f["severity"] != cand_f["severity"]:
        changes["severity"] = [base_f["severity"], cand_f["severity"]]
    if base_f["confidence"] != cand_f["confidence"]:
        changes["confidence"] = [base_f["confidence"], cand_f["confidence"]]
    if base_f["recommendation_sha"] != cand_f["recommendation_sha"]:
        changes["recommendation"] = {"before": base_f["recommendation"],
                                     "after": cand_f["recommendation"]}
    if base_f["evidence_sha"] != cand_f["evidence_sha"]:
        changes["evidence"] = [base_f["evidence_sha"], cand_f["evidence_sha"]]
    if base_f["affected_urls"] != cand_f["affected_urls"]:
        changes["affected_urls"] = [base_f["affected_urls"], cand_f["affected_urls"]]
    return changes


def diff_snapshots(base: Dict[str, Any], cand: Dict[str, Any]) -> Dict[str, Any]:
    base_fx, cand_fx = base["fixtures"], cand["fixtures"]
    shared = sorted(set(base_fx) & set(cand_fx))

    added, removed, changed = [], [], []
    sev_changes = conf_changes = rec_changes = 0
    per_fixture: Dict[str, Any] = {}
    flags: List[Dict[str, Any]] = []

    for name in shared:
        b, c = base_fx[name], cand_fx[name]
        expected = set(c.get("expected", []))
        ignore = set(c.get("ignore", []))
        b_idx, c_idx = _index_findings(b), _index_findings(c)

        rem = {k: v for k, v in b_idx.items() if k not in c_idx}
        add = {k: v for k, v in c_idx.items() if k not in b_idx}
        pairs = _reconcile_url_shift(rem, add)  # mutates rem/add

        fx_added, fx_removed, fx_changed = [], [], []

        for k in sorted(add):
            f = add[k]
            gt = _ground_truth(f["check_id"], expected, ignore)
            verdict = {"expected": "improvement", "forbidden": "regression",
                       "ignore": "neutral"}[gt]
            row = {"fixture": name, "key": k, "check_id": f["check_id"],
                   "severity": f["severity"], "confidence": f["confidence"],
                   "ground_truth": gt, "verdict": verdict}
            added.append(row)
            fx_added.append(row)
            if name in _PRECISION_ANCHORS:
                flags.append({"level": "suspicious", "code": "finding_on_clean_fixture",
                              "fixture": name,
                              "message": f"New finding {f['check_id']} on clean anchor {name} "
                                         f"— anchors must stay silent."})
            if gt == "forbidden":
                flags.append({"level": "suspicious", "code": "new_false_positive",
                              "fixture": name,
                              "message": f"NEW false positive {f['check_id']} on {name} "
                                         f"({f['severity']}/{f['confidence']}) — noisier."})
            elif gt == "ignore":
                flags.append({"level": "warning", "code": "new_unscored_finding",
                              "fixture": name,
                              "message": f"New unscored finding {f['check_id']} on {name} "
                                         f"— review whether it is signal or noise."})
            else:
                flags.append({"level": "info", "code": "new_true_positive",
                              "fixture": name,
                              "message": f"Now catching {f['check_id']} on {name} (recall up)."})

        for k in sorted(rem):
            f = rem[k]
            gt = _ground_truth(f["check_id"], expected, ignore)
            verdict = {"expected": "regression", "forbidden": "improvement",
                       "ignore": "neutral"}[gt]
            row = {"fixture": name, "key": k, "check_id": f["check_id"],
                   "severity": f["severity"], "confidence": f["confidence"],
                   "ground_truth": gt, "verdict": verdict}
            removed.append(row)
            fx_removed.append(row)
            if gt == "expected":
                flags.append({"level": "regression", "code": "lost_true_positive",
                              "fixture": name,
                              "message": f"No longer catching planted defect {f['check_id']} "
                                         f"on {name} (recall down)."})
            elif gt == "forbidden":
                flags.append({"level": "info", "code": "removed_false_positive",
                              "fixture": name,
                              "message": f"Removed false positive {f['check_id']} on {name} "
                                         f"(precision up)."})

        for rf, af in pairs:
            ch = _compare_pair(rf, af)
            row = {"fixture": name, "check_id": af["check_id"], "changes": ch}
            changed.append(row); fx_changed.append(row)
            if "severity" in ch:
                sev_changes += 1
            if "confidence" in ch:
                conf_changes += 1
            if "recommendation" in ch:
                rec_changes += 1

        for k in sorted(set(b_idx) & set(c_idx)):
            ch = _compare_pair(b_idx[k], c_idx[k])
            if not ch:
                continue
            row = {"fixture": name, "key": k, "check_id": c_idx[k]["check_id"], "changes": ch}
            changed.append(row); fx_changed.append(row)
            if "severity" in ch:
                sev_changes += 1
                a, bb = ch["severity"]
                if _SEV_RANK.get(bb, -1) > _SEV_RANK.get(a, -1):
                    flags.append({"level": "warning", "code": "severity_inflation",
                                  "fixture": name,
                                  "message": f"Severity of {c_idx[k]['check_id']} on {name} "
                                             f"raised {a} -> {bb}; confirm it is warranted."})
            if "confidence" in ch:
                conf_changes += 1
            if "recommendation" in ch:
                rec_changes += 1

        # per-fixture metric + runtime deltas
        bm = _metrics(b["confusion"]["TP"], b["confusion"]["FP"], b["confusion"]["FN"], b["confusion"]["TN"])
        cm = _metrics(c["confusion"]["TP"], c["confusion"]["FP"], c["confusion"]["FN"], c["confusion"]["TN"])
        per_fixture[name] = {
            "added": len(fx_added), "removed": len(fx_removed), "changed": len(fx_changed),
            "finding_count": [b["finding_count"], c["finding_count"]],
            "precision": [bm["precision"], cm["precision"]],
            "recall": [bm["recall"], cm["recall"]],
            "fp_rate": [bm["fp_rate"], cm["fp_rate"]],
            "runtime_s": [b["runtime_s"], c["runtime_s"]],
            "pages_requested": [b["request_count"]["pages_requested"],
                                c["request_count"]["pages_requested"]],
            "scores": {"before": b["scores"], "after": c["scores"]},
        }

    # fixtures only in one snapshot
    only_base = sorted(set(base_fx) - set(cand_fx))
    only_cand = sorted(set(cand_fx) - set(base_fx))
    for n in only_cand:
        flags.append({"level": "info", "code": "fixture_added", "fixture": n,
                      "message": f"Fixture {n} exists only in candidate."})
    for n in only_base:
        flags.append({"level": "warning", "code": "fixture_removed", "fixture": n,
                      "message": f"Fixture {n} exists only in baseline (coverage dropped)."})

    # ---- aggregate metric deltas ----------------------------------------
    bmet, cmet = base["metrics"], cand["metrics"]
    mdelta = {k: round(cmet[k] - bmet[k], 4)
              for k in ("precision", "recall", "fp_rate", "fn_rate")}

    # ---- the core guard: more findings without a precision gain ----------
    b_total = base["totals"]["findings"]
    c_total = cand["totals"]["findings"]
    if c_total > b_total and mdelta["precision"] <= 0 and mdelta["recall"] <= 0:
        flags.append({"level": "suspicious", "code": "findings_up_no_quality_gain",
                      "message": f"Total findings rose {b_total} -> {c_total} with no gain in "
                                 f"precision or recall — likely added noise, not signal."})
    if mdelta["precision"] < 0:
        flags.append({"level": "suspicious", "code": "precision_regression",
                      "message": f"Precision dropped {bmet['precision']} -> {cmet['precision']}."})
    if mdelta["fp_rate"] > 0:
        flags.append({"level": "suspicious", "code": "fp_rate_increase",
                      "message": f"False-positive rate rose {bmet['fp_rate']} -> {cmet['fp_rate']} "
                                 f"(detector got noisier)."})
    if mdelta["recall"] < 0:
        flags.append({"level": "regression", "code": "recall_regression",
                      "message": f"Recall dropped {bmet['recall']} -> {cmet['recall']} "
                                 f"(missing real defects)."})

    # ---- runtime + request budget deltas --------------------------------
    b_rt, c_rt = base["totals"]["runtime_s"], cand["totals"]["runtime_s"]
    if b_rt >= 0.10 and c_rt > b_rt * 1.5:
        flags.append({"level": "warning", "code": "runtime_regression",
                      "message": f"Total runtime rose {b_rt}s -> {c_rt}s (>50%)."})
    b_req, c_req = base["totals"]["pages_requested"], cand["totals"]["pages_requested"]
    if c_req > b_req:
        flags.append({"level": "warning", "code": "request_count_increase",
                      "message": f"Total page requests rose {b_req} -> {c_req} (heavier crawl)."})

    suspicious = any(f["level"] in ("suspicious", "regression") for f in flags)
    improvements = any(f["verdict"] == "improvement" for f in added) or \
        any(f["verdict"] == "improvement" for f in removed) or mdelta["precision"] > 0 or \
        mdelta["recall"] > 0
    regressions = suspicious
    if regressions and improvements:
        verdict = "mixed"
    elif regressions:
        verdict = "regressed"
    elif improvements or added or removed or changed:
        verdict = "improved" if (improvements) else "changed"
    else:
        verdict = "unchanged"

    return {
        "baseline": {"label": base["label"], "git": base["git"],
                     "created_at": base["created_at"], "metrics": bmet,
                     "totals": base["totals"]},
        "candidate": {"label": cand["label"], "git": cand["git"],
                      "created_at": cand["created_at"], "metrics": cmet,
                      "totals": cand["totals"]},
        "summary": {
            "verdict": verdict,
            "suspicious": suspicious,
            "added_findings": len(added),
            "removed_findings": len(removed),
            "changed_findings": len(changed),
            "severity_changes": sev_changes,
            "confidence_changes": conf_changes,
            "recommendation_changes": rec_changes,
            "precision_delta": mdelta["precision"],
            "recall_delta": mdelta["recall"],
            "fp_rate_delta": mdelta["fp_rate"],
            "fn_rate_delta": mdelta["fn_rate"],
            "finding_count_delta": c_total - b_total,
            "runtime_delta_s": round(c_rt - b_rt, 4),
            "request_count_delta": c_req - b_req,
            "flag_counts": _count_levels(flags),
        },
        "metrics": {"baseline": bmet, "candidate": cmet, "delta": mdelta},
        "findings": {"added": added, "removed": removed, "changed": changed},
        "per_fixture": per_fixture,
        "fixtures_only_in_baseline": only_base,
        "fixtures_only_in_candidate": only_cand,
        "flags": flags,
    }


def _count_levels(flags: List[Dict[str, Any]]) -> Dict[str, int]:
    out = {"suspicious": 0, "regression": 0, "warning": 0, "info": 0}
    for f in flags:
        out[f["level"]] = out.get(f["level"], 0) + 1
    return out


# ==========================================================================
# I/O + rendering
# ==========================================================================
def _snap_path(label: str) -> str:
    return label if label.endswith(".json") and os.path.sep in label else \
        os.path.join(_SNAP_DIR, f"{label}.json")


def save_snapshot(snap: Dict[str, Any], label: str) -> str:
    os.makedirs(_SNAP_DIR, exist_ok=True)
    path = _snap_path(label)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, indent=2, ensure_ascii=False, sort_keys=True)
    return path


def load_snapshot(label: str) -> Dict[str, Any]:
    path = _snap_path(label)
    if not os.path.exists(path):
        raise SystemExit(f"snapshot not found: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def render_diff(report: Dict[str, Any]) -> None:
    s = report["summary"]
    b, c = report["baseline"], report["candidate"]
    mark = {"unchanged": "=", "improved": "+", "changed": "~",
            "mixed": "±", "regressed": "!"}.get(s["verdict"], "?")
    print(f"\n  {b['label']}  ->  {c['label']}")
    print(f"  verdict: [{mark}] {s['verdict'].upper()}"
          + ("   *** SUSPICIOUS ***" if s["suspicious"] else ""))
    print("-" * 78)
    m = report["metrics"]
    print("  metric            baseline  candidate   delta")
    for k in ("precision", "recall", "fp_rate", "fn_rate"):
        d = m["delta"][k]
        arrow = "  " if d == 0 else (" ▲" if d > 0 else " ▼")
        print(f"  {k:16} {m['baseline'][k]:>8} {m['candidate'][k]:>10} {d:>+8}{arrow}")
    print("-" * 78)
    print(f"  findings: +{s['added_findings']} added  "
          f"-{s['removed_findings']} removed  ~{s['changed_findings']} changed  "
          f"(total {s['finding_count_delta']:+d})")
    print(f"  severity changes: {s['severity_changes']}   "
          f"confidence changes: {s['confidence_changes']}   "
          f"recommendation changes: {s['recommendation_changes']}")
    print(f"  runtime delta: {s['runtime_delta_s']:+}s   "
          f"request delta: {s['request_count_delta']:+d}")
    if report["flags"]:
        print("-" * 78)
        order = {"suspicious": 0, "regression": 1, "warning": 2, "info": 3}
        for fl in sorted(report["flags"], key=lambda x: order.get(x["level"], 9)):
            tag = {"suspicious": "SUSPECT", "regression": "REGRESS",
                   "warning": "WARN", "info": "info"}[fl["level"]]
            print(f"  [{tag:7}] {fl['message']}")
    print()


# ==========================================================================
# CLI
# ==========================================================================
def _cmd_snapshot(args) -> int:
    if args.ref:
        return _snapshot_via_worktree(args)
    root = os.path.abspath(args.root) if args.root else _DEFAULT_ROOT
    snap = build_snapshot(root, args.label, args.only)
    path = save_snapshot(snap, args.out or args.label)
    m = snap["metrics"]
    print(f"snapshot '{args.label}' -> {path}")
    print(f"  fixtures={snap['totals']['fixtures']} findings={snap['totals']['findings']} "
          f"precision={m['precision']} recall={m['recall']} "
          f"fp_rate={m['fp_rate']} runtime={snap['totals']['runtime_s']}s")
    return 0


def _snapshot_via_worktree(args) -> int:
    tmp = tempfile.mkdtemp(prefix="regr_wt_")
    try:
        r = subprocess.run(["git", "-C", _DEFAULT_ROOT, "worktree", "add", "--detach", tmp, args.ref],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"git worktree add failed: {r.stderr.strip()}")
        # Run THIS harness against the checked-out version's code + fixtures.
        snap = build_snapshot(tmp, args.label, args.only)
        snap["git"] = _git_info(tmp)
        snap["git"]["ref"] = args.ref
        path = save_snapshot(snap, args.out or args.label)
        m = snap["metrics"]
        print(f"snapshot '{args.label}' @ {args.ref} -> {path}")
        print(f"  fixtures={snap['totals']['fixtures']} findings={snap['totals']['findings']} "
              f"precision={m['precision']} recall={m['recall']} fp_rate={m['fp_rate']}")
        return 0
    finally:
        subprocess.run(["git", "-C", _DEFAULT_ROOT, "worktree", "remove", "--force", tmp],
                       capture_output=True, text=True)


def _cmd_diff(args) -> int:
    base = load_snapshot(args.baseline)
    cand = load_snapshot(args.candidate)
    report = diff_snapshots(base, cand)
    os.makedirs(_REPORT_DIR, exist_ok=True)
    out = args.json or os.path.join(
        _REPORT_DIR, f"{base['label']}__vs__{cand['label']}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, ensure_ascii=False, sort_keys=True)
    render_diff(report)
    print(f"  machine-readable report: {out}\n")
    if args.fail_on_suspicious and report["summary"]["suspicious"]:
        return 2
    return 0


def _cmd_list(args) -> int:
    if not os.path.isdir(_SNAP_DIR):
        print("no snapshots yet")
        return 0
    for f in sorted(os.listdir(_SNAP_DIR)):
        if f.endswith(".json"):
            snap = load_snapshot(f[:-5])
            m = snap["metrics"]
            print(f"  {f[:-5]:20} findings={snap['totals']['findings']:>3} "
                  f"precision={m['precision']} recall={m['recall']} "
                  f"({snap['created_at']})")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Regression + differential testing framework.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("snapshot", help="record a snapshot of all fixtures")
    sp.add_argument("--label", required=True)
    sp.add_argument("--only", nargs="*", default=[])
    sp.add_argument("--ref", help="git ref to snapshot (uses a temp worktree)")
    sp.add_argument("--root", help="marketplace root to snapshot (advanced)")
    sp.add_argument("--out", help="explicit output path")
    sp.set_defaults(fn=_cmd_snapshot)

    dp = sub.add_parser("diff", help="compare two snapshots")
    dp.add_argument("baseline")
    dp.add_argument("candidate")
    dp.add_argument("--json", help="explicit report output path")
    dp.add_argument("--fail-on-suspicious", action="store_true",
                    help="exit non-zero if any suspicious regression is flagged")
    dp.set_defaults(fn=_cmd_diff)

    lp = sub.add_parser("list", help="list stored snapshots")
    lp.set_defaults(fn=_cmd_list)

    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
