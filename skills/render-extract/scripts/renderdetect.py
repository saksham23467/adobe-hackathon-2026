"""Render-gap detectors.

Given the acquire-once cache (raw HTTP body + rendered DOM per page), we build a
`RenderView` for every page that has BOTH, then run pure detectors that fire only
when JavaScript *materially reduces machine accessibility*. JavaScript by itself
is never a defect: a server-rendered React page whose text is already in the raw
HTML produces zero findings.

Every finding records, per URL:
  * initial HTML evidence   (raw snippet + raw token count)
  * rendered evidence       (rendered visible snippet + token count)
  * extracted text difference (gap ratio, tokens added by JS)
  * affected content        (a sample of the content missing from raw / headings)
  * severity                (from the catalog, capped by confidence)
  * confidence              (from the tier)
  * mechanism explanation   (why it hurts a no-JS fetcher)

Standard library only.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_LIB = os.path.join(_ROOT, "lib")
for _p in (_HERE, _LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from textextract import PageExtract, extract, tokens  # noqa: E402
from audit_common.schema import (  # noqa: E402
    Concern,
    Confidence,
    Method,
    Observation,
    Status,
)

# --- thresholds (bounded, tuned against fixtures) -------------------------
MIN_MEANINGFUL = 40      # tokens that constitute "major" content
SHELL_MAX_RAW = 15       # raw text at/below this is an "empty shell"
GAP_FAIL = 0.6           # rendered visible text >> raw text
TEXT_STARVED = 20        # a rendered page below this has almost no machine text
HIDDEN_FRACTION = 0.4    # share of DOM text that is hidden from the a11y tree
SCRIPT_HEAVY = 12        # many scripts (informational only)
_SNIPPET = 240


class Tier(str, Enum):
    CONFIRMED = "confirmed"
    LIKELY = "likely"
    INFORMATIONAL = "informational"


def _obs(check_id, tier, urls, metric, evidence, method=Method.RENDER, artifacts=None):
    if tier == Tier.CONFIRMED:
        status, conf = Status.FAIL, Confidence.HIGH
    elif tier == Tier.LIKELY:
        status, conf = Status.FAIL, Confidence.MEDIUM
    else:
        status, conf = Status.PASS, Confidence.LOW
    return Observation(
        check_id=check_id,
        concern=Concern.RENDER,
        method=method,
        status=status,
        confidence=conf,
        urls=urls[:20],
        sample_n=len(urls),
        metric={**metric, "tier": tier.value},
        artifacts=(artifacts or [])[:20],
        evidence_sentence=evidence,
    )


def _snip(s: str, n: int = _SNIPPET) -> str:
    s = " ".join((s or "").split())
    return s[:n] + ("…" if len(s) > n else "")


@dataclass
class RenderView:
    url: str
    page_class: str
    raw: PageExtract
    rend: PageExtract
    raw_tokens: int
    rend_visible: int
    rend_all: int
    gap_ratio: float
    added_tokens: int
    missing_headings: List[str] = field(default_factory=list)

    def page_metric(self) -> Dict:
        return {
            "url": self.url,
            "page_class": self.page_class,
            "initial_html_tokens": self.raw_tokens,
            "rendered_visible_tokens": self.rend_visible,
            "rendered_dom_tokens": self.rend_all,
            "text_gap_ratio": round(self.gap_ratio, 2),
            "tokens_added_by_js": self.added_tokens,
        }

    def artifacts(self) -> List[Dict[str, str]]:
        return [
            {"type": "initial_html", "label": self.url, "value": _snip(self.raw.text_all) or "(empty)"},
            {"type": "rendered_text", "label": self.url, "value": _snip(self.rend.text_visible)},
        ]


def build_views(records) -> List[RenderView]:
    """One view per record that has both a raw body and a rendered DOM."""
    views: List[RenderView] = []
    for r in records:
        if not r.rendered_html or not r.raw_html:
            continue
        raw = extract(r.raw_html)
        rend = extract(r.rendered_html)
        raw_t = raw.all_tokens
        vis = rend.visible_tokens
        gap = 1.0 - (raw_t / vis) if vis > 0 else 0.0
        raw_lower = raw.text_all.lower()
        missing = [h for (_lvl, h) in rend.headings if h.lower() not in raw_lower]
        views.append(RenderView(
            url=r.url,
            page_class=getattr(r, "page_class", "other"),
            raw=raw,
            rend=rend,
            raw_tokens=raw_t,
            rend_visible=vis,
            rend_all=rend.all_tokens,
            gap_ratio=max(0.0, gap),
            added_tokens=max(0, vis - raw_t),
            missing_headings=missing,
        ))
    return views


# --- per-view classification (mutual exclusion of the major categories) ---
def _is_empty_shell(v: RenderView) -> bool:
    return (
        v.raw_tokens <= SHELL_MAX_RAW
        and v.rend_visible >= MIN_MEANINGFUL
        and (v.raw.has_mount or v.raw.script_count >= 1)
    )


def _is_primary_gap(v: RenderView) -> bool:
    return v.gap_ratio >= GAP_FAIL and v.added_tokens >= MIN_MEANINGFUL


def _is_text_starved(v: RenderView) -> bool:
    return v.rend_visible < TEXT_STARVED


# --- detectors ------------------------------------------------------------
def detect_empty_shell(views, shell_urls, gap_urls) -> List[Observation]:
    vs = [v for v in views if v.url in shell_urls]
    if not vs:
        return []
    metric = {"pages": [v.page_metric() for v in vs],
              "mechanism": "The initial HTML is an empty app shell (mount node + scripts); "
                           "a fetcher that does not execute JavaScript stores a blank page."}
    arts = [a for v in vs for a in v.artifacts()]
    ev = (f"{len(vs)} page(s) return an essentially empty HTML shell "
          f"(≤{SHELL_MAX_RAW} words) and only populate content after JavaScript; "
          f"no-JS crawlers and many AI fetchers see nothing.")
    return [_obs("render.empty_initial_shell", Tier.CONFIRMED, [v.url for v in vs], metric, ev, artifacts=arts)]


def _build_gap_metric(vs, mechanism):
    return {
        "pages": [dict(v.page_metric(), missing_headings=v.missing_headings[:5]) for v in vs],
        "affected_content_sample": _snip(vs[0].rend.text_visible),
        "mechanism": mechanism,
    }


def detect_primary_content(views, shell_urls, gap_urls) -> List[Observation]:
    gap_views = [v for v in views if v.url in gap_urls]
    if not gap_views:
        return []
    # A class is "fully gapped" only when it has >=2 sampled pages and every one
    # of them gaps -> that is a template-level (class) render gap. Anything else
    # is a broad primary-content gap on individual pages.
    total_by_class: Dict[str, int] = {}
    gap_by_class: Dict[str, int] = {}
    for v in views:
        total_by_class[v.page_class] = total_by_class.get(v.page_class, 0) + 1
    for v in gap_views:
        gap_by_class[v.page_class] = gap_by_class.get(v.page_class, 0) + 1
    fully_gapped = {c for c, g in gap_by_class.items() if g >= 2 and g == total_by_class.get(c)}

    mech = ("The page's primary text appears only after client-side rendering; the raw HTTP "
            "response omits it, so a no-JS fetcher cannot read or quote it.")
    out: List[Observation] = []

    class_pages = [v for v in gap_views if v.page_class in fully_gapped]
    if class_pages:
        cls = sorted({v.page_class for v in class_pages})
        ev = (f"An entire page-class ({', '.join(cls)}) renders its primary content only after "
              f"JavaScript across {len(class_pages)} sampled pages; the template omits main text from raw HTML.")
        out.append(_obs("render.class_js_gap", Tier.CONFIRMED, [v.url for v in class_pages],
                        _build_gap_metric(class_pages, mech), ev,
                        artifacts=[a for v in class_pages for a in v.artifacts()]))

    broad_pages = [v for v in gap_views if v.page_class not in fully_gapped]
    if broad_pages:
        median = sorted(v.gap_ratio for v in broad_pages)[len(broad_pages) // 2]
        ev = (f"{len(broad_pages)} page(s) have primary content only after JavaScript "
              f"(median gap {median:.0%}); the initial HTML omits the main text.")
        out.append(_obs("render.primary_content_js_only", Tier.CONFIRMED, [v.url for v in broad_pages],
                        _build_gap_metric(broad_pages, mech), ev,
                        artifacts=[a for v in broad_pages for a in v.artifacts()]))
    return out


def detect_headings_js_only(views, shell_urls, gap_urls) -> List[Observation]:
    # Subtler case: raw has *some* text, but the heading outline (h1..h3) is only
    # in the rendered DOM. Excluded when the page already fired a broader gap.
    excluded = shell_urls | gap_urls
    vs = [v for v in views if v.url not in excluded and v.missing_headings
          and any(v.rend.headings) and v.raw_tokens > SHELL_MAX_RAW]
    # Require that the H1 (or most headings) is missing to avoid noise.
    vs = [v for v in vs if len(v.missing_headings) >= max(1, len(v.rend.headings) // 2)]
    if not vs:
        return []
    metric = {"pages": [dict(v.page_metric(), missing_headings=v.missing_headings[:8]) for v in vs],
              "mechanism": "The heading outline that structures the page for machines is injected by "
                           "JavaScript and absent from the initial HTML, degrading machine parsing."}
    arts = [a for v in vs for a in v.artifacts()]
    ev = (f"{len(vs)} page(s) render headings that are absent from the initial HTML "
          f"(e.g. {vs[0].missing_headings[0]!r}); no-JS parsers see an unstructured page.")
    return [_obs("render.headings_js_only", Tier.CONFIRMED, [v.url for v in vs], metric, ev, artifacts=arts)]


def detect_content_behind_interaction(views, shell_urls) -> List[Observation]:
    vs = []
    for v in views:
        if v.url in shell_urls:
            continue
        if v.rend_all <= 0:
            continue
        hidden = v.rend.hidden_tokens
        frac = hidden / v.rend_all
        if hidden >= MIN_MEANINGFUL and frac >= HIDDEN_FRACTION:
            vs.append((v, hidden, frac))
    if not vs:
        return []
    metric = {"pages": [dict(v.page_metric(), hidden_tokens=h, hidden_fraction=round(f, 2))
                        for (v, h, f) in vs],
              "mechanism": "A large share of the page's text sits in elements hidden from the "
                           "accessibility tree (aria-hidden / display:none / behind interactions), "
                           "so assistants that read the exposed DOM miss it."}
    arts = [a for (v, _, _) in vs for a in v.artifacts()]
    ev = (f"{len(vs)} page(s) keep a large share of their text hidden behind interactions or "
          f"inaccessible UI patterns (≥{int(HIDDEN_FRACTION*100)}% of DOM text not exposed).")
    return [_obs("render.content_behind_interaction", Tier.LIKELY, [v.url for (v, _, _) in vs], metric, ev, artifacts=arts)]


def detect_schema_injected(views, shell_urls, gap_urls) -> List[Observation]:
    excluded = shell_urls | gap_urls
    vs = [v for v in views if v.url not in excluded
          and v.raw.jsonld_count == 0 and v.rend.jsonld_count >= 1]
    if not vs:
        return []
    metric = {"pages": [dict(v.page_metric(), rendered_jsonld_types=v.rend.jsonld_types[:6]) for v in vs],
              "mechanism": "schema.org JSON-LD is injected only after hydration; a no-JS fetcher "
                           "retrieves the page without any structured data."}
    arts = [{"type": "rendered_jsonld", "label": v.url, "value": ", ".join(v.rend.jsonld_types[:6]) or "present"}
            for v in vs]
    ev = (f"{len(vs)} page(s) expose JSON-LD only in the rendered DOM (types: "
          f"{', '.join(vs[0].rend.jsonld_types[:3]) or 'present'}); the raw HTML has none.")
    return [_obs("render.schema_injected_post_hydration", Tier.LIKELY, [v.url for v in vs], metric, ev,
                 method=Method.SCHEMA, artifacts=arts)]


def detect_text_as_image(views) -> List[Observation]:
    vs = []
    for v in views:
        if not _is_text_starved(v):
            continue
        imgs = v.rend.meaningful_images()
        heading_tokens = sum(tokens(h) for (_l, h) in v.rend.headings)
        if imgs and heading_tokens < TEXT_STARVED:
            vs.append((v, imgs))
    if not vs:
        return []
    metric = {"pages": [dict(v.page_metric(), meaningful_images=len(imgs),
                             image_srcs=[i.get("src", "") for i in imgs][:5]) for (v, imgs) in vs],
              "mechanism": "The page carries its meaningful text inside images rather than as HTML "
                           "text, so machines cannot extract or quote it without OCR."}
    arts = [{"type": "rendered_text", "label": v.url, "value": _snip(v.rend.text_visible) or "(almost no text)"}
            for (v, _imgs) in vs]
    ev = (f"{len(vs)} page(s) have almost no machine-readable text (<{TEXT_STARVED} words) while "
          f"carrying content in images; assistants cannot read image text.")
    return [_obs("render.text_as_image", Tier.LIKELY, [v.url for (v, _i) in vs], metric, ev,
                 method=Method.OCR, artifacts=arts)]


def detect_canvas_content(views) -> List[Observation]:
    vs = [v for v in views if v.rend.canvas_count >= 1 and _is_text_starved(v)]
    if not vs:
        return []
    metric = {"pages": [dict(v.page_metric(), canvas_elements=v.rend.canvas_count) for v in vs],
              "mechanism": "Primary content is painted into <canvas>, which exposes no text nodes; "
                           "machines see an opaque bitmap."}
    ev = (f"{len(vs)} page(s) render primary content into <canvas> with almost no accompanying "
          f"HTML text; the content is invisible to text extractors.")
    return [_obs("render.canvas_content", Tier.LIKELY, [v.url for v in vs], metric, ev)]


def detect_excessive_scripts(views, shell_urls, gap_urls) -> List[Observation]:
    # Informational ONLY. Heavy JS is not a defect when the content is present.
    excluded = shell_urls | gap_urls
    vs = [v for v in views if v.url not in excluded
          and v.raw.script_count >= SCRIPT_HEAVY and v.raw_tokens >= MIN_MEANINGFUL]
    if not vs:
        return []
    metric = {"pages": [dict(v.page_metric(), scripts=v.raw.script_count) for v in vs],
              "mechanism": "Informational: the page loads many scripts but its content is already in "
                           "the raw HTML, so this is a performance note, not an accessibility defect."}
    ev = (f"{len(vs)} page(s) are script-heavy (≥{SCRIPT_HEAVY} scripts) but already contain their "
          f"content in raw HTML — recorded as informational, not a defect.")
    return [_obs("render.excessive_script_dependence", Tier.INFORMATIONAL, [v.url for v in vs], metric, ev)]


def run_all(records) -> List[Observation]:
    views = build_views(records)
    if not views:
        return []
    shell_urls = {v.url for v in views if _is_empty_shell(v)}
    gap_urls = {v.url for v in views if v.url not in shell_urls and _is_primary_gap(v)}

    out: List[Observation] = []
    out += detect_empty_shell(views, shell_urls, gap_urls)
    out += detect_primary_content(views, shell_urls, gap_urls)
    out += detect_headings_js_only(views, shell_urls, gap_urls)
    out += detect_content_behind_interaction(views, shell_urls)
    out += detect_schema_injected(views, shell_urls, gap_urls)
    out += detect_text_as_image(views)
    out += detect_canvas_content(views)
    out += detect_excessive_scripts(views, shell_urls, gap_urls)
    return out
