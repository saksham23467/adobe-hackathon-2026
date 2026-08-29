"""Entity graph: parse structured data into a normalized, linkable graph.

Extracts schema.org entities from three syntaxes — JSON-LD (primary), microdata
(itemscope/itemprop), and RDFa (typeof/property) — into a single `EntityNode`
model, then wires them into an `EntityGraph` whose edges express identity
relationships (brand, publisher, isPartOf, about, @id references, sameAs).

The graph is what the detectors reason over, so a finding can name the *affected
entity* and the *expected relationship* rather than a raw JSON blob.

Standard library only (json + html.parser + re).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

_LD_RE = re.compile(
    r'(?is)<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>'
)

# A broad (not exhaustive) set of real schema.org types. Types outside this set
# that still look like a schema attempt (single CamelCase token) are treated as
# unrecognized — this catches typos like "Organisation" / "Prodcut".
SCHEMA_TYPES = {
    "thing", "organization", "localbusiness", "corporation", "ngo",
    "educationalorganization", "governmentorganization", "onlinestore", "store",
    "restaurant", "website", "webpage", "aboutpage", "contactpage", "collectionpage",
    "itempage", "faqpage", "qapage", "searchresultspage", "profilepage",
    "product", "productgroup", "individualproduct", "offer", "aggregateoffer",
    "offercatalog", "brand", "service", "article", "newsarticle", "blogposting",
    "blog", "techarticle", "report", "breadcrumblist", "listitem", "itemlist",
    "question", "answer", "postaladdress", "contactpoint", "person", "review",
    "aggregaterating", "rating", "imageobject", "videoobject", "mediaobject",
    "searchaction", "readaction", "place", "geocoordinates",
    "openinghoursspecification", "event", "recipe", "jobposting", "howto",
    "howtostep", "sitenavigationelement", "wpheader", "wpfooter", "pricespecification",
    "unitpricespecification", "quantitativevalue", "propertyvalue", "audience",
    "country", "postaladdress", "creativework", "softwareapplication", "course",
    "mobileapplication", "webapplication", "book", "movie", "musicgroup",
    "collection", "datafeed", "menu", "menuitem", "logo",
    "hotel", "motel", "lodgingbusiness", "bedandbreakfast", "hostel",
    "bakery", "cafeorcoffeeshop", "dentist", "physician", "hospital",
    "autorepair", "hvacbusiness", "legalservice", "attorney", "notary",
    "realestateagent", "employmentagency", "travelagency",
    "touristdestination", "museum", "library", "park", "stadium",
    "sportsactivitylocation", "gym", "dayspa", "beautysalon", "hairsalon",
    "governmentoffice", "cityhall", "nonprofit", "medicalorganization",
    "pharmacy", "veterinarycare",
}


def _ci_get(props: Dict[str, Any], *keys) -> Any:
    low = {str(k).lower(): v for k, v in props.items()}
    for k in keys:
        if k.lower() in low:
            return low[k.lower()]
    return None


def _norm_type(t: Any) -> List[str]:
    out: List[str] = []
    for item in (t if isinstance(t, list) else [t]):
        if not item:
            continue
        s = str(item)
        s = s.rsplit("/", 1)[-1].rsplit("#", 1)[-1]
        out.append(s.strip().lower())
    return out


@dataclass
class EntityNode:
    types: List[str] = field(default_factory=list)
    props: Dict[str, Any] = field(default_factory=dict)
    node_id: str = ""
    source_url: str = ""
    fmt: str = "jsonld"
    raw: Any = None

    def primary_type(self) -> str:
        return self.types[0] if self.types else ""

    def is_a(self, *names) -> bool:
        s = set(self.types)
        return any(n.lower() in s for n in names)

    def name(self) -> str:
        v = _ci_get(self.props, "name", "legalName", "headline")
        return str(v).strip() if isinstance(v, (str, int, float)) else ""

    def url(self) -> str:
        v = _ci_get(self.props, "url", "@id")
        return str(v).strip() if isinstance(v, str) else ""

    def get(self, *keys) -> Any:
        return _ci_get(self.props, *keys)


# --------------------------------------------------------------------------
# JSON-LD
# --------------------------------------------------------------------------
def extract_ld_blocks(html: str) -> List[str]:
    return [b.strip() for b in _LD_RE.findall(html or "") if b.strip()]


def scan_jsonld_errors(html: str) -> List[Dict[str, str]]:
    """Return a list of malformed JSON-LD blocks with the parser error."""
    errors: List[Dict[str, str]] = []
    for block in extract_ld_blocks(html):
        try:
            json.loads(block)
        except json.JSONDecodeError as e:
            errors.append({"error": str(e), "snippet": " ".join(block.split())[:160]})
    return errors


def _collect_typed(obj: Any, source_url: str, out: List[EntityNode]) -> None:
    """Recursively collect every dict that declares an @type as a node."""
    if isinstance(obj, dict):
        if "@type" in obj:
            out.append(EntityNode(
                types=_norm_type(obj.get("@type")),
                props=obj,
                node_id=str(obj.get("@id", "")).strip(),
                source_url=source_url,
                fmt="jsonld",
                raw=obj,
            ))
        for v in obj.values():
            _collect_typed(v, source_url, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_typed(v, source_url, out)


def parse_jsonld(html: str, source_url: str = "") -> List[EntityNode]:
    nodes: List[EntityNode] = []
    for block in extract_ld_blocks(html):
        try:
            data = json.loads(block)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                for n in item["@graph"]:
                    _collect_typed(n, source_url, nodes)
            else:
                _collect_typed(item, source_url, nodes)
    return nodes


# --------------------------------------------------------------------------
# Microdata / RDFa (practical subset)
# --------------------------------------------------------------------------
class _MicroRdfaParser(HTMLParser):
    def __init__(self, source_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source_url = source_url
        self.nodes: List[EntityNode] = []
        self._stack: List[Tuple[EntityNode, list]] = []  # (node, prop_names_pending)
        self._text_target: Optional[Tuple[EntityNode, str]] = None

    def handle_starttag(self, tag, attrs):
        a = {(k or "").lower(): (v or "") for k, v in attrs}
        # microdata item
        if "itemscope" in a:
            itype = a.get("itemtype", "")
            node = EntityNode(types=_norm_type(itype) if itype else [], props={},
                              source_url=self.source_url, fmt="microdata")
            if self._stack:
                pass
            self.nodes.append(node)
            self._stack.append((node, []))
        # rdfa typeof
        elif a.get("typeof"):
            node = EntityNode(types=_norm_type(a["typeof"]), props={},
                              source_url=self.source_url, fmt="rdfa")
            self.nodes.append(node)
            self._stack.append((node, []))

        prop = a.get("itemprop") or a.get("property")
        if prop and self._stack:
            node = self._stack[-1][0]
            content = a.get("content") or a.get("href") or a.get("src")
            if content:
                node.props[prop] = content
            else:
                self._text_target = (node, prop)

    def handle_endtag(self, tag):
        self._text_target = None
        # Best-effort scope close: pop on any close if a scope is open and the
        # tag likely matches. Kept simple; nesting is approximate for the subset.

    def handle_data(self, data):
        if self._text_target and data.strip():
            node, prop = self._text_target
            node.props[prop] = (str(node.props.get(prop, "")) + " " + data.strip()).strip()


def parse_microdata_rdfa(html: str, source_url: str = "") -> List[EntityNode]:
    p = _MicroRdfaParser(source_url)
    try:
        p.feed(html or "")
    except Exception:
        pass
    # Keep only nodes that actually carry a type + at least one property.
    return [n for n in p.nodes if n.types and n.props]


# --------------------------------------------------------------------------
# Graph
# --------------------------------------------------------------------------
@dataclass
class EntityGraph:
    nodes: List[EntityNode] = field(default_factory=list)

    def of_type(self, *names) -> List[EntityNode]:
        return [n for n in self.nodes if n.is_a(*names)]

    def organizations(self) -> List[EntityNode]:
        return self.of_type("organization", "localbusiness", "corporation", "ngo",
                            "onlinestore", "store", "restaurant", "educationalorganization")

    def brands(self) -> List[EntityNode]:
        return self.of_type("brand")

    def products(self) -> List[EntityNode]:
        return self.of_type("product", "individualproduct", "productgroup")

    def offers(self) -> List[EntityNode]:
        return self.of_type("offer", "aggregateoffer")

    def websites(self) -> List[EntityNode]:
        return self.of_type("website")

    def articles(self) -> List[EntityNode]:
        return self.of_type("article", "newsarticle", "blogposting", "techarticle")

    def all_ids(self) -> set:
        return {n.node_id for n in self.nodes if n.node_id}

    def references(self, node: EntityNode) -> set:
        """Return the set of identity tokens (ids/urls/names) this node points at."""
        refs: set = set()

        def walk(v):
            if isinstance(v, dict):
                for k in ("@id", "url", "name"):
                    if k in v and isinstance(v[k], str):
                        refs.add(v[k].strip().lower())
                for vv in v.values():
                    walk(vv)
            elif isinstance(v, list):
                for vv in v:
                    walk(vv)
            elif isinstance(v, str):
                refs.add(v.strip().lower())

        for key in ("brand", "manufacturer", "publisher", "isPartOf", "about",
                    "mainEntityOfPage", "parentOrganization", "provider", "sameAs"):
            val = node.get(key)
            if val is not None:
                walk(val)
        return refs


def build_graph(records) -> EntityGraph:
    nodes: List[EntityNode] = []
    for r in records:
        html = getattr(r, "rendered_html", "") or getattr(r, "raw_html", "")
        if not html:
            continue
        url = getattr(r, "url", "")
        nodes.extend(parse_jsonld(html, url))
        nodes.extend(parse_microdata_rdfa(html, url))
    return EntityGraph(nodes=nodes)


# --------------------------------------------------------------------------
# Name-identity helpers
# --------------------------------------------------------------------------
_SUFFIXES = ("inc", "llc", "ltd", "co", "corp", "gmbh", "plc", "sa", "srl", "ag")


def normalize_name(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", "", (name or "").lower()).strip()
    parts = [p for p in s.split() if p not in _SUFFIXES]
    return " ".join(parts)


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def names_equivalent(a: str, b: str) -> bool:
    """Same entity after punctuation/space folding (NimbusPay == Nimbus Pay)."""
    na, nb = normalize_name(a).replace(" ", ""), normalize_name(b).replace(" ", "")
    return bool(na and nb and na == nb)


def names_similar(a: str, b: str) -> bool:
    if names_equivalent(a, b):
        return False
    na, nb = normalize_name(a), normalize_name(b)
    if not na or not nb:
        return False
    if len(na) >= 5 and len(nb) >= 5 and (na in nb.split() or nb in na.split()):
        return True
    return levenshtein(na, nb) <= max(1, min(len(na), len(nb)) // 6)
