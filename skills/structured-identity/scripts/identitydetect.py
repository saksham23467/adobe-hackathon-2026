"""Structured-data & entity-identity detectors.

Operates over the `EntityGraph` built from JSON-LD / microdata / RDFa. Schema
presence is never treated as automatically good and schema *absence* is never a
finding on its own — every detector connects structured data to a concrete
discoverability, extraction, attribution, or ambiguity problem.

Each finding carries: exact evidence, the affected entity, the detected
mismatch, the expected relationship, severity, confidence, mechanism, and a
concrete fix.
"""

from __future__ import annotations

import os
import re
import sys
import urllib.parse
from enum import Enum
from typing import Any, Dict, List

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_LIB = os.path.join(_ROOT, "lib")
for _p in (_HERE, _LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import entitygraph as eg  # noqa: E402
from audit_common.schema import (  # noqa: E402
    Concern,
    Confidence,
    Method,
    Observation,
    Status,
)

_PRICE_RE = re.compile(r"(?:[$€£₹]\s?\d[\d,]*(?:\.\d{1,2})?)|\b\d[\d,]*(?:\.\d{1,2})?\s?(?:USD|EUR|GBP)\b", re.I)


class Tier(str, Enum):
    CONFIRMED = "confirmed"
    LIKELY = "likely"


def _host(url: str) -> str:
    h = urllib.parse.urlsplit(url).netloc.lower()
    return h[4:] if h.startswith("www.") else h


def _visible_prices(text: str):
    out = set()
    for m in _PRICE_RE.finditer(text or ""):
        num = re.sub(r"[^\d.]", "", m.group(0))
        try:
            if num:
                out.add(round(float(num), 2))
        except ValueError:
            pass
    return out


def _obs(check_id, tier, urls, *, affected, mismatch, expected, mechanism, fix,
         evidence, items, method=Method.SCHEMA, score=0.0, artifacts=None):
    status = Status.FAIL
    conf = Confidence.HIGH if tier == Tier.CONFIRMED else Confidence.MEDIUM
    return Observation(
        check_id=check_id,
        concern=Concern.IDENTITY,
        method=method,
        status=status,
        confidence=conf,
        urls=urls[:20],
        sample_n=len(urls),
        metric={
            "affected_entity": affected,
            "detected_mismatch": mismatch,
            "expected_relationship": expected,
            "mechanism": mechanism,
            "fix": fix,
            "confidence_score": round(score, 2),
            "tier": tier.value,
            "items": items[:20],
        },
        artifacts=(artifacts or [])[:20],
        evidence_sentence=evidence,
    )


# --------------------------------------------------------------------------
# 6. Malformed JSON-LD
# --------------------------------------------------------------------------
def detect_malformed(records) -> List[Observation]:
    items, urls, arts = [], [], []
    for r in records:
        html = getattr(r, "rendered_html", "") or getattr(r, "raw_html", "")
        for err in eg.scan_jsonld_errors(html):
            items.append({"url": r.url, "error": err["error"], "snippet": err["snippet"]})
            urls.append(r.url)
            arts.append({"type": "malformed_jsonld", "label": r.url, "value": err["snippet"]})
    if not items:
        return []
    return [_obs(
        "identity.jsonld_malformed", Tier.CONFIRMED, urls,
        affected=f"{len(items)} JSON-LD block(s)",
        mismatch="script[type=application/ld+json] contains invalid JSON",
        expected="Every ld+json block must be valid JSON so parsers can read it",
        mechanism="A JSON syntax error makes the whole block invisible to crawlers; the entity it "
                  "describes simply does not exist for machines.",
        fix="Fix the JSON syntax (trailing commas, unescaped quotes) and validate with a JSON-LD validator.",
        evidence=f"{len(items)} JSON-LD block(s) failed to parse; e.g. on {items[0]['url']}: {items[0]['error']}.",
        items=items, score=0.95, artifacts=arts,
    )]


# --------------------------------------------------------------------------
# 4. schema.org type appropriateness (unrecognized / typo types)
# --------------------------------------------------------------------------
def detect_type_inappropriate(graph) -> List[Observation]:
    items, urls, arts = [], [], []
    for n in graph.nodes:
        for t in n.types:
            if t in eg.SCHEMA_TYPES or not re.fullmatch(r"[a-z][a-z0-9]{2,}", t):
                continue
            suggestion = min(eg.SCHEMA_TYPES, key=lambda s: eg.levenshtein(t, s))
            dist = eg.levenshtein(t, suggestion)
            # Unknown-but-valid types (Hotel, Bakery, …) are not defects.
            # Only flag near-typos of types we know.
            if dist > 2:
                continue
            near = suggestion
            items.append({"url": n.source_url, "type": t, "entity": n.name(), "suggestion": near})
            urls.append(n.source_url)
            arts.append({"type": "bad_type", "label": n.source_url, "value": f"@type={t}"})
    if not items:
        return []
    hint = f" (did you mean '{items[0]['suggestion']}'?)" if items[0]["suggestion"] else ""
    return [_obs(
        "identity.type_inappropriate", Tier.LIKELY, urls,
        affected=items[0]["entity"] or items[0]["type"],
        mismatch=f"@type '{items[0]['type']}' is not a recognized schema.org type",
        expected="@type must be a valid schema.org class so machines can interpret the entity",
        mechanism="An unrecognized @type is ignored by consumers, so the markup yields no usable entity.",
        fix=f"Replace the @type with a valid schema.org class{hint}.",
        evidence=f"{len(items)} entity(ies) declare an unrecognized @type; e.g. '{items[0]['type']}' on {items[0]['url']}{hint}.",
        items=items, score=0.75, artifacts=arts,
    )]


# --------------------------------------------------------------------------
# 5. Required/important properties (present-but-incomplete)
# --------------------------------------------------------------------------
def _missing_required(n) -> List[str]:
    miss = []
    if n.is_a("product", "individualproduct", "productgroup") and not n.name():
        miss.append("name")
    if n.is_a("offer", "aggregateoffer"):
        if n.get("price") is not None and not n.get("priceCurrency"):
            miss.append("priceCurrency")
    if n.is_a("organization", "localbusiness", "corporation") and not n.name():
        miss.append("name")
    if n.is_a("website") and (not n.name() or not n.get("url")):
        miss.append("name/url")
    if n.is_a("article", "newsarticle", "blogposting") and not n.get("datePublished"):
        miss.append("datePublished")
    if n.is_a("postaladdress") and not (n.get("streetAddress") or n.get("addressLocality")):
        miss.append("streetAddress/addressLocality")
    return miss


def detect_schema_invalid(graph) -> List[Observation]:
    items, urls, arts = [], [], []
    for n in graph.nodes:
        miss = _missing_required(n)
        if miss:
            label = n.name() or n.primary_type()
            items.append({"url": n.source_url, "entity": label, "type": n.primary_type(),
                          "missing": miss})
            urls.append(n.source_url)
            arts.append({"type": "missing_props", "label": f"{n.source_url} · {label}",
                         "value": ", ".join(miss)})
    if not items:
        return []
    ex = items[0]
    return [_obs(
        "identity.schema_invalid", Tier.CONFIRMED, urls,
        affected=ex["entity"],
        mismatch=f"{ex['type']} is missing required property/ies: {', '.join(ex['missing'])}",
        expected="Required schema.org properties must be present for the entity to be usable",
        mechanism="A structured entity missing its required properties is dropped or treated as "
                  "incomplete, so the fact it should convey is not machine-quotable.",
        fix=f"Add the missing propert(ies) ({', '.join(ex['missing'])}) to the {ex['type']} entity.",
        evidence=f"{len(items)} entity(ies) are missing required properties; e.g. {ex['type']} on "
                 f"{ex['url']} lacks {', '.join(ex['missing'])}.",
        items=items, score=0.85, artifacts=arts,
    )]


# --------------------------------------------------------------------------
# 7. Contradictory structured data (internal / cross-node)
# --------------------------------------------------------------------------
# schema.org type families. Types within a family are subtype/supertype
# compatible, so one @id shared across them is legitimate, not a contradiction.
_TYPE_FAMILIES = {
    "org": {
        "organization", "localbusiness", "corporation", "ngo", "company",
        "onlinebusiness", "onlinestore", "store", "restaurant", "professionalservice",
        "educationalorganization", "governmentorganization", "medicalorganization",
        "nonprofit", "foodestablishment", "hotel", "motel", "lodgingbusiness",
        "bedandbreakfast", "hostel", "bakery", "cafeorcoffeeshop", "dentist",
        "physician", "hospital", "autorepair", "hvacbusiness", "legalservice",
        "attorney", "notary", "realestateagent", "employmentagency", "travelagency",
        "gym", "dayspa", "beautysalon", "hairsalon", "governmentoffice", "cityhall",
        "pharmacy", "veterinarycare", "sportsactivitylocation", "sportsorganization",
    },
    "webpage": {
        "webpage", "aboutpage", "contactpage", "collectionpage", "itempage",
        "faqpage", "qapage", "searchresultspage", "profilepage",
    },
    "creativework": {
        "creativework", "article", "newsarticle", "blogposting", "blog",
        "techarticle", "report", "book", "recipe", "howto",
    },
    "product": {"product", "productgroup", "individualproduct"},
    "website": {"website"},
    "person": {"person"},
    "place": {"place", "touristdestination", "museum", "library", "park", "stadium"},
}


def _family_of(t: str) -> str:
    for fam, members in _TYPE_FAMILIES.items():
        if t in members:
            return fam
    return "other:" + t  # unknown type is only compatible with itself


def _incompatible_types(types: set) -> bool:
    """True when a set of types spans more than one schema.org family."""
    fams = {_family_of(t) for t in types if t and t != "thing"}
    return len(fams) > 1


def detect_contradiction(graph) -> List[Observation]:
    items, urls, arts = [], [], []
    for n in graph.articles():
        dp, dm = n.get("datePublished"), n.get("dateModified")
        if isinstance(dp, str) and isinstance(dm, str) and dm[:10] and dm[:10] < dp[:10]:
            items.append({"url": n.source_url, "entity": n.name(),
                          "mismatch": f"dateModified {dm[:10]} precedes datePublished {dp[:10]}"})
            urls.append(n.source_url)
            arts.append({"type": "contradiction", "label": n.source_url,
                         "value": f"modified<published: {dm[:10]}<{dp[:10]}"})
    by_id: Dict[str, set] = {}
    for n in graph.nodes:
        if n.node_id:
            by_id.setdefault(n.node_id, set()).add(n.primary_type())
    for nid, types in by_id.items():
        # Reusing one @id across types is only a contradiction when the types are
        # genuinely incompatible. LocalBusiness IS an Organization, AboutPage IS a
        # WebPage, etc., so sharing an @id within one schema.org family is valid.
        if len(types) > 1 and _incompatible_types({t.lower() for t in types}):
            items.append({"url": "", "entity": nid,
                          "mismatch": f"@id reused across incompatible types {sorted(types)}"})
            arts.append({"type": "contradiction", "label": nid, "value": f"types={sorted(types)}"})
    if not items:
        return []
    ex = items[0]
    return [_obs(
        "identity.schema_contradiction", Tier.CONFIRMED, urls or [""],
        affected=ex["entity"],
        mismatch=ex["mismatch"],
        expected="Structured data must be internally consistent",
        mechanism="Contradictory structured data makes the entity untrustworthy; consumers may discard "
                  "it or surface a wrong value.",
        fix="Reconcile the conflicting values so the structured data is internally consistent.",
        evidence=f"{len(items)} structured-data contradiction(s); e.g. {ex['entity']}: {ex['mismatch']}.",
        items=items, score=0.9, artifacts=arts,
    )]


# --------------------------------------------------------------------------
# 8. Visible / schema mismatch (stale or wrong structured data)
# --------------------------------------------------------------------------
def detect_visible_mismatch(graph, visible) -> List[Observation]:
    from audit_common.dom import name_mentioned
    items, urls, arts = [], [], []
    for n in graph.products():
        text = visible.get(n.source_url, "")
        name = n.name()
        if name and len(text) > 30 and not name_mentioned(name, text):
            items.append({"url": n.source_url, "entity": name, "kind": "name",
                          "mismatch": f"schema name '{name}' not present in visible text"})
            urls.append(n.source_url)
            arts.append({"type": "mismatch", "label": n.source_url, "value": f"name '{name}' absent from page"})
    for off in graph.offers():
        text = visible.get(off.source_url, "")
        price = off.get("price")
        vis = _visible_prices(text)
        try:
            pval = round(float(re.sub(r"[^\d.]", "", str(price))), 2) if price is not None else None
        except ValueError:
            pval = None
        if pval is not None and vis and pval not in vis:
            items.append({"url": off.source_url, "entity": f"Offer {pval}", "kind": "price",
                          "mismatch": f"schema price {pval} not shown in visible text {sorted(vis)}"})
            urls.append(off.source_url)
            arts.append({"type": "mismatch", "label": off.source_url,
                         "value": f"schema={pval} displayed={sorted(vis)}"})
    if not items:
        return []
    ex = items[0]
    return [_obs(
        "identity.schema_visible_mismatch", Tier.LIKELY, urls,
        affected=ex["entity"],
        mismatch=ex["mismatch"],
        expected="Structured data must match what the page actually shows",
        mechanism="When structured data disagrees with the visible page, an assistant may quote a stale or "
                  "wrong value that a human never sees — eroding trust and accuracy.",
        fix="Regenerate the structured data from the same source as the visible content so they agree.",
        evidence=f"{len(items)} entity(ies) have structured data that disagrees with the visible page; e.g. "
                 f"{ex['mismatch']} on {ex['url']}.",
        items=items, score=0.7, artifacts=arts,
    )]


# --------------------------------------------------------------------------
# 9. Duplicate / conflicting entities
# --------------------------------------------------------------------------
def detect_duplicate_entity(graph) -> List[Observation]:
    orgs = graph.organizations()
    names = [(o.name(), o.source_url) for o in orgs if o.name()]
    conflicts = []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i][0], names[j][0]
            if eg.names_equivalent(a, b):
                continue
            if eg.normalize_name(a) != eg.normalize_name(b) and not eg.names_similar(a, b):
                conflicts.append((a, b, names[i][1], names[j][1]))
    if not conflicts:
        return []
    a, b, ua, ub = conflicts[0]
    urls = sorted({u for c in conflicts for u in (c[2], c[3]) if u})
    return [_obs(
        "identity.duplicate_entity", Tier.LIKELY, urls,
        affected=f"{a} / {b}",
        mismatch=f"multiple distinct primary Organization entities: '{a}' and '{b}'",
        expected="A site should expose one canonical Organization entity",
        mechanism="Two conflicting primary organizations make it impossible for a machine to decide which "
                  "entity owns the site, splitting or misattributing the brand.",
        fix="Consolidate to a single Organization entity (use one @id) and reference it everywhere via @id.",
        evidence=f"Multiple conflicting Organization entities found: '{a}' and '{b}'.",
        items=[{"a": a, "b": b} for (a, b, _u, _v) in conflicts], score=0.7,
        artifacts=[{"type": "duplicate", "label": "organizations", "value": f"'{a}' vs '{b}'"}],
    )]


