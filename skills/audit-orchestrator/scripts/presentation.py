"""Report presentation layer.

Turns the reconciled, recommendation-attached report into a professional
AI-readiness diagnostic. This layer NEVER invents findings or changes severity /
confidence — it only re-expresses what the detectors and reconciler already
established, adding:

  * deterministic scores (overall / discoverability / engagement);
  * an executive summary in plain, non-marketing language;
  * a strict separation of OBSERVED FACT vs INFERRED PROBLEM vs RECOMMENDATION,
    so an inference is never presented as an observation;
  * a "why this matters to AI systems" explanation per finding;
  * reader-facing sections: confidence_summary, top_priorities,
    proactive_opportunities, crawl_summary, coverage_summary.

Pure, deterministic, standard-library only. Operates on the plain report dict
(post new_report + human_summary) so the required Adobe schema stays intact and
every added key is an extension.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

# --------------------------------------------------------------------------
# Vocabularies
# --------------------------------------------------------------------------
# The five discoverability concerns describe whether an AI system can find,
# fetch, read, extract and trust the brand. Engagement is scored separately
# because it governs what happens *after* a human arrives from an AI answer.
_DISCOVERABILITY_CONCERNS = {
    "crawl_access",
    "render",
    "fact_access",
    "identity",
    "freshness",
}
_ENGAGEMENT_CONCERNS = {"engagement"}

# Why each class of defect matters specifically to AI systems (retrieval +
# extraction pipelines), stated as mechanism, not marketing.
_WHY_AI: Dict[str, str] = {
    "crawl_access": (
        "AI assistants can only cite pages they are permitted to fetch and able "
        "to discover. When access is blocked or discovery fails, the content is "
        "invisible to retrieval regardless of how good it is."
    ),
    "render": (
        "Most AI fetchers read the initial HTML response and do not execute "
        "JavaScript. Content that appears only after client-side rendering is "
        "usually not captured, so it cannot be summarized or quoted."
    ),
    "fact_access": (
        "Assistants answer by extracting and quoting concrete facts. Facts that "
        "are vague, locked inside images, or not clearly tied to the brand "
        "cannot be lifted into an answer with confidence."
    ),
    "identity": (
        "Structured data lets a machine resolve which real-world entity a page "
        "describes. Missing, broken, or contradictory markup causes the brand to "
        "be misattributed or merged with a different entity."
    ),
    "freshness": (
        "Assistants weigh recency and agreement across sources. Stale, "
        "undated, or self-contradictory claims lower the confidence with which a "
        "model will state something about the brand."
    ),
    "engagement": (
        "After a person follows an AI answer to the site, unclear orientation, "
        "confusing navigation, or dead ends cause them to leave before the visit "
        "achieves anything."
    ),
}

# Connectives that separate an observed clause from its implication, so we can
# quote only the measured part as "observed fact".
_CONNECTIVES = (" so that ", " so ", ", so ", " — ", " which means ", " because ")

# Diminishing, relevance-weighted penalties. One critical finding must not
# zero a site that is otherwise readable (Adobe-sized sites have many
# medium/high nits that would otherwise sum past 100).
_SEV_PENALTY = {"critical": 22.0, "high": 11.0, "medium": 4.0}
_CONF_WEIGHT = {"high": 1.0, "medium": 0.75, "low": 0.5}
_REL_WEIGHT = {"high": 1.0, "medium": 0.55, "low": 0.2, "none": 0.0}
_PENALTY_DECAY = 0.62
_SCORE_FLOOR_CRAWLED = 18
_ZERO_SCORE_CHECKS = {
    "access.homepage_unreachable",
    "access.global_disallow_all",
}


# --------------------------------------------------------------------------
# Scoring (deterministic penalty model)
# --------------------------------------------------------------------------
def _penalty(f: Dict[str, Any]) -> float:
    base = _SEV_PENALTY.get(f.get("severity", "medium"), 4.0)
    w = _CONF_WEIGHT.get(f.get("confidence", "medium"), 0.75)
    rel = _REL_WEIGHT.get(f.get("semantic_relevance") or "high", 1.0)
    # A contested finding (skills disagreed) is deliberately discounted; we are
    # less sure the defect is real, so it should weigh less on the score.
    contested = 0.6 if f.get("contested") else 1.0
    return base * w * contested * rel


def _coverage_cap(report: Dict[str, Any]) -> int:
    """Never grade excellent when the audit could not actually see the site."""
    cov = report.get("coverage", {}) or {}
    sampled = int(cov.get("pages_sampled") or 0)
    rendered = int(cov.get("rendered") or 0)
    limitations = report.get("limitations") or []
    cap = 100
    if sampled < 2:
        cap = min(cap, 82)
    if sampled < 3:
        cap = min(cap, 88)
    render_limited = any(
        "render" in (l.get("check_id") or "") and "not" in (l.get("reason") or "").lower()
        for l in limitations
    )
    if rendered == 0 and render_limited:
        cap = min(cap, 80)
    skipped = sum(
        1 for l in limitations
        if "time budget" in (l.get("reason") or "") or "skill error" in (l.get("reason") or "")
    )
    if skipped:
        cap = min(cap, 75)
    return cap


def _score_floor(report: Dict[str, Any], findings: List[Dict[str, Any]]) -> int:
    """Floor for a reachable, sampled site. Access blockers score 0 instead."""
    if any(f.get("check_id") in _ZERO_SCORE_CHECKS for f in findings):
        return 0
    sampled = int((report.get("coverage") or {}).get("pages_sampled") or 0)
    if sampled >= 2:
        return _SCORE_FLOOR_CRAWLED
    return 0


def _score(findings: List[Dict[str, Any]], cap: int = 100, floor: int = 0) -> int:
    """Sum penalties with diminishing returns so volume cannot zero a site.

    The first (largest) penalty counts in full; each further finding is
    decayed. A crawled, reachable site never scores below ``floor`` unless
    a zero-score access blocker is present (those return 0).
    """
    if any(f.get("check_id") in _ZERO_SCORE_CHECKS for f in findings):
        return 0
    penalties = sorted((_penalty(f) for f in findings), reverse=True)
    total = 0.0
    decay = 1.0
    for p in penalties:
        total += p * decay
        decay *= _PENALTY_DECAY
    return max(floor, min(cap, round(100.0 - total)))


def _grade(score: int) -> str:
    if score >= 90:
        return "excellent"
    if score >= 75:
        return "good"
    if score >= 55:
        return "fair"
    if score >= 35:
        return "poor"
    return "critical"


# --------------------------------------------------------------------------
# Observed vs inferred separation
# --------------------------------------------------------------------------
def _split_observed(sentence: str) -> Tuple[str, str]:
    """Split a detector sentence into (observed clause, implication clause).

    Detector sentences follow an "OBSERVATION <connective> IMPLICATION" shape.
    The left side is what was literally measured; the right side is the
    consequence. If no connective is present the whole sentence is treated as
    observed and the implication is empty.
    """
    s = (sentence or "").strip()
    if not s:
        return "", ""
    lowered = s.lower()
    cut = None
    for c in _CONNECTIVES:
        idx = lowered.find(c)
        if idx != -1 and (cut is None or idx < cut[0]):
            cut = (idx, len(c))
    if cut is None:
        return s, ""
    left = s[: cut[0]].strip().rstrip(",;")
    right = s[cut[0] + cut[1] :].strip()
    return left, right


def _measurement(evidence: Dict[str, Any]) -> str:
    metric = evidence.get("metric") or {}
    if not isinstance(metric, dict) or not metric:
        return ""
    parts = [f"{k}={v}" for k, v in metric.items() if not isinstance(v, (dict, list))]
    return "; ".join(parts)


def _impact_level(sa: Dict[str, Any], severity: str) -> str:
    val = float(sa.get("impact") or 0.0)
    if val >= 0.8 or severity == "critical":
        return "high"
    if val >= 0.55 or severity == "high":
        return "medium"
    return "low"


# --------------------------------------------------------------------------
# Finding enrichment
# --------------------------------------------------------------------------
def _evidence_dict(f: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize evidence to a dict for enrichment.

    The Adobe handout requires ``findings[].evidence`` to be a string. We keep
    the structured payload on ``evidence_detail`` and publish the sentence as
    ``evidence`` at the end of enrich_report.
    """
    ev = f.get("evidence")
    if isinstance(ev, dict):
        return ev
    detail = f.get("evidence_detail")
    if isinstance(detail, dict) and detail:
        out = dict(detail)
        if isinstance(ev, str) and ev and not out.get("sentence"):
            out["sentence"] = ev
        return out
    return {"sentence": str(ev or "")}


