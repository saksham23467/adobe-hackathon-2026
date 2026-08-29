"""Engagement detectors: locate where the visitor journey breaks.

Journey model:  arrival → orientation → understanding → evaluation → next action

Every detector consumes the deterministic `JourneyModel` (journey.py) and emits
evidence-backed Observations. Findings are tiered by *confidence*, which the
compiler maps to severity:

  * CONFIRMED structural issue → Confidence.HIGH   (an objective DOM defect)
  * LIKELY UX issue           → Confidence.MEDIUM  (strong signal, some judgement)
  * RECOMMENDATION            → Confidence.LOW     (worth improving, not a defect)

`metric["tier"]` and `metric["stage"]` label each finding so the report can
separate the three buckets and place the break in the journey. Detectors are
deliberately conservative: they never flag mere absence-of-nicety, only
observable breakages.
"""

from __future__ import annotations

import os
import re
import sys
from typing import Dict, List

_HERE = os.path.dirname(__file__)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_LIB = os.path.join(_ROOT, "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import journey as J  # noqa: E402
from audit_common.schema import (  # noqa: E402
    Concern,
    Confidence,
    Method,
    Observation,
    Status,
)
from audit_common.semantics import (  # noqa: E402
    SemanticClass,
    classify_resource,
    in_scope,
    link_role,
)

CONFIRMED = Confidence.HIGH
LIKELY = Confidence.MEDIUM
RECOMMENDATION = Confidence.LOW

_TIER_LABEL = {
    Confidence.HIGH: "confirmed_structural",
    Confidence.MEDIUM: "likely_ux",
    Confidence.LOW: "recommendation",
}


def _obs(check_id, urls, tier, stage, sentence, metric=None,
         resource_type="html_document", page_type="", entity_scope="page",
         semantic_relevance="high", evidence_tier=""):
    m = dict(metric or {})
    m["tier"] = _TIER_LABEL[tier]
    m["stage"] = stage
    etier = evidence_tier or {
        Confidence.HIGH: "confirmed",
        Confidence.MEDIUM: "likely",
        Confidence.LOW: "review",
    }.get(tier, "likely")
    return Observation(
        check_id=check_id,
        concern=Concern.ENGAGEMENT,
        method=Method.RENDER,
        status=Status.FAIL,
        confidence=tier,
        urls=list(urls),
        sample_n=len(urls),
        metric=m,
        evidence_sentence=sentence,
        resource_type=resource_type,
        page_type=page_type,
        entity_scope=entity_scope,
        semantic_relevance=semantic_relevance,
        evidence_tier=etier,
    )


# ---------------------------------------------------------------------------
# ARRIVAL — is the first paint usable, or blocked?
# ---------------------------------------------------------------------------
def detect_blocking_interstitial(model: J.JourneyModel) -> List[Observation]:
    home = model.home
    if home is None or not home.overlay:
        return []
    txt = home.overlay.get("text", "")
    return [_obs(
        "engagement.blocking_interstitial", [home.url], CONFIRMED, "arrival",
        "A full-content-blocking overlay ('%s') appears before the page content, "
        "so an arriving visitor sees an obstacle instead of the site's value."
        % (txt[:80] or home.overlay.get("selector", "modal")),
        {"selector": home.overlay.get("selector", ""), "overlay_text": txt},
    )]


# ---------------------------------------------------------------------------
# ORIENTATION — can they tell what this is and what to do next?
# ---------------------------------------------------------------------------
def detect_no_orientation(model: J.JourneyModel) -> List[Observation]:
    if not model.has_real_homepage():
        return []
    home = model.home
    if home is None:
        return []
    has_h1 = home.has_visible_h1()
    has_cta = home.has_primary_cta()
    if has_h1 or has_cta:
        return []
    return [_obs(
        "engagement.no_orientation", [home.url], LIKELY, "orientation",
        "The entry page has no visible <h1> value proposition and no primary "
        "call-to-action, so a first-time visitor cannot tell what this is or "
        "what to do next.",
        {"has_visible_h1": has_h1, "has_primary_cta": has_cta,
         "title": home.title},
    )]


def detect_no_ia_navigation(model: J.JourneyModel) -> List[Observation]:
    if not model.has_real_homepage():
        return []
    home = model.home
    if home is None:
        return []
    if home.has_nav or home.has_breadcrumb or home.has_search:
        return []
    return [_obs(
        "engagement.no_ia_navigation", [home.url], LIKELY, "orientation",
        "The entry page exposes no navigation, breadcrumb, or site search, so a "
        "visitor has no structured way to move through the site.",
        {"has_nav": False, "has_breadcrumb": False, "has_search": False},
    )]


# ---------------------------------------------------------------------------
# UNDERSTANDING — is orientation content reachable and comprehensible?
# ---------------------------------------------------------------------------
def detect_content_hidden_interaction(model: J.JourneyModel) -> List[Observation]:
    if not model.has_real_homepage():
        return []
    home = model.home
    if home is None:
        return []
    if not home.hidden_h1_only():
        return []
    hidden = next((h for h in home.h1s if h), "")
    return [_obs(
        "engagement.content_hidden_interaction", [home.url], LIKELY, "understanding",
        "The entry page's main heading ('%s') exists only inside hidden / "
        "interaction-gated DOM, so the orientation message is not present in the "
        "content a visitor first sees." % hidden[:80],
        {"hidden_h1": hidden},
    )]


def detect_unexplained_jargon(model: J.JourneyModel) -> List[Observation]:
    # RECOMMENDATION: bare acronyms used as navigation labels that are never
    # expanded anywhere on the site (excludes widely understood acronyms).
    site_text = " ".join(
        (p.title + " " + " ".join(p.headings) + " " +
         " ".join(l.label for l in p.links))
        for p in model.pages.values()
    )
    offenders = {}
    for p in model.pages.values():
        for path, label in p.nav_dest_labels():
            token = label.strip()
            if re.fullmatch(r"[A-Z]{2,6}", token) and token.upper() not in J._KNOWN_ACRONYMS:
                # expanded elsewhere?  e.g. "ACME (A Company...)" or letters spelled out
                if re.search(re.escape(token) + r"\s*\(", site_text):
                    continue
                offenders.setdefault(token, p.url)
    if not offenders:
        return []
    sample = sorted(offenders.items())[:5]
    return [_obs(
        "engagement.unexplained_jargon",
        [u for _, u in sample], RECOMMENDATION, "understanding",
        "Navigation uses unexpanded acronym label(s) (%s) that never appear "
        "spelled out on the site, which can confuse first-time visitors."
        % ", ".join(t for t, _ in sample),
        {"acronyms": [t for t, _ in sample]},
    )]


# ---------------------------------------------------------------------------
# NAVIGATION / CONTINUITY — are links clear and consistent?
# ---------------------------------------------------------------------------
def detect_unclear_navigation(model: J.JourneyModel) -> List[Observation]:
    empties, vague = [], []
    for p in model.pages.values():
        src = classify_resource(p.url)
        if not in_scope("engagement.unclear_navigation", src):
            continue
        for l in p.links:
            h = (l.href or "").lower()
            if not h or h.startswith(("mailto:", "tel:", "javascript:")):
                continue
            # Media/embed hrefs are not navigation elements (a silent <a> around
            # an .mp4 / .svg is an asset, not an unclear menu item).
            if link_role(l.href, l.label) == "media_asset":
                continue
            label = _norm(l.label)
            if not label:
                empties.append((p.url, l.href))
            elif label in J._NON_DESCRIPTIVE:
                vague.append((p.url, label))
    out: List[Observation] = []
    if empties:
        urls = _uniq([u for u, _ in empties])
        out.append(_obs(
            "engagement.unclear_navigation", urls, CONFIRMED, "next_action",
            "%d link(s) have no discernible text or aria-label (e.g. %s), so a "
            "visitor (and assistants) cannot tell where they lead."
            % (len(empties), empties[0][1] or "<empty href>"),
            {"empty_link_count": len(empties),
             "examples": [h for _, h in empties[:5]]},
        ))
    if vague:
        urls = _uniq([u for u, _ in vague])
        labels = sorted({v for _, v in vague})
        out.append(_obs(
            "engagement.unclear_navigation", urls, LIKELY, "next_action",
            "%d link(s) use non-descriptive text (%s), giving no context about "
            "the destination." % (len(vague), ", ".join("'%s'" % x for x in labels[:4])),
            {"vague_link_count": len(vague), "labels": labels[:8]},
        ))
    return out


def detect_inconsistent_naming(model: J.JourneyModel) -> List[Observation]:
    dest_labels: Dict[str, set] = {}
    dest_pages: Dict[str, set] = {}
    for p in model.pages.values():
        for path, label in p.nav_dest_labels():
            if not path:
                continue
            dest_labels.setdefault(path, set()).add(label)
            dest_pages.setdefault(path, set()).add(p.url)
    conflicts = {d: labs for d, labs in dest_labels.items() if len(labs) >= 2}
    if not conflicts:
        return []
    dest, labs = sorted(conflicts.items())[0]
    urls = _uniq(sorted(dest_pages.get(dest, [])))
    return [_obs(
        "engagement.inconsistent_naming", urls, CONFIRMED, "understanding",
        "The same destination (%s) is labelled inconsistently in navigation "
        "(%s), so visitors cannot build a stable mental map of the site."
        % (dest, ", ".join("'%s'" % x for x in sorted(labs))),
        {"destination": dest, "labels": sorted(labs),
         "all_conflicts": {d: sorted(v) for d, v in list(conflicts.items())[:5]}},
    )]


# ---------------------------------------------------------------------------
# NEXT ACTION — can the visitor continue toward a goal?
# ---------------------------------------------------------------------------
def detect_dead_end_pages(model: J.JourneyModel) -> List[Observation]:
    dead = []
    for p in model.pages.values():
        if p.url == model.home_url:
            continue
        cls = classify_resource(p.url)
        if getattr(p, "resource_type", ""):
            cls = SemanticClass(
                resource_type=p.resource_type or "html_document",
                page_type=getattr(p, "page_type", "") or cls.page_type,
                entity_scope="page",
            )
        if not in_scope("engagement.dead_end_page", cls):
            continue
        if p.has_nav or p.has_breadcrumb:
            continue
        if p.content_links() or p.has_primary_cta():
            continue
        dead.append(p.url)
    if not dead:
        return []
    return [_obs(
        "engagement.dead_end_page", _uniq(dead), LIKELY, "next_action",
        "%d page(s) offer no navigation, no onward content links, and no "
        "call-to-action, leaving a visitor with no next step (e.g. %s)."
        % (len(dead), dead[0]),
        {"dead_end_count": len(dead), "examples": dead[:5]},
    )]


def detect_key_info_undiscoverable(model: J.JourneyModel) -> List[Observation]:
    if not model.has_real_homepage():
        return []
    out: List[Observation] = []
    for cls, label in (("product", "product/service"), ("pricing", "pricing"),
                       ("contact", "contact")):
        pages = model.by_class(cls)
        if not pages:
            continue  # never penalise absence — only undiscoverable presence
        if any(model.home_reaches(p.self_path) for p in pages):
            continue
        out.append(_obs(
            "engagement.key_info_undiscoverable",
            [model.home_url], LIKELY, "evaluation",
            "The site has %s page(s) (%s) but none are linked from the home "
            "page, so a visitor cannot reach this key information from the entry "
            "point." % (label, pages[0].url),
            {"page_class": cls, "pages": [p.url for p in pages[:5]]},
        ))
    return out


# ---------------------------------------------------------------------------
# MOBILE — reliably detectable signals only
# ---------------------------------------------------------------------------
def detect_mobile_signals(model: J.JourneyModel) -> List[Observation]:
    missing, blocks_zoom = [], []
    for p in model.pages.values():
        if p.viewport is None:
            missing.append(p.url)
        elif p.viewport_blocks_zoom:
            blocks_zoom.append(p.url)
    total = len(model.pages) or 1
    out: List[Observation] = []
    if missing and len(missing) >= max(1, total // 2):
        out.append(_obs(
            "engagement.no_viewport_meta", _uniq(missing), LIKELY, "orientation",
            "%d of %d sampled pages have no <meta name=viewport>, so the layout "
            "will not adapt to phones — a major bounce driver on mobile."
            % (len(missing), total),
            {"missing_count": len(missing), "total": total, "examples": missing[:5]},
        ))
    if blocks_zoom:
        out.append(_obs(
            "engagement.no_viewport_meta", _uniq(blocks_zoom), LIKELY, "orientation",
            "%d page(s) disable pinch-zoom (user-scalable=no / maximum-scale=1), "
            "which blocks visitors who need to enlarge text." % len(blocks_zoom),
            {"blocks_zoom_count": len(blocks_zoom), "examples": blocks_zoom[:5]},
        ))
    return out


# ---------------------------------------------------------------------------
_DETECTORS = [
    detect_blocking_interstitial,
    detect_no_orientation,
    detect_no_ia_navigation,
    detect_content_hidden_interaction,
    detect_unexplained_jargon,
    detect_unclear_navigation,
    detect_inconsistent_naming,
    detect_dead_end_pages,
    detect_key_info_undiscoverable,
    detect_mobile_signals,
]


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def _uniq(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def run_all(records) -> List[Observation]:
    model = J.build_model(records)
    if not model.pages:
        return []
    out: List[Observation] = []
    for det in _DETECTORS:
        try:
            out.extend(det(model))
        except Exception:
            continue
    return out