# --------------------------------------------------------------------------
# 17/16. Organization / product naming ambiguity (similar names)
# --------------------------------------------------------------------------
def detect_entity_ambiguous(graph) -> List[Observation]:
    named = [(n.name(), n.source_url) for n in (graph.organizations() + graph.brands()) if n.name()]
    pairs = []
    for i in range(len(named)):
        for j in range(i + 1, len(named)):
            a, b = named[i][0], named[j][0]
            if a.strip().lower() != b.strip().lower() and eg.names_similar(a, b):
                pairs.append((a, b, named[i][1], named[j][1]))
    if not pairs:
        return []
    a, b, ua, ub = pairs[0]
    urls = sorted({u for p in pairs for u in (p[2], p[3]) if u})
    return [_obs(
        "identity.entity_ambiguous", Tier.LIKELY, urls,
        affected=f"{a} / {b}",
        mismatch=f"confusingly similar brand names: '{a}' and '{b}'",
        expected="Each brand entity should have one unambiguous, disambiguated name",
        mechanism="Near-identical brand names with no disambiguators (sameAs, legalName, distinct @id) let "
                  "an assistant conflate or mis-attribute the brands.",
        fix="Use one consistent brand name, or disambiguate with legalName, distinct @id, and sameAs links.",
        evidence=f"Confusingly similar brand names in structured data: '{a}' and '{b}'.",
        items=[{"a": a, "b": b} for (a, b, _u, _v) in pairs], score=0.65,
        artifacts=[{"type": "ambiguous", "label": "brands", "value": f"'{a}' ~ '{b}'"}],
    )]


