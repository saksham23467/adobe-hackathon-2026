"""Finding reconciliation layer.

The orchestrator does NOT blindly concatenate skill outputs. After compilation,
every finding passes through this layer, which:

  * enriches each finding for a non-expert (category, affected_urls, sources,
    evidence_type);
  * MERGES overlapping findings — when several skills detect the *same underlying
    problem* on the same page(s), they collapse into one finding that preserves
    the strongest evidence and records every contributing source (so the
    recommendation engine never emits duplicate fixes);
  * RESOLVES contradictions — when a skill FAILs a check but another skill's
    PASS observation disagrees about the same problem on the same URL, the merged
    finding keeps the defect but *lowers confidence* and records the
    disagreement. It never fabricates certainty in either direction;
  * RECALCULATES confidence (corroboration raises, contradiction lowers) and
    SEVERITY (always capped by the final confidence).

Deterministic: inputs are sorted, tie-breaks are total, no randomness.
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List, Set, Tuple

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_LIB = os.path.join(_ROOT, "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from audit_common.schema import (  # noqa: E402
    Confidence,
    Finding,
    Observation,
    Severity,
    Status,
)
from audit_common.semantics import apply_severity_gate  # noqa: E402


# --------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------
# Groups of check_ids that describe the SAME underlying problem across skills.
# Findings in the same group whose affected URLs intersect are merged.
_OVERLAP_GROUP = {
    # Content only reachable after an interaction (click/tab/accordion).
    "render.content_behind_interaction": "content_hidden_behind_interaction",
    "fact.behind_interaction": "content_hidden_behind_interaction",
    "engagement.content_hidden_interaction": "content_hidden_behind_interaction",
    # Meaningful text/facts baked into images instead of HTML text.
    "render.text_as_image": "text_locked_in_images",
    "fact.facts_locked_in_non_text": "text_locked_in_images",
    "fact.informational_images_no_alt": "text_locked_in_images",
    # Primary content missing from the initial HTML response.
    "render.primary_content_js_only": "content_missing_from_initial_html",
    "render.empty_initial_shell": "content_missing_from_initial_html",
    "render.class_js_gap": "content_missing_from_initial_html",
    "render.headings_js_only": "content_missing_from_initial_html",
    # Structured data disagrees with what the page visibly says.
    "identity.schema_visible_mismatch": "structured_vs_visible_mismatch",
    "fact.contradictory_values": "structured_vs_visible_mismatch",
}

# Friendly category per concern (for non-experts).
_CATEGORY = {
    "crawl_access": "Discoverability & Access",
    "render": "Rendering & Readability",
    "fact_access": "Fact Extractability",
    "identity": "Structured Identity",
    "freshness": "Freshness & Trust",
    "engagement": "Engagement & UX",
}

# Friendly evidence type per detection method.
_EVIDENCE_TYPE = {
    "http": "initial HTML",
    "render": "rendered DOM",
    "schema": "structured data",
    "robots": "robots.txt",
    "sitemap": "sitemap",
    "ocr": "image / OCR",
    "external": "external source",
    "header": "HTTP header",
}

_CONF_ORDER = [Confidence.LOW, Confidence.MEDIUM, Confidence.HIGH]


def _bump(c: Confidence) -> Confidence:
    i = _CONF_ORDER.index(c)
    return _CONF_ORDER[min(i + 1, len(_CONF_ORDER) - 1)]


def _lower(c: Confidence) -> Confidence:
    i = _CONF_ORDER.index(c)
    return _CONF_ORDER[max(i - 1, 0)]


def _cap(sev: Severity, conf: Confidence) -> Severity:
    cap = {Confidence.HIGH: Severity.CRITICAL,
           Confidence.MEDIUM: Severity.HIGH,
           Confidence.LOW: Severity.MEDIUM}[conf]
    return sev if sev.rank <= cap.rank else cap


def _group_key(check_id: str) -> str:
    return _OVERLAP_GROUP.get(check_id, check_id)


# --------------------------------------------------------------------------
# URL extraction
# --------------------------------------------------------------------------
def _finding_urls(f: Finding) -> Set[str]:
    urls: Set[str] = set()
    ev = f.evidence or {}
    scope = ev.get("scope", "")
    if isinstance(scope, str):
        for part in scope.split(","):
            part = part.strip()
            if part.startswith("http"):
                urls.add(part)
    metric = ev.get("metric", {}) or {}
    for key in ("urls", "pages", "examples"):
        v = metric.get(key)
        if isinstance(v, list):
            urls.update(str(x) for x in v if str(x).startswith("http"))
    for key in ("url", "page"):
        v = metric.get(key)
        if isinstance(v, str) and v.startswith("http"):
            urls.add(v)
    return urls


def _urls_overlap(a: Set[str], b: Set[str]) -> bool:
    if not a and not b:
        return True  # both site-wide -> same scope
    return bool(a & b)


# --------------------------------------------------------------------------
# Enrichment
# --------------------------------------------------------------------------
def _enrich(f: Finding) -> None:
    f.category = _CATEGORY.get(f.concern.value, f.concern.value)
    method = (f.evidence or {}).get("method", "")
    f.evidence_type = _EVIDENCE_TYPE.get(method, method or "analysis")
    if not f.affected_urls:
        f.affected_urls = sorted(_finding_urls(f))
    if not f.sources:
        f.sources = [{
            "check_id": f.check_id,
            "concern": f.concern.value,
            "method": method,
        }]


# --------------------------------------------------------------------------
# Main entry
# --------------------------------------------------------------------------
def reconcile(
    findings: List[Finding], observations: List[Observation], catalog
) -> Tuple[List[Finding], Dict[str, int]]:
    """Merge overlaps, resolve contradictions, recompute confidence/severity.

    Returns the reconciled findings and a small stats dict for report meta.
    """
    for f in findings:
        _enrich(f)

    # Index PASS observations by overlap group for contradiction detection.
    pass_index: Dict[str, List[Tuple[Set[str], str]]] = {}
    for obs in observations:
        if obs.status != Status.PASS:
            continue
        gk = _group_key(obs.check_id)
        pass_index.setdefault(gk, []).append((set(obs.urls), obs.check_id))

    # Deterministic processing order.
    def canon_key(f: Finding):
        return (_group_key(f.check_id), -f.severity.rank, -f.confidence.rank,
                f.check_id, f.id)

    ordered = sorted(findings, key=canon_key)

    # Greedy grouping into merge-clusters.
    clusters: List[List[Finding]] = []
    for f in ordered:
        gk = _group_key(f.check_id)
        urls = _finding_urls(f)
        placed = False
        for cluster in clusters:
            c0 = cluster[0]
            if _group_key(c0.check_id) != gk:
                continue
            if any(_urls_overlap(urls, _finding_urls(m)) for m in cluster):
                cluster.append(f)
                placed = True
                break
        if not placed:
            clusters.append([f])

    merged_count = 0
    contested_count = 0
    result: List[Finding] = []

    for cluster in clusters:
        canonical = cluster[0]  # highest severity/confidence by sort order
        if len(cluster) > 1:
            merged_count += len(cluster) - 1
            _merge_into(canonical, cluster[1:])

        # Contradiction: a PASS from a *different* check in the same group,
        # overlapping this finding's URLs, disputes the defect.
        gk = _group_key(canonical.check_id)
        my_urls = set(canonical.affected_urls) or _finding_urls(canonical)
        contradictors = []
        for p_urls, p_check in pass_index.get(gk, []):
            if p_check == canonical.check_id and gk == canonical.check_id:
                continue  # same check passing elsewhere is not a contradiction
            if _urls_overlap(my_urls, p_urls):
                contradictors.append(p_check)
        if contradictors:
            contested_count += 1
            _apply_contradiction(canonical, sorted(set(contradictors)))

        # Final severity is always capped by the (possibly changed) confidence
        # and by semantic relevance (a true-but-off-topic observation cannot
        # read as high/critical).
        spec = catalog.get(canonical.check_id) if catalog else None
        base_sev = spec.default_severity if spec else canonical.severity
        if base_sev.rank < canonical.severity.rank:
            base_sev = canonical.severity
        sev, conf = apply_severity_gate(
            base_sev, canonical.confidence,
            getattr(canonical, "semantic_relevance", "high") or "high",
        )
        canonical.severity = sev
        canonical.confidence = conf
        result.append(canonical)

    # Stable output order: severity desc, confidence desc, category, check, id.
    result.sort(key=lambda f: (-f.severity.rank, -f.confidence.rank,
                               f.category, f.check_id, f.id))
    return result, {"merged": merged_count, "contested": contested_count,
                    "input": len(findings), "output": len(result)}


def _merge_into(canonical: Finding, others: List[Finding]) -> None:
    concerns = {canonical.concern.value}
    urls: Set[str] = set(canonical.affected_urls)
    source_seen = {s["check_id"] for s in canonical.sources}
    cross_checks: List[str] = []   # different check_ids -> true corroboration
    same_check = 0                 # same check_id -> duplicate consolidation
    extra_values: Dict[str, Set[str]] = {}

    for o in others:
        concerns.add(o.concern.value)
        urls.update(o.affected_urls)
        if o.check_id != canonical.check_id:
            cross_checks.append(o.check_id)
        else:
            same_check += 1
        for s in o.sources:
            if s["check_id"] not in source_seen:
                source_seen.add(s["check_id"])
                canonical.sources.append(s)
        if o.id not in canonical.related_findings:
            canonical.related_findings.append(o.id)
        om = (o.evidence or {}).get("metric", {}) or {}
        for k in ("page_class", "destination"):
            if om.get(k):
                extra_values.setdefault(k, set()).add(str(om[k]))

    canonical.affected_urls = sorted(urls)
    ev = canonical.evidence or {}
    cm = ev.setdefault("metric", {})
    # Preserve distinguishing detail from consolidated duplicates.
    for k, vals in extra_values.items():
        if cm.get(k):
            vals.add(str(cm[k]))
        cm[k + "s"] = sorted(vals)

    notes: List[str] = []
    if cross_checks:
        methods = {
            (s.get("method") or "")
            for s in (canonical.sources or [])
            if s.get("method")
        }
        # Only bump when evidence methods actually differ (HTTP vs render vs schema).
        if len(concerns) >= 2 and len(methods) >= 2:
            canonical.confidence = _bump(canonical.confidence)
        cm["corroborated_by"] = sorted(set(cross_checks))
        notes.append(
            f"Corroborated by {len(set(cross_checks))} independent signal(s): "
            f"{', '.join(sorted(set(cross_checks)))}."
        )
    if same_check:
        notes.append(f"Also affects {same_check} more location(s)/case(s) of the same issue.")
    if notes:
        ev["sentence"] = (ev.get("sentence", "") + " " + " ".join(notes)).strip()
    canonical.evidence = ev


def _apply_contradiction(f: Finding, contradictors: List[str]) -> None:
    f.contested = True
    f.confidence = _lower(f.confidence)
    ev = f.evidence or {}
    who = ", ".join(contradictors)
    ev["sentence"] = (ev.get("sentence", "")
                      + f" Contested: {who} found no problem on the same page — "
                      "confidence lowered to reflect the disagreement.").strip()
    ev.setdefault("metric", {})["contested_by"] = contradictors
    f.sources.append({"check_id": who, "concern": "contradiction",
                      "method": "pass_observation"})
    f.evidence = ev


# --------------------------------------------------------------------------
# Non-expert summary helpers
# --------------------------------------------------------------------------
def human_summary(findings: List[Finding]) -> Dict[str, object]:
    by_category: Dict[str, int] = {}
    for f in findings:
        by_category[f.category or f.concern.value] = \
            by_category.get(f.category or f.concern.value, 0) + 1
    crit = sum(1 for f in findings if f.severity == Severity.CRITICAL)
    high = sum(1 for f in findings if f.severity == Severity.HIGH)
    med = sum(1 for f in findings if f.severity == Severity.MEDIUM)
    if not findings:
        headline = ("No blocking AI-readiness defects were detected on the pages "
                    "sampled. See proactive recommendations to strengthen the site.")
    else:
        parts = []
        if crit:
            parts.append(f"{crit} critical")
        if high:
            parts.append(f"{high} high")
        if med:
            parts.append(f"{med} medium")
        # Only point the reader at "do first" markers when some actually exist;
        # otherwise the headline promised a marker the report never set (as
        # happened on sites whose top finding fell just below the do-first bar).
        has_do_first = any(
            getattr(f.suggested_action, "do_first", False)
            for f in findings if f.suggested_action is not None
        )
        lead = ("Start with the items marked 'do first'." if has_do_first
                else "Start with the highest-ranked items in top_priorities.")
        headline = f"{len(findings)} issue(s) found ({', '.join(parts)}). {lead}"
    return {"headline": headline, "by_category": by_category}
