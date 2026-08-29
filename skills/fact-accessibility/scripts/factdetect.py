"""Content-extractability detectors.

Given the parsed fact inventory (`factextract.Doc`) for each sampled page, decide
whether important brand facts are easy for a machine to identify, extract,
attribute, and quote. Every finding names the actual mechanism and carries an
evidence snippet plus a numeric confidence score.

We only flag concrete, mechanism-specific defects — never "content could be
improved". Findings are tiered:
    confirmed     -> FAIL / high confidence
    likely        -> FAIL / medium confidence
    informational -> PASS (recorded, not a finding)
"""

from __future__ import annotations

import os
import re
import sys
from enum import Enum
from typing import Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_LIB = os.path.join(_ROOT, "lib")
for _p in (_HERE, _LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import factextract as fx  # noqa: E402
from audit_common.schema import (  # noqa: E402
    Concern,
    Confidence,
    Method,
    Observation,
    Status,
)

_LOCKED_CATEGORIES = {"pricing", "hours", "contact", "specs", "menu", "location"}
_INTERACTION_CATEGORIES = {"faq", "specs", "policy", "features", "pricing", "hours", "contact"}
_ADD_TO_CART_RE = re.compile(r"\b(add to (?:cart|bag|basket)|buy now|checkout)\b", re.I)


class Tier(str, Enum):
    CONFIRMED = "confirmed"
    LIKELY = "likely"
    INFORMATIONAL = "informational"


def _obs(check_id, tier, urls, mechanism, evidence, items, method=Method.HTTP,
         score=0.0, artifacts=None):
    if tier == Tier.CONFIRMED:
        status, conf = Status.FAIL, Confidence.HIGH
    elif tier == Tier.LIKELY:
        status, conf = Status.FAIL, Confidence.MEDIUM
    else:
        status, conf = Status.PASS, Confidence.LOW
    return Observation(
        check_id=check_id,
        concern=Concern.FACT_ACCESS,
        method=method,
        status=status,
        confidence=conf,
        urls=urls[:20],
        sample_n=len(urls),
        metric={"mechanism": mechanism, "confidence_score": round(score, 2),
                "tier": tier.value, "items": items[:20]},
        artifacts=(artifacts or [])[:20],
        evidence_sentence=evidence,
    )


# --------------------------------------------------------------------------
# Q4: unambiguous wording — vague qualifier instead of a concrete value
# --------------------------------------------------------------------------
def detect_ambiguous_value(pages) -> List[Observation]:
    out: List[Observation] = []
    checks = {
        "pricing": lambda d: not (fx.find_prices(d.text_all) or d.offer_prices()),
        "hours": lambda d: not (fx.has_concrete_hours(d.text_all) or d.schema_has_hours()),
        "location": lambda d: not (fx.has_concrete_address(d.text_all) or d.schema_has_address()),
    }
    labels = {
        "pricing": "price", "hours": "opening hours", "location": "location/address",
    }
    for cat, no_concrete in checks.items():
        items, urls, arts = [], [], []
        for p in pages:
            d = p["doc"]
            phrase = fx.vague_hit(d.text_all, cat)
            if phrase and cat == "pricing" and re.search(
                r"\b(enterprise plan|talk to sales|custom plan for enterprise)\b",
                d.text_all, re.I,
            ):
                continue
            if phrase and no_concrete(d):
                snip = fx.snippet(d.text_all, phrase)
                items.append({"url": p["url"], "category": cat, "vague_phrase": phrase, "snippet": snip})
                urls.append(p["url"])
                arts.append({"type": "snippet", "label": p["url"], "value": snip})
        if items:
            mech = (f"The {labels[cat]} is referenced only with a vague qualifier "
                    f"(e.g. {items[0]['vague_phrase']!r}) and no concrete value, so a machine cannot "
                    f"extract or quote a specific {labels[cat]}.")
            ev = (f"{len(items)} page(s) reference {labels[cat]} with a vague phrase and no concrete "
                  f"value; e.g. {items[0]['vague_phrase']!r} on {items[0]['url']}.")
            out.append(_obs("fact.ambiguous_value", Tier.LIKELY, urls, mech, ev, items,
                            method=Method.HTTP, score=0.8, artifacts=arts))
    return out


# --------------------------------------------------------------------------
# Q2/Q6/Q10: readable text — a labeled fact is only in an image
# --------------------------------------------------------------------------
def detect_facts_locked_in_non_text(pages) -> List[Observation]:
    items, urls, arts = [], [], []
    for p in pages:
        for s in p["doc"].sections:
            cat = s.category()
            if cat not in _LOCKED_CATEGORIES:
                continue
            imgs = fx.meaningful_images(s.images)
            if not imgs or fx.tokens(s.text) >= 8:
                continue
            concrete = (
                fx.find_prices(s.text) if cat == "pricing" else
                fx.has_concrete_hours(s.text) if cat == "hours" else
                fx.has_concrete_address(s.text) if cat == "location" else
                (fx.find_emails(s.text) or fx.find_phones(s.text)) if cat == "contact" else
                False
            )
            if concrete:
                continue
            src = imgs[0].get("src", "")
            items.append({"url": p["url"], "category": cat, "heading": s.heading, "image": src,
                          "body_words": fx.tokens(s.text)})
            urls.append(p["url"])
            arts.append({"type": "image_only_fact", "label": f"{p['url']} · {s.heading}", "value": src})
    if not items:
        return []
    mech = ("A labeled fact section presents its content as an image with no equivalent readable text, "
            "so the fact can only be recovered by interpreting the visual/OCR, not by reading the HTML.")
    ev = (f"{len(items)} fact section(s) (e.g. {items[0]['heading']!r} on {items[0]['url']}) carry "
          f"their content only as an image with no textual equivalent.")
    return [_obs("fact.facts_locked_in_non_text", Tier.CONFIRMED, urls, mech, ev, items,
                 method=Method.OCR, score=0.85, artifacts=arts)]


# --------------------------------------------------------------------------
# Q7: contradictory information — structured price != displayed price
# --------------------------------------------------------------------------
def detect_contradictory_values(pages) -> List[Observation]:
    items, urls, arts = [], [], []
    for p in pages:
        d = p["doc"]
        schema_prices = d.offer_prices()
        text_prices = fx.find_prices(d.text_visible)
        if schema_prices and text_prices and schema_prices.isdisjoint(text_prices):
            # Ignore comparison tables that list many prices; require the
            # schema price to be absent from the visible set entirely.
            items.append({"url": p["url"], "schema_price": sorted(schema_prices),
                          "displayed_price": sorted(text_prices)})
            urls.append(p["url"])
            arts.append({"type": "conflict", "label": p["url"],
                         "value": f"schema={sorted(schema_prices)} vs displayed={sorted(text_prices)}"})
    if not items:
        return []
    mech = ("The price in the structured data (schema.org Offer) disagrees with the price shown in the "
            "visible text, so an assistant may quote a wrong, contradictory value.")
    ev = (f"{len(items)} page(s) show a price that contradicts their structured data; e.g. "
          f"schema {items[0]['schema_price']} vs displayed {items[0]['displayed_price']} on {items[0]['url']}.")
    return [_obs("fact.contradictory_values", Tier.CONFIRMED, urls, mech, ev, items,
                 method=Method.SCHEMA, score=0.9, artifacts=arts)]


# --------------------------------------------------------------------------
# Q3: correct entity — a price is not tied to a named product via schema
# --------------------------------------------------------------------------
def detect_not_attributed(pages) -> List[Observation]:
    items, urls, arts = [], [], []
    for p in pages:
        d = p["doc"]
        prices = fx.find_prices(d.text_visible)
        product_intent = p.get("page_class") == "product" or bool(_ADD_TO_CART_RE.search(d.text_visible))
        has_micro = bool(re.search(r'itemtype=["\'][^"\']*Product', p.get("html") or "", re.I))
        if prices and product_intent and not d.has_type("Product") and not d.offer_prices() and not has_micro:
            snip = fx.snippet(d.text_visible, next(iter(str(int(x)) for x in prices), ""))
            items.append({"url": p["url"], "prices": sorted(prices)})
            urls.append(p["url"])
            arts.append({"type": "snippet", "label": p["url"], "value": snip})
    if not items:
        return []
    mech = ("A price is shown on a product page but there is no Product/Offer structured data (and no "
            "machine-readable product entity), so an assistant cannot attribute the price to a named product.")
    ev = (f"{len(items)} product page(s) display a price with no Product/Offer schema to attribute it; "
          f"e.g. {items[0]['prices']} on {items[0]['url']}.")
    return [_obs("fact.not_attributed_to_entity", Tier.LIKELY, urls, mech, ev, items,
                 method=Method.SCHEMA, score=0.7, artifacts=arts)]


# --------------------------------------------------------------------------
# Q9: only after interaction — a fact category lives in hidden DOM
# --------------------------------------------------------------------------
def detect_behind_interaction(pages) -> List[Observation]:
    items, urls, arts = [], [], []
    for p in pages:
        for s in p["doc"].sections:
            cat = s.category()
            if cat in _INTERACTION_CATEGORIES and s.hidden and fx.tokens(s.text) >= 15:
                snip = fx.snippet(s.text, "")
                items.append({"url": p["url"], "category": cat, "heading": s.heading,
                              "hidden_words": fx.tokens(s.text)})
                urls.append(p["url"])
                arts.append({"type": "hidden_fact", "label": f"{p['url']} · {s.heading}", "value": snip})
    if not items:
        return []
    mech = ("A fact section is present in the DOM but hidden from the accessibility tree "
            "(aria-hidden / display:none / behind an interaction), so it is absent from the initial "
            "readable text an assistant extracts.")
    ev = (f"{len(items)} fact section(s) (e.g. {items[0]['heading']!r} on {items[0]['url']}) are only "
          f"available after interaction / hidden from the accessibility tree.")
    return [_obs("fact.behind_interaction", Tier.LIKELY, urls, mech, ev, items,
                 method=Method.RENDER, score=0.7, artifacts=arts)]


# --------------------------------------------------------------------------
# Q1: explicitly stated — homepage lacks a quotable "what we do" sentence
# --------------------------------------------------------------------------
def detect_identity_not_textual(pages) -> List[Observation]:
    for p in pages:
        if not p.get("is_home"):
            continue
        d = p["doc"]
        if fx.tokens(d.text_visible) < 15:
            continue
        has_identity = bool(d.meta_description) or bool(d.org_description()) or bool(
            fx._IDENTITY_RE.search(d.text_visible))
        if has_identity:
            continue
        headline = d.sections[0].heading if d.sections and d.sections[0].heading else d.title
        mech = ("The homepage has no machine-extractable sentence stating what the company does "
                "(no meta description, no Organization description, no plain 'we provide/build/offer' "
                "statement); identity is conveyed only via slogans/imagery.")
        ev = (f"The homepage {p['url']} states no quotable one-sentence description of what the brand "
              f"does; the most prominent text is a slogan ({headline!r}).")
        art = [{"type": "snippet", "label": p["url"], "value": fx.snippet(d.text_visible, "")}]
        return [_obs("fact.no_quotable_value_prop", Tier.LIKELY, [p["url"]], mech, ev,
                     [{"url": p["url"], "headline": headline}], method=Method.HTTP, score=0.65, artifacts=art)]
    return []


# --------------------------------------------------------------------------
# Q2: readable text — content images without alt text
# --------------------------------------------------------------------------
def detect_images_no_alt(pages) -> List[Observation]:
    items, urls = [], []
    for p in pages:
        imgs = fx.meaningful_images(p["doc"].images)
        if len(imgs) < 3:
            continue
        missing = [i for i in imgs if not (i.get("alt") or "").strip()]
        if len(missing) / len(imgs) >= 0.6:
            items.append({"url": p["url"], "images": len(imgs), "missing_alt": len(missing)})
            urls.append(p["url"])
    if not items:
        return []
    mech = ("Most content images on the page have empty/missing alt text, so any facts they carry "
            "(specs, prices, claims shown in graphics) are unreadable to extractors.")
    ev = (f"{len(items)} page(s) are image-heavy with most images missing alt text; e.g. "
          f"{items[0]['missing_alt']}/{items[0]['images']} on {items[0]['url']}.")
    return [_obs("fact.informational_images_no_alt", Tier.LIKELY, urls, mech, ev, items,
                 method=Method.HTTP, score=0.6)]


_DETECTORS = [
    detect_ambiguous_value,
    detect_facts_locked_in_non_text,
    detect_contradictory_values,
    detect_not_attributed,
    detect_behind_interaction,
    detect_identity_not_textual,
    detect_images_no_alt,
]


def build_pages(records) -> List[Dict]:
    pages = []
    for r in records:
        html = r.rendered_html or r.raw_html
        if not html:
            continue
        doc = fx.extract_doc(html)
        pclass = getattr(r, "page_class", "other")
        url = getattr(r, "url", "")
        is_home = pclass == "home" or re.sub(r"^https?://[^/]+", "", url).strip("/") == ""
        pages.append({"url": url, "doc": doc, "page_class": pclass, "is_home": is_home, "html": html})
    return pages


def run_all(records) -> List[Observation]:
    pages = build_pages(records)
    if not pages:
        return []
    out: List[Observation] = []
    for det in _DETECTORS:
        try:
            out += det(pages)
        except Exception:
            continue
    return out