# --------------------------------------------------------------------------
# 13. WebSite/WebPage/Product relationships — disconnected entity graph
# --------------------------------------------------------------------------
def detect_entity_unlinked(graph) -> List[Observation]:
    orgs = graph.organizations()
    if not orgs:
        return []
    org_tokens = set()
    for o in orgs:
        if o.name():
            org_tokens.add(o.name().strip().lower())
        if o.node_id:
            org_tokens.add(o.node_id.strip().lower())
        if o.url():
            org_tokens.add(o.url().strip().lower())
    items, urls, arts = [], [], []
    for p in graph.products():
        refs = graph.references(p)
        if not (refs & org_tokens):
            label = p.name() or "product"
            items.append({"url": p.source_url, "entity": label})
            urls.append(p.source_url)
            arts.append({"type": "unlinked", "label": p.source_url, "value": f"{label} has no brand/publisher link"})
    if not items:
        return []
    ex = items[0]
    return [_obs(
        "identity.entity_unlinked", Tier.LIKELY, urls,
        affected=ex["entity"],
        mismatch="Product entity is not linked to the site's Organization/Brand entity",
        expected="Product should reference its brand/manufacturer (and pages should reference the Organization)",
        mechanism="Without an edge from the product to the brand entity, a machine cannot attribute the "
                  "product to the company, weakening brand-level answers.",
        fix="Add a `brand` (or `manufacturer`) reference on the Product pointing to the Organization @id.",
        evidence=f"{len(items)} Product entity(ies) are not linked to the Organization/Brand; e.g. "
                 f"'{ex['entity']}' on {ex['url']}.",
        items=items, score=0.65, artifacts=arts,
    )]


