"""recommendation-engine: turn findings into concrete, prioritized actions.

`attach_actions(findings, ctx) -> (findings, beyond_defect_recommendations)`

For every finding we synthesize a `SuggestedAction` that answers the eight
required questions:

  1. what   — exactly what should change            (template.what)
  2. where  — where it should change                (template.where)
  3. why    — why it solves the *detected mechanism* (template.why)
  4. how    — implementation steps                  (template.how / patch)
  5. evidence — the observed evidence               (finding.evidence)
  6. impact — expected impact                       (template.effect + weight)
  7. effort — implementation difficulty             (template.effort)
  8. first  — sequencing                            (priority_score / rank / do_first)

Prioritization is a multi-factor model — impact × severity × confidence × scope ×
effort × dependency — deliberately NOT a `high severity -> high priority` map.
Duplicate recommendations (same fix across pages) are de-duplicated, and a small
set of genuinely-useful proactive recommendations is added only where relevant.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional, Tuple

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_SCRIPTS, "..", "..", ".."))
_LIB = os.path.join(_ROOT, "lib")
for _p in (_SCRIPTS, _LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from audit_common.schema import Finding, Severity, SuggestedAction  # noqa: E402
from templates import CONCERN_IMPACT, TEMPLATES  # noqa: E402


# --------------------------------------------------------------------------
# Prioritization factor weights
# --------------------------------------------------------------------------
_SEV_W = {Severity.CRITICAL: 1.0, Severity.HIGH: 0.75, Severity.MEDIUM: 0.5}
_CONF_W = {"high": 1.0, "medium": 0.75, "low": 0.5}
_EFFORT_W = {"low": 1.0, "medium": 0.7, "high": 0.45}

# Findings that gate everything downstream (machine cannot even read the site).
_ACCESS_BLOCKERS = {
    "access.ai_ua_blocked", "access.our_ua_blocked", "access.global_noindex",
    "access.global_disallow_all", "access.homepage_unreachable",
    "access.class_noindex", "access.important_noindex", "access.robots_unreachable",
}
# Findings that gate content-reading (facts/identity/freshness rely on rendered
# content actually existing in the HTML).
_RENDER_BLOCKERS = {"render.primary_content_js_only", "render.empty_initial_shell",
                    "render.class_js_gap"}

# Checks whose effect is inherently site-wide (max scope regardless of counts).
_SITE_WIDE = {
    "access.ai_ua_blocked", "access.our_ua_blocked", "access.global_noindex",
    "access.global_disallow_all", "access.robots_missing", "access.robots_unreachable",
    "access.sitemap_missing", "access.sitemap_invalid", "access.homepage_unreachable",
    "identity.no_organization_schema", "identity.duplicate_entity",
    "identity.entity_ambiguous",
}

_DEP_PENALTY = 0.6  # multiplier applied while a prerequisite is unresolved


# --------------------------------------------------------------------------
# Scope
# --------------------------------------------------------------------------
def _scope_mult(f: Finding) -> Tuple[float, str]:
    if f.check_id in _SITE_WIDE:
        return 1.0, "site_wide"
    if ".class_" in f.check_id or "_class" in f.check_id:
        return 0.8, "class_wide"
    metric = (f.evidence or {}).get("metric", {}) or {}
    n = 0
    for key in ("total", "missing_count", "count", "dead_end_count",
                "empty_link_count", "vague_link_count", "blocks_zoom_count"):
        if isinstance(metric.get(key), int):
            n = max(n, metric[key])
    for key in ("pages", "examples", "urls"):
        v = metric.get(key)
        if isinstance(v, list):
            n = max(n, len(v))
    scope = (f.evidence or {}).get("scope", "")
    if isinstance(scope, str) and scope.count(",") >= 2:
        n = max(n, scope.count(",") + 1)
    if n >= 3:
        return min(1.0, 0.6 + 0.08 * n), "multi_page"
    if n == 2:
        return 0.6, "multi_page"
    return 0.5, "single_page"


# --------------------------------------------------------------------------
# Dependencies
# --------------------------------------------------------------------------
def _blocker_refs(f: Finding, access_blockers: List[Finding],
                  render_blockers: List[Finding]) -> List[Finding]:
    """Return the prerequisite finding objects (by identity, not id)."""
    refs: List[Finding] = []
    if f.concern.value != "crawl_access":
        refs += [b for b in access_blockers if b is not f]
    if f.concern.value in ("fact_access", "identity", "freshness") \
            and f.check_id not in _RENDER_BLOCKERS:
        refs += [b for b in render_blockers if b is not f]
    out: List[Finding] = []
    for b in refs:
        if b not in out:
            out.append(b)
    return out


# --------------------------------------------------------------------------
# Priority score
# --------------------------------------------------------------------------
def _score(f: Finding, tpl: Dict[str, Any], scope: float, has_deps: bool) -> float:
    impact = float(tpl.get("impact") or CONCERN_IMPACT.get(f.concern.value, 0.6))
    sev = _SEV_W[f.severity]
    conf = _CONF_W[f.confidence.value]
    effort = _EFFORT_W.get(tpl.get("effort", "medium"), 0.7)
    dep_mult = _DEP_PENALTY if has_deps else 1.0
    # Effort matters but never dominates: blend so a high-impact/high-effort item
    # is not buried beneath a trivial one.
    effort_blend = 0.55 + 0.45 * effort
    score = 100.0 * impact * sev * conf * scope * effort_blend * dep_mult
    return round(score, 1)


def _priority_bucket(score: float) -> Severity:
    # Decoupled from raw severity on purpose.
    if score >= 65:
        return Severity.CRITICAL
    if score >= 42:
        return Severity.HIGH
    return Severity.MEDIUM


def _evidence_text(f: Finding) -> str:
    ev = f.evidence or {}
    return ev.get("sentence") or ev.get("scope") or ""


def _fallback_template(f: Finding) -> Dict[str, Any]:
    # Specific (not filler): grounded in the finding's own title/mechanism.
    return {
        "what": f"Resolve the detected issue: {f.title}.",
        "where": (f.evidence or {}).get("scope", "") or "the affected pages",
        "why": f"Directly addresses mechanism {f.mechanism} behind '{f.title}'.",
        "how": [f"Remediate the specific condition described: {_evidence_text(f)}"],
        "patch": None,
        "impact": CONCERN_IMPACT.get(f.concern.value, 0.6),
        "effort": "medium",
        "effect": "Removes the detected defect and its downstream impact.",
    }


# --------------------------------------------------------------------------
# Beyond-defect (proactive) recommendations — guarded + gated + de-duplicated.
# --------------------------------------------------------------------------
_BEYOND_DEFECT = [
    {
        "id": "llms_txt",
        "summary": "Publish an llms.txt with canonical brand facts and key URLs.",
        "what": "Add an /llms.txt file summarizing the brand and its key pages.",
        "where": "Site root (/llms.txt).",
        "why": "Gives assistants a concise, authoritative source to quote instead "
               "of reconstructing facts from scattered pages.",
        "steps": ["List the legal name, category, key pages and canonical claims."],
        "patch": "# llms.txt\nName: …\nWhat: …\nKey pages: /products, /pricing, /about",
        "effort": "low",
        "expected_effect": "Assistants get a clean, canonical brand summary.",
        "guards_concern": "crawl_access",
        "requires_class": None,
    },
    {
        "id": "entity_paragraph",
        "summary": "Add a 40-80 word citation-ready entity paragraph on the about page.",
        "what": "Write one plain-text paragraph describing the brand.",
        "where": "The about page, as HTML text.",
        "why": "Provides a clean, quotable summary assistants can lift verbatim, "
               "improving attribution accuracy.",
        "steps": ["State legal name, category, founding, location and what makes the "
                  "brand distinct in one paragraph."],
        "patch": None,
        "effort": "low",
        "expected_effect": "A verbatim-quotable brand description exists.",
        "guards_concern": "identity",
        "requires_class": None,
    },
    {
        "id": "faq_schema",
        "summary": "Add FAQPage structured data for common customer questions.",
        "what": "Mark up the FAQ/help content with FAQPage JSON-LD.",
        "where": "FAQ / support pages.",
        "why": "Turns Q&A content into directly extractable question-answer pairs "
               "assistants can quote.",
        "steps": ["Emit FAQPage with mainEntity Question/Answer nodes matching the "
                  "visible text."],
        "patch": '<script type="application/ld+json">{"@context":"https://schema.org","@type":"FAQPage","mainEntity":[{"@type":"Question","name":"…","acceptedAnswer":{"@type":"Answer","text":"…"}}]}</script>',
        "effort": "low",
        "expected_effect": "Common questions become directly quotable.",
        "guards_concern": "fact_access",
        "requires_class": "faq",
    },
    {
        "id": "article_dates",
        "summary": "Expose accurate datePublished/dateModified on articles.",
        "what": "Add honest publish/modified dates to article markup.",
        "where": "The article/blog template (Article JSON-LD + visible date).",
        "why": "Lets assistants judge recency and prefer current information, "
               "improving trust in time-sensitive claims.",
        "steps": ["Emit Article datePublished and dateModified from real timestamps."],
        "patch": None,
        "effort": "low",
        "expected_effect": "Recency is machine-readable and trustworthy.",
        "guards_concern": "freshness",
        "requires_class": "article",
    },
]


def _page_classes(ctx) -> set:
    try:
        return {getattr(r, "page_class", "") for r in ctx.cache.all()}
    except Exception:
        return set()


def _beyond_defect(findings: List[Finding], ctx) -> List[Dict[str, Any]]:
    present_concerns = {f.concern.value for f in findings}
    classes = _page_classes(ctx)
    out: List[Dict[str, Any]] = []
    seen_summaries: set = set()
    for item in _BEYOND_DEFECT:
        # Suppress if the area already has a live defect (that defect's fix wins).
        if item.get("guards_concern") in present_concerns:
            continue
        # Only surface when genuinely relevant to this site.
        req = item.get("requires_class")
        if req is not None and req not in classes:
            continue
        if item["summary"] in seen_summaries:  # de-dupe proactive items
            continue
        seen_summaries.add(item["summary"])
        entry = {k: v for k, v in item.items()
                 if k not in ("guards_concern", "requires_class")}
        entry.setdefault("priority", "medium")
        out.append(entry)
    return out


# --------------------------------------------------------------------------
# Main entry
# --------------------------------------------------------------------------
def attach_actions(findings: List[Finding], ctx) -> Tuple[List[Finding], List[Dict[str, Any]]]:
    access_blockers = [f for f in findings if f.check_id in _ACCESS_BLOCKERS]
    render_blockers = [f for f in findings if f.check_id in _RENDER_BLOCKERS]

    # 1. Build a working action for each finding (score + prerequisite refs).
    work: List[Dict[str, Any]] = []
    for f in findings:
        tpl = TEMPLATES.get(f.check_id) or _fallback_template(f)
        scope_mult, scope_label = _scope_mult(f)
        blockers = _blocker_refs(f, access_blockers, render_blockers)
        score = _score(f, tpl, scope_mult, bool(blockers))
        work.append({"f": f, "tpl": tpl, "scope": scope_label,
                     "blockers": blockers, "score": score})

    # 2. Rank: score desc, then easier effort first, then severity, then id.
    work.sort(key=lambda w: (
        -w["score"], _EFFORT_W.get(w["tpl"].get("effort", "medium"), 0.7) * -1,
        -w["f"].severity.rank, w["f"].check_id, (w["f"].evidence or {}).get("scope", ""),
    ))

    # 3. Assign stable presentation ids in ranked order. Dependencies are resolved
    #    from object identity afterwards, so duplicate incoming ids are harmless.
    ordered: List[Finding] = []
    for i, w in enumerate(work, start=1):
        w["f"].id = f"F-{i:03d}"
        ordered.append(w["f"])

    # 4. Attach the SuggestedAction to each finding.
    for rank, w in enumerate(work, start=1):
        f, tpl = w["f"], w["tpl"]
        deps = [b.id for b in w["blockers"]]
        priority = _priority_bucket(w["score"])
        effort = tpl.get("effort", "medium")
        do_first = (not deps) and effort in ("low", "medium") and w["score"] >= 42
        f.related_findings = deps
        f.suggested_action = SuggestedAction(
            summary=tpl["what"],
            priority=priority,
            what=tpl["what"],
            where=tpl.get("where", ""),
            why=tpl.get("why", ""),
            steps=list(tpl.get("how", [])),
            patch=tpl.get("patch"),
            evidence=_evidence_text(f),
            expected_effect=tpl.get("effect", ""),
            effort=effort,
            impact=float(tpl.get("impact") or CONCERN_IMPACT.get(f.concern.value, 0.6)),
            priority_score=w["score"],
            rank=rank,
            do_first=do_first,
            dependencies=deps,
        )

    # 5. De-duplicate recommendations that share a fix (same check_id). Keep the
    #    highest-ranked as canonical, merge affected locations into it, and mark
    #    the rest as duplicate_of the canonical.
    _dedup(ordered)

    beyond = _beyond_defect(ordered, ctx)
    return ordered, beyond


def _merge_where(primary_where: str, extra: str) -> str:
    parts = [p.strip() for p in (primary_where or "").split(";") if p.strip()]
    if extra and extra not in parts:
        parts.append(extra)
    return "; ".join(parts)


def _dedup(findings: List[Finding]) -> None:
    canonical: Dict[str, Finding] = {}
    for f in findings:  # findings are already in ranked order
        sa = f.suggested_action
        if sa is None:
            continue
        page_class = ((f.evidence or {}).get("metric") or {}).get("page_class") or ""
        key = f"{f.check_id}|{page_class}"
        if key not in canonical:
            canonical[key] = f
            continue
        primary = canonical[key]
        # Merge this finding's location into the canonical recommendation.
        loc = (f.evidence or {}).get("scope", "") or (f.evidence or {}).get("sentence", "")
        primary.suggested_action.where = _merge_where(primary.suggested_action.where, loc)
        # Mark the duplicate.
        sa.duplicate_of = primary.id
        sa.summary = f"Same fix as {primary.id}: {primary.suggested_action.what}"
        if primary.id not in f.related_findings:
            f.related_findings = [primary.id] + f.related_findings
