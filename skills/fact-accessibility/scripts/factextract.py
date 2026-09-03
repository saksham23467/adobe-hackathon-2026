"""Fact extraction: turn HTML into a machine-readable inventory of brand facts.

This is the deterministic half of the content-extractability pipeline. It parses a
page into headed sections (tracking which are hidden from the accessibility tree),
mines schema.org JSON-LD properties, and provides concrete/vague matchers for the
important fact categories (pricing, hours, location, contact, identity, …).

The heuristic detectors in `factdetect.py` consume this inventory. Standard
library only (html.parser + re + json).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional, Set

_SKIP_TAGS = {"script", "style", "noscript", "template"}
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
_HEADINGS = {"h1", "h2", "h3"}
_ICON_HINTS = ("icon", "logo", "sprite", "avatar", "pixel", "spacer")


def tokens(text: str) -> int:
    return len([t for t in (text or "").split() if t.strip()])


# --------------------------------------------------------------------------
# Category keyword map (headings/intent) — deliberately conservative.
# --------------------------------------------------------------------------
CATEGORY_KEYWORDS = {
    "pricing": ["pricing", "plans", "price", "cost", "rates", "fees", "how much"],
    "hours": ["hours", "opening times", "opening hours", "business hours", "when we are open"],
    "location": ["location", "address", "visit us", "find us", "where we are", "our office", "directions"],
    "contact": ["contact", "get in touch", "reach us", "reach out"],
    "specs": ["specification", "specifications", "specs", "technical details", "dimensions", "tech specs"],
    "faq": ["faq", "faqs", "frequently asked", "questions"],
    "policy": ["policy", "warranty", "guarantee", "returns", "refund", "shipping policy", "eligibility"],
    "features": ["features", "what you get", "capabilities", "highlights"],
    "menu": ["menu"],
}

# --------------------------------------------------------------------------
# Concrete-value detectors
# --------------------------------------------------------------------------
_PRICE_RE = re.compile(
    r"(?:[$€£₹¥]\s?\d[\d,]*(?:\.\d{1,2})?)"
    r"|(?:\b\d[\d,]*(?:\.\d{1,2})?\s?(?:USD|EUR|GBP|INR|JPY|dollars|euros)\b)"
    r"|(?:\b\d[\d,]*(?:\.\d{1,2})?\s?(?:/|per)\s?(?:mo|month|year|yr)\b)",
    re.I,
)
_HOURS_RE = re.compile(
    r"(?:\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?\s*[-–—to]+\s*(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*)"
    r"|(?:\b\d{1,2}(?::\d{2})?\s?(?:am|pm)\b)"
    r"|(?:open\s+24\s*(?:hours|/7))",
    re.I,
)
_ZIP_RE = re.compile(r"\b\d{5}(?:-\d{4})?\b")
_UK_POSTCODE_RE = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}\b", re.I)
_DE_POSTCODE_RE = re.compile(r"\b\d{5}\s+[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]{2,}\b")
_STREET_RE = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9.\- ]{2,40}\b"
    r"(?:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|way|court|ct|suite|ste|"
    r"strasse|straße|str\.|gasse|weg|platz|rue|avenue|boulevard)\b",
    re.I,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+\d{1,3}[\s.\-]?)?(?:\(?\d{2,4}\)?[\s.\-]?)\d{3,4}[\s.\-]?\d{3,4}(?!\d)"
)
_IDENTITY_RE = re.compile(
    r"\b(?:we|our (?:company|team|mission|firm)|[A-Z][A-Za-z0-9&]+)\b[^.]{0,60}?"
    r"\b(?:is|are|provides?|offers?|helps?|builds?|makes?|designs?|sells?|delivers?|"
    r"creates?|special(?:ize|ise)s?|manufactures?)\b",
)

VAGUE_PHRASES = {
    "pricing": [
        "contact us for pricing", "contact us for a quote", "get a quote", "request a quote",
        "custom pricing", "pricing on request", "pricing available on request", "call for pricing",
        "affordable pricing", "competitive pricing", "reach out for pricing", "let's talk pricing",
        "pricing varies", "flexible pricing",
        # Natural paraphrases seen on real SaaS pricing pages. All are anchored on
        # "pricing" so they cannot misfire on non-pricing copy that merely uses
        # words like "flexible" or "scales".
        "pricing is flexible", "pricing scales", "pricing that scales", "pricing that grows",
        "pricing that fits", "tailored pricing", "personalized pricing", "personalised pricing",
        "pricing depends on", "pricing tailored",
    ],
    "hours": [
        "flexible hours", "by appointment", "hours vary", "call for hours", "varies by season",
        "hours may vary",
    ],
    "location": [
        "serving the greater", "nationwide", "multiple locations", "serving the region",
        "across the country", "serving customers everywhere", "serving the area", "serving all of",
    ],
}


_PROMO_NEAR = re.compile(
    r"(cashback|no[- ]cost\s+emi|\bemi\b|minimum transaction|eligible cards?|"
    r"instant cash|save up to|up to\s.{0,24}(off|cashback)|trade[- ]in|"
    r"on orders?\s+(of|over))",
    re.I,
)
_HOURS_CONTEXT = re.compile(
    r"\b(hours|opening|open|closed|appointment|weekday|weekend)\b",
    re.I,
)


def find_prices(text: str, *, exclude_promo: bool = False) -> Set[float]:
    out: Set[float] = set()
    blob = text or ""
    for m in _PRICE_RE.finditer(blob):
        if exclude_promo:
            window = blob[max(0, m.start() - 90): m.end() + 90]
            if _PROMO_NEAR.search(window):
                continue
        num = re.sub(r"[^\d.]", "", m.group(0))
        try:
            if num:
                out.add(round(float(num), 2))
        except ValueError:
            continue
    return out


def has_concrete_hours(text: str) -> bool:
    return bool(_HOURS_RE.search(text or ""))


def has_concrete_address(text: str) -> bool:
    t = text or ""
    return bool(
        _STREET_RE.search(t)
        or _UK_POSTCODE_RE.search(t)
        or _DE_POSTCODE_RE.search(t)
        or (_ZIP_RE.search(t) and re.search(r"\b(street|st|ave|road|rd|suite|floor)\b", t, re.I))
    )


def find_emails(text: str) -> Set[str]:
    return set(m.group(0) for m in _EMAIL_RE.finditer(text or ""))


def find_phones(text: str) -> Set[str]:
    out = set()
    for m in _PHONE_RE.finditer(text or ""):
        digits = re.sub(r"\D", "", m.group(0))
        if 10 <= len(digits) <= 12:
            out.add(digits[-10:])
    return out


def vague_hit(text: str, category: str) -> Optional[str]:
    low = (text or "").lower()
    for phrase in VAGUE_PHRASES.get(category, []):
        if phrase not in low:
            continue
        if category == "hours" and not _HOURS_CONTEXT.search(snippet(text, phrase, width=70)):
            continue
        return phrase
    return None


def snippet(text: str, needle: str, width: int = 90) -> str:
    if not text:
        return ""
    low = text.lower()
    idx = low.find(needle.lower()) if needle else -1
    if idx < 0:
        return " ".join(text.split())[: width * 2]
    start = max(0, idx - width // 2)
    end = min(len(text), idx + len(needle) + width // 2)
    return ("…" if start > 0 else "") + " ".join(text[start:end].split()) + ("…" if end < len(text) else "")


# --------------------------------------------------------------------------
# Parsed document model
# --------------------------------------------------------------------------
@dataclass
class Section:
    heading: str = ""
    level: int = 0
    text: str = ""
    visible_text: str = ""
    images: List[Dict[str, str]] = field(default_factory=list)

    @property
    def hidden(self) -> bool:
        return tokens(self.text) > 0 and tokens(self.visible_text) == 0

    def category(self) -> Optional[str]:
        h = self.heading.lower()
        for cat, kws in CATEGORY_KEYWORDS.items():
            if any(kw in h for kw in kws):
                return cat
        return None


@dataclass
class Doc:
    title: str = ""
    meta_description: str = ""
    sections: List[Section] = field(default_factory=list)
    images: List[Dict[str, str]] = field(default_factory=list)
    jsonld_nodes: List[dict] = field(default_factory=list)
    has_table: bool = False
    has_dl: bool = False
    text_all: str = ""
    text_visible: str = ""
    question_headings: int = 0

    # -- schema helpers -------------------------------------------------
    def schema_types(self) -> Set[str]:
        out: Set[str] = set()
        for n in self.jsonld_nodes:
            t = n.get("@type")
            if isinstance(t, list):
                out.update(str(x) for x in t)
            elif t:
                out.add(str(t))
        return out

    def has_type(self, *names) -> bool:
        types = {t.lower() for t in self.schema_types()}
        return any(n.lower() in types for n in names)

    def offer_prices(self) -> Set[float]:
        out: Set[float] = set()

        def _scan(node):
            if isinstance(node, dict):
                if "price" in node:
                    try:
                        out.add(round(float(re.sub(r"[^\d.]", "", str(node["price"]))), 2))
                    except (ValueError, TypeError):
                        pass
                for v in node.values():
                    _scan(v)
            elif isinstance(node, list):
                for v in node:
                    _scan(v)

        for n in self.jsonld_nodes:
            _scan(n)
        return out

    def org_description(self) -> str:
        for n in self.jsonld_nodes:
            t = n.get("@type", "")
            t = " ".join(t) if isinstance(t, list) else str(t)
            if t.lower() in ("organization", "localbusiness", "corporation"):
                d = n.get("description")
                if isinstance(d, str) and d.strip():
                    return d.strip()
        return ""

    def schema_has_address(self) -> bool:
        for n in self.jsonld_nodes:
            addr = n.get("address")
            if isinstance(addr, dict) and (addr.get("streetAddress") or addr.get("addressLocality")):
                return True
            if isinstance(addr, str) and len(addr) >= 8:
                return True
            t = n.get("@type", "")
            t = " ".join(t) if isinstance(t, list) else str(t)
            if t.lower() == "postaladdress" and (n.get("streetAddress") or n.get("addressLocality")):
                return True
        return False

    def schema_has_hours(self) -> bool:
        return any("openinghours" in {k.lower() for k in n} for n in self.jsonld_nodes)


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.doc = Doc()
        self._sections: List[Section] = [Section()]
        self._skip: List[str] = []
        self._hidden_depth = 0
        self._stack: List[Dict] = []
        self._in_heading = False
        self._heading_buf: List[str] = []
        self._heading_level = 0
        self._in_title = False
        self._in_ld = False
        self._ld_buf: List[str] = []

    @property
    def _cur(self) -> Section:
        return self._sections[-1]

    @staticmethod
    def _is_hidden(a: Dict[str, str]) -> bool:
        try:
            from audit_common.dom import attrs_hidden
            return attrs_hidden(a)
        except Exception:
            if "hidden" in a:
                return True
            if a.get("aria-hidden", "").lower() == "true":
                return True
            style = a.get("style", "").replace(" ", "").lower()
            return "display:none" in style or "visibility:hidden" in style

    def handle_starttag(self, tag, attrs):
        a = {(k or "").lower(): (v or "") for k, v in attrs}

        if tag == "script":
            if a.get("type", "").lower() == "application/ld+json":
                self._in_ld = True
                self._ld_buf = []
            self._skip.append(tag)
            return
        if tag in _SKIP_TAGS:
            self._skip.append(tag)
            return
        if self._skip:
            return

        if tag == "meta" and a.get("name", "").lower() == "description":
            self.doc.meta_description = a.get("content", "").strip()
        elif tag == "title":
            self._in_title = True
        elif tag == "table":
            self.doc.has_table = True
        elif tag == "dl":
            self.doc.has_dl = True
        elif tag == "img":
            img = {"alt": a.get("alt", ""), "src": a.get("src", ""),
                   "w": a.get("width", ""), "h": a.get("height", "")}
            self.doc.images.append(img)
            self._cur.images.append(img)

        if tag not in _VOID_TAGS:
            hidden = self._is_hidden(a)
            self._stack.append({"tag": tag, "hidden": hidden})
            if hidden:
                self._hidden_depth += 1

        if tag in _HEADINGS:
            # Close the previous section, open a new one for this heading.
            self._sections.append(Section(level=int(tag[1])))
            self._in_heading = True
            self._heading_buf = []
            self._heading_level = int(tag[1])

    def handle_startendtag(self, tag, attrs):
        if tag == "img" and not self._skip:
            a = {(k or "").lower(): (v or "") for k, v in attrs}
            img = {"alt": a.get("alt", ""), "src": a.get("src", ""),
                   "w": a.get("width", ""), "h": a.get("height", "")}
            self.doc.images.append(img)
            self._cur.images.append(img)

    def handle_endtag(self, tag):
        if self._skip:
            if tag == self._skip[-1]:
                popped = self._skip.pop()
                if popped == "script" and self._in_ld:
                    self._in_ld = False
                    self._record_jsonld("".join(self._ld_buf))
            return
        if tag == "title":
            self._in_title = False
        if tag in _HEADINGS and self._in_heading:
            self._in_heading = False
            heading = " ".join(self._heading_buf).strip()
            self._cur.heading = heading
            self._cur.level = self._heading_level
            if heading.endswith("?"):
                self.doc.question_headings += 1
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i]["tag"] == tag:
                for e in self._stack[i:]:
                    if e["hidden"]:
                        self._hidden_depth -= 1
                del self._stack[i:]
                break

    def handle_data(self, data):
        if self._skip:
            if self._in_ld:
                self._ld_buf.append(data)
            return
        if self._in_title:
            if data.strip():
                self.doc.title = (self.doc.title + " " + data.strip()).strip()
            return
        if self._in_heading:
            if data.strip():
                self._heading_buf.append(data.strip())
            return
        if data.strip():
            self._cur.text += " " + data
            if self._hidden_depth == 0:
                self._cur.visible_text += " " + data

    def _record_jsonld(self, raw: str) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            parsed = json.loads(raw)
        except Exception:
            return
        items = parsed if isinstance(parsed, list) else [parsed]
        for item in items:
            if isinstance(item, dict) and "@graph" in item and isinstance(item["@graph"], list):
                self.doc.jsonld_nodes.extend(n for n in item["@graph"] if isinstance(n, dict))
            elif isinstance(item, dict):
                self.doc.jsonld_nodes.append(item)


def extract_doc(html: str) -> Doc:
    p = _Parser()
    try:
        p.feed(html or "")
    except Exception:
        pass
    doc = p.doc
    doc.sections = []
    for s in p._sections:
        s.text = " ".join(s.text.split())
        s.visible_text = " ".join(s.visible_text.split())
        if s.heading or s.text or s.images:
            doc.sections.append(s)
    doc.text_all = " ".join(" ".join(s.text for s in doc.sections).split())
    doc.text_visible = " ".join(" ".join(s.visible_text for s in doc.sections).split())
    return doc


def meaningful_images(images: List[Dict[str, str]]) -> List[Dict[str, str]]:
    out = []
    for img in images:
        src = (img.get("src") or "").lower()
        if any(h in src for h in _ICON_HINTS):
            continue
        w = _to_int(img.get("w"))
        h = _to_int(img.get("h"))
        if (w and w < 64) or (h and h < 64):
            continue
        out.append(img)
    return out


def _to_int(v) -> int:
    try:
        return int(str(v).strip().replace("px", ""))
    except Exception:
        return 0