# --------------------------------------------------------------------------
# 14. sameAs links present but broken/malformed
# --------------------------------------------------------------------------
def detect_sameas_broken(graph) -> List[Observation]:
    items, urls, arts = [], [], []
    for o in graph.organizations():
        sa = o.get("sameAs")
        if sa is None:
            continue
        entries = sa if isinstance(sa, list) else [sa]
        bad = [str(e) for e in entries if not re.match(r"^https?://\S+\.\S+", str(e).strip())]
        if bad:
            items.append({"url": o.source_url, "entity": o.name(), "bad": bad})
            urls.append(o.source_url)
            arts.append({"type": "sameas", "label": o.source_url, "value": "; ".join(bad)[:160]})
    if not items:
        return []
    ex = items[0]
    return [_obs(
        "identity.sameas_broken", Tier.LIKELY, urls,
        affected=ex["entity"] or "Organization",
        mismatch=f"sameAs contains non-URL/malformed entries: {ex['bad']}",
        expected="sameAs entries must be absolute, resolvable profile URLs",
        mechanism="Malformed sameAs links fail to ground the entity to known references, so disambiguation "
                  "against Wikidata/social profiles breaks.",
        fix="Replace the malformed sameAs entries with absolute https URLs to canonical profiles.",
        evidence=f"{len(items)} Organization(s) have malformed sameAs entries; e.g. {ex['bad']} on {ex['url']}.",
        items=items, score=0.7, artifacts=arts,
    )]