def _publish_handout_evidence(f: Dict[str, Any]) -> None:
    """Emit the required string ``evidence`` plus structured ``evidence_detail``."""
    ev = f.get("evidence")
    if not isinstance(ev, dict):
        ev = _evidence_dict(f)
    f["evidence_detail"] = ev
    f["evidence"] = (ev.get("observed_fact") or ev.get("sentence") or "").strip()


def _enrich_finding(f: Dict[str, Any]) -> None:
    concern = f.get("concern", "")
    ev = _evidence_dict(f)
    f["evidence"] = ev

    observed, implication = _split_observed(ev.get("sentence", ""))
    measurement = _measurement(ev)

    # Rewrite the evidence block so the three epistemic tiers are explicit and an
    # inference is never labeled as an observation.
    ev["observed_fact"] = observed or ev.get("sentence", "")
    if measurement:
        ev["measurement"] = measurement
    ev["basis"] = "measured" if measurement else "observed"

    sa = f.get("suggested_action") or {}
    # Inferred problem = the finding title (the defect class we conclude),
    # optionally sharpened by the detector's implication clause.
    inferred = f.get("title", "")
    if implication:
        inferred = f"{inferred}: {implication[0].lower()}{implication[1:]}"
    f["inferred_problem"] = inferred

    f["impact"] = {
        "level": _impact_level(sa, f.get("severity", "medium")),
        "axis": "engagement" if concern in _ENGAGEMENT_CONCERNS else "AI discoverability",
        "expected_effect": sa.get("expected_effect", ""),
    }

    f["why_it_matters_to_ai"] = _WHY_AI.get(concern, "")

    # Recommendation, expressed as a compact "implementation" block distinct from
    # the observed/inferred tiers above.
    if isinstance(sa, dict):
        implementation = {
            "where": sa.get("where", ""),
            "steps": list(sa.get("steps", [])),
        }
        if sa.get("patch"):
            implementation["patch"] = sa["patch"]
        sa["implementation"] = implementation
        # Keep the required summary/priority; ensure expected_effect present.
        sa.setdefault("expected_effect", "")