# --------------------------------------------------------------------------
# 15. Canonical / entity consistency
# --------------------------------------------------------------------------
def detect_canonical_mismatch(graph) -> List[Observation]:
    items, urls, arts = [], [], []
    for n in graph.organizations() + graph.websites() + graph.products():
        u = n.url()
        if not u.startswith("http") or not n.source_url:
            continue
        eh, ph = _host(u), _host(n.source_url)
        if eh and ph and eh != ph:
            items.append({"url": n.source_url, "entity": n.name() or n.primary_type(),
                          "entity_host": eh, "page_host": ph})
            urls.append(n.source_url)
            arts.append({"type": "host_mismatch", "label": n.source_url, "value": f"{eh} != {ph}"})
    if not items:
        return []
    ex = items[0]
    return [_obs(
        "identity.canonical_entity_mismatch", Tier.LIKELY, urls,
        affected=ex["entity"],
        mismatch=f"entity url host '{ex['entity_host']}' differs from page host '{ex['page_host']}'",
        expected="An entity's url/@id host should match the site it is served on",
        mechanism="An entity whose url points to a different host looks like a copied or wrong entity, so "
                  "machines may attribute the page to the other domain.",
        fix="Point the entity url/@id at this site's canonical host (or serve it from the matching domain).",
        evidence=f"{len(items)} entity(ies) declare a url on a different host; e.g. {ex['entity']} -> "
                 f"{ex['entity_host']} on page {ex['page_host']}.",
        items=items, score=0.6, artifacts=arts,
    )]


def run_all(records) -> List[Observation]:
    graph = eg.build_graph(records)
    visible = {}
    for r in records:
        visible[getattr(r, "url", "")] = (getattr(r, "rendered_text", "") or getattr(r, "raw_text", "") or "")
    out: List[Observation] = []
    detectors = [
        lambda: detect_malformed(records),
        lambda: detect_type_inappropriate(graph),
        lambda: detect_schema_invalid(graph),
        lambda: detect_contradiction(graph),
        lambda: detect_visible_mismatch(graph, visible),
        lambda: detect_duplicate_entity(graph),
        lambda: detect_entity_ambiguous(graph),
        lambda: detect_entity_unlinked(graph),
        lambda: detect_sameas_broken(graph),
        lambda: detect_canonical_mismatch(graph),
    ]
    for det in detectors:
        try:
            out += det()
        except Exception:
            continue
    return out