# --------------------------------------------------------------------------
# Report-level sections
# --------------------------------------------------------------------------
def _confidence_summary(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    by = {"high": 0, "medium": 0, "low": 0}
    contested = 0
    for f in findings:
        by[f.get("confidence", "medium")] = by.get(f.get("confidence", "medium"), 0) + 1
        if f.get("contested"):
            contested += 1
    if not findings:
        note = "No defects were detected, so there is nothing to be confident or unsure about."
    elif contested:
        note = (
            f"{contested} finding(s) are contested: at least two checks disagreed, "
            "so confidence and severity were lowered rather than fabricated."
        )
    else:
        note = (
            "Confidence reflects how directly each defect was measured; severity "
            "is capped so a low-confidence issue can never read as critical."
        )
    return {"by_confidence": by, "contested": contested, "note": note}


def _top_priorities(findings: List[Dict[str, Any]], limit: int = 5) -> List[Dict[str, Any]]:
    def rank_of(f: Dict[str, Any]) -> int:
        sa = f.get("suggested_action") or {}
        return int(sa.get("rank") or 9999)

    ordered = sorted(findings, key=rank_of)
    out: List[Dict[str, Any]] = []
    for f in ordered[:limit]:
        sa = f.get("suggested_action") or {}
        out.append(
            {
                "id": f.get("id"),
                "title": f.get("title"),
                "category": f.get("category", f.get("concern", "")),
                "severity": f.get("severity"),
                "confidence": f.get("confidence"),
                "do_first": bool(sa.get("do_first")),
                "action": sa.get("summary", ""),
                "expected_effect": sa.get("expected_effect", ""),
            }
        )
    return out


def _proactive_opportunities(beyond: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for b in beyond:
        implementation = {
            "where": b.get("where", ""),
            "steps": list(b.get("steps", [])),
        }
        if b.get("patch"):
            implementation["patch"] = b["patch"]
        out.append(
            {
                "id": b.get("id", ""),
                "title": b.get("summary", ""),
                "rationale": b.get("why", ""),
                "priority": b.get("priority", "medium"),
                "effort": b.get("effort", "medium"),
                "implementation": implementation,
                "expected_effect": b.get("expected_effect", ""),
            }
        )
    return out


def _crawl_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    cov = report.get("coverage", {}) or {}
    meta = report.get("meta", {}) or {}
    return {
        "pages_discovered": cov.get("pages_discovered", 0),
        "pages_sampled": cov.get("pages_sampled", 0),
        "pages_rendered": cov.get("rendered", 0),
        "page_classes": cov.get("page_classes", []),
        "external_lookups": cov.get("external_lookups", 0),
        "robots_respected": bool(cov.get("robots_respected", meta.get("robots_respected", False))),
        "renderer_used": bool(meta.get("renderer")),
        "runtime_s": meta.get("runtime_s"),
    }


def _coverage_summary(report: Dict[str, Any]) -> Dict[str, Any]:
    cov = report.get("coverage", {}) or {}
    discovered = cov.get("pages_discovered", 0)
    sampled = cov.get("pages_sampled", 0)
    rendered = cov.get("rendered", 0)
    classes = cov.get("page_classes", []) or []
    class_str = ", ".join(str(c) for c in classes) if classes else "n/a"
    description = (
        f"Sampled {sampled} of {discovered} discovered page(s) across "
        f"{len(classes)} page class(es) ({class_str}); {rendered} rendered for "
        "initial-HTML vs rendered-DOM comparison."
    )
    return {
        "sampled": sampled,
        "discovered": discovered,
        "rendered": rendered,
        "page_classes": classes,
        "description": description,
    }


def _executive_summary(
    report: Dict[str, Any],
    scores: Dict[str, int],
    findings: List[Dict[str, Any]],
) -> str:
    site = report.get("site", "the site")
    summ = report.get("summary", {})
    total = summ.get("total_findings", 0)
    crit, high, med = summ.get("critical", 0), summ.get("high", 0), summ.get("medium", 0)
    by_cat = summ.get("by_category", {}) or {}

    overall = scores["overall_score"]
    if total == 0:
        return (
            f"{site} scored {overall}/100 for AI readiness "
            f"(discoverability {scores['discoverability_score']}, "
            f"engagement {scores['engagement_score']}). No blocking defects were "
            "found on the sampled pages; the proactive opportunities list ways to "
            "strengthen how AI systems find, read, and trust the brand."
        )

    top_cats = sorted(by_cat.items(), key=lambda kv: (-kv[1], kv[0]))[:2]
    cats_phrase = " and ".join(c for c, _ in top_cats) if top_cats else "several areas"
    sev_bits = []
    if crit:
        sev_bits.append(f"{crit} critical")
    if high:
        sev_bits.append(f"{high} high")
    if med:
        sev_bits.append(f"{med} medium")
    sev_phrase = ", ".join(sev_bits)

    # The single most consequential lever, from the ranked top priority.
    first = _top_priorities(findings, 1)
    lead = first[0]["action"] if first else ""

    return (
        f"{site} scored {overall}/100 for AI readiness "
        f"(discoverability {scores['discoverability_score']}, "
        f"engagement {scores['engagement_score']}). The audit found {total} "
        f"issue(s) ({sev_phrase}), concentrated in {cats_phrase}. These reduce how "
        "reliably AI systems can discover, read, and quote the brand. "
        + (f"Start with: {lead}" if lead else "See top_priorities for the recommended order.")
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def enrich_report(report: Dict[str, Any]) -> Dict[str, Any]:
    """Augment a valid report dict with the professional diagnostic sections.

    Additive and deterministic: required fields are untouched; every finding
    keeps its existing keys and gains observed/inferred/why-it-matters context.
    """
    findings = report.get("findings", []) or []
    for f in findings:
        _enrich_finding(f)
        _publish_handout_evidence(f)

    disc = [f for f in findings if f.get("concern") in _DISCOVERABILITY_CONCERNS]
    eng = [f for f in findings if f.get("concern") in _ENGAGEMENT_CONCERNS]
    cap = _coverage_cap(report)
    scores = {
        "overall_score": _score(findings, cap, _score_floor(report, findings)),
        "discoverability_score": _score(disc, cap, _score_floor(report, disc)),
        "engagement_score": _score(eng, cap, _score_floor(report, eng)),
    }

    report["overall_score"] = scores["overall_score"]
    report["overall_grade"] = _grade(scores["overall_score"])
    report["discoverability_score"] = scores["discoverability_score"]
    report["engagement_score"] = scores["engagement_score"]

    report["executive_summary"] = _executive_summary(report, scores, findings)
    report["confidence_summary"] = _confidence_summary(findings)
    report["top_priorities"] = _top_priorities(findings)
    report["proactive_opportunities"] = _proactive_opportunities(
        report.get("beyond_defect_recommendations", []) or []
    )
    report["crawl_summary"] = _crawl_summary(report)
    report["coverage_summary"] = _coverage_summary(report)

    # A small legend so a non-expert reader knows how to read each finding.
    report["reading_guide"] = {
        "observed_fact": "What the audit literally measured or saw in the page.",
        "inferred_problem": "The defect concluded from that observation (an inference).",
        "recommendation": "The suggested_action.implementation — a proposed fix, not a fact.",
        "why_it_matters_to_ai": "How the defect affects AI retrieval and extraction.",
    }
    return report
