"""Extract high-value factual claims and content dates from a site (on-site only).

This is the deterministic first half of the freshness-corroboration pipeline:
before ever touching the network we identify what the site actually *claims*, so
external checks can be applied conservatively and only where a specific claim
exists. Standard library only.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

_LD_RE = re.compile(
    r'(?is)<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>'
)
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], 1)}
for _abbr, _i in list(_MONTHS.items()):
    _MONTHS[_abbr[:3]] = _i

_YEAR = r"(?:18|19|20)\d{2}"

# NOTE: bare "since" is deliberately excluded — "since 2021" is almost never a
# founding statement and is the single biggest source of false founding years on
# content-heavy sites. Only phrases that actually assert a founding are matched.
_FOUNDING_RE = re.compile(
    r"\b(?:founded|established|est\.|incorporated|founding year|"
    r"in business since|serving since|operating since)\b[^.\d]{0,20}(" + _YEAR + r")",
    re.I,
)
# Case-sensitive on purpose: lowercase "us" is first person, but "US" (as in
# "US$", "US-based") is a country/currency and must not count as first person.
_FIRST_PERSON_RE = re.compile(r"\b(?:[Ww]e|[Ww]e['’]re|[Ww]e['’]ve|[Oo]ur|[Oo]urs|us)\b")
# Capitalized tokens that a name-shaped regex catches but which are never a
# person's given name (verbs, articles, headline words).
_NON_NAME_TOKENS = {
    "is", "are", "was", "were", "be", "been", "has", "have", "had",
    "says", "said", "announces", "announced", "unveils", "reveals", "launches",
    "plans", "planning", "plan", "the", "a", "an", "in", "on", "of", "and",
    "or", "to", "for", "with", "by", "this", "that", "it", "he", "she", "they",
    "we", "our", "new", "now", "here", "how", "why", "what", "when",
}
# Generic/pronoun subjects that still refer to the site's own brand.
_GENERIC_SUBJECTS = {
    "we", "our", "it", "they", "the company", "the business", "the firm",
    "the organization", "the organisation", "this company", "the site",
    "the brand", "the team", "company", "business", "firm", "organization",
    "organisation", "this", "that",
}
# Captures an explicit subject that precedes a founding verb ("Google was founded").
_FOUNDING_SUBJECT_RE = re.compile(
    r"([A-Z][A-Za-z0-9&.\-']+(?:\s+[A-Z][A-Za-z0-9&.\-']+){0,3})\s*,?\s*"
    r"(?:was|were|is|are|had been|which was|that was)?\s*"
    r"(?:founded|established|incorporated)\b"
)
_MARKETING_PAGES = {
    "home", "about", "product", "products", "pricing",
    "landing", "services", "service",
}
_LEADERSHIP_RE = re.compile(
    r"\b(CEO|CTO|CFO|COO|Founder|Co-?founder|President|Owner|Chief Executive(?:\s+Officer)?)\b"
    r"[\s:,\-–—]*(?:is|are|:)?\s*([A-Z][a-z]+(?:\s+[A-Z]\.?)?\s+[A-Z][a-z]+)",
)
_STATUS_PATTERNS = {
    "open_247": r"\bopen\s+24/?7\b|\bopen\s+24\s+hours\b|\balways\s+open\b",
    "permanently_closed": r"\bpermanently\s+closed\b|\bno\s+longer\s+(?:operating|in\s+business|trading)\b|\bwe\s+have\s+closed\b",
    "temporarily_closed": r"\btemporarily\s+closed\b|\bclosed\s+until\s+further\s+notice\b",
    "coming_soon": r"\bcoming\s+soon\b|\bopening\s+soon\b|\blaunching\s+soon\b",
}
_ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

_SUPERLATIVES = [
    "#1", "number one", "no. 1", "world's best", "voted best",
    "largest ", "unbeatable", "second to none", "fastest-growing", "fastest growing",
]
_SUPPORT_CUE_RE = re.compile(
    r"(?:according to|ranked|voted|awarded|award from|certified by|accredited by|"
    r"source[:\s]|based on|per\s+[A-Z]|by\s+[A-Z][A-Za-z]+\s+"
    r"(?:Magazine|Times|Institute|Association|Awards|Review|Report|Gartner|Forrester)|"
    r"\b" + _YEAR + r"\b)",
    re.I,
)


def strip_text(html: str) -> str:
    # Delegate to the shared, memoized stripper so text extraction is computed
    # once per page and reused across skills (byte-identical output).
    try:
        from audit_common.artifacts import shared_strip
        return shared_strip(html)
    except Exception:
        if not html:
            return ""
        html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
        return re.sub(r"\s+", " ", _TAG_RE.sub(" ", html)).strip()


def parse_date(raw: str) -> Optional[_dt.date]:
    raw = (raw or "").strip()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        try:
            return _dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{1,2}),?\s+(" + _YEAR + r")", raw)
    if m and m.group(1).lower() in _MONTHS:
        try:
            return _dt.date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
        except ValueError:
            return None
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,9})\.?\s+(" + _YEAR + r")", raw)
    if m and m.group(2).lower() in _MONTHS:
        try:
            return _dt.date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
        except ValueError:
            return None
    return None


@dataclass
class ContentDate:
    kind: str          # dateModified|datePublished|time|meta_modified|last_updated|copyright
    date: _dt.date
    raw: str = ""
    url: str = ""
    page_class: str = ""


def extract_dates(html: str, text: Optional[str] = None) -> List[ContentDate]:
    out: List[ContentDate] = []
    for block in _LD_RE.findall(html or ""):
        for key in ("dateModified", "datePublished"):
            for m in re.finditer(r'"' + key + r'"\s*:\s*"([^"]+)"', block):
                d = parse_date(m.group(1))
                if d:
                    out.append(ContentDate(key, d, m.group(1)))
    for m in re.finditer(r'(?is)<time[^>]*\bdatetime\s*=\s*["\']([^"\']+)["\']', html or ""):
        d = parse_date(m.group(1))
        if d:
            out.append(ContentDate("time", d, m.group(1)))
    for m in re.finditer(
        r'(?is)<meta[^>]*(?:property|name)\s*=\s*["\'](?:article:modified_time|og:updated_time|'
        r'last-modified|lastmod)["\'][^>]*\bcontent\s*=\s*["\']([^"\']+)["\']', html or ""):
        d = parse_date(m.group(1))
        if d:
            out.append(ContentDate("meta_modified", d, m.group(1)))
    text = text if text is not None else strip_text(html)
    for m in re.finditer(r"(?i)(?:last updated|updated on|last modified|reviewed on)[:\s]+([^.\n<]{4,30})", text):
        d = parse_date(m.group(1))
        if d:
            out.append(ContentDate("last_updated", d, m.group(1).strip()))
    for m in re.finditer(r"(?i)(?:©|copyright|&copy;)\s*(" + _YEAR + r")", text):
        try:
            out.append(ContentDate("copyright", _dt.date(int(m.group(1)), 1, 1), m.group(0)))
        except ValueError:
            pass
    return out


def content_dates(dates: List[ContentDate]) -> List[ContentDate]:
    return [d for d in dates if d.kind != "copyright"]


@dataclass
class Claim:
    category: str
    value: str
    url: str
    evidence: str = ""
    strength: str = "normal"  # normal | strong
    brand_tied: bool = True   # is this claim actually *about the audited brand*?


@dataclass
class SiteClaims:
    brand_name: str = ""
    has_sameas: bool = False
    founding_years: Set[str] = field(default_factory=set)
    leadership: Dict[str, str] = field(default_factory=dict)   # role -> name
    statuses: Set[str] = field(default_factory=set)
    postal_codes: Set[str] = field(default_factory=set)
    hq_city: str = ""
    claims: List[Claim] = field(default_factory=list)
    dates: List[ContentDate] = field(default_factory=list)


def _brand_from(html: str) -> str:
    for block in _LD_RE.findall(html or ""):
        m = re.search(r'"@type"\s*:\s*"(?:Organization|LocalBusiness|Corporation)"', block)
        if m:
            nm = re.search(r'"name"\s*:\s*"([^"]+)"', block)
            if nm:
                return nm.group(1).strip()
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
    if m:
        return re.split(r"[|\-–—]", strip_text(m.group(1)))[0].strip()
    return ""


def _schema_founding_years(html: str) -> Set[str]:
    """Authoritative founding years declared on an Organization-ish entity."""
    out: Set[str] = set()
    for block in _LD_RE.findall(html or ""):
        if re.search(
            r'"@type"\s*:\s*"(?:Organization|LocalBusiness|Corporation|NGO|Company)"', block
        ):
            for m in re.finditer(r'"foundingDate"\s*:\s*"([^"]+)"', block):
                ym = re.search(_YEAR, m.group(1))
                if ym:
                    out.add(ym.group(0))
    return out


def _first_person(sentence: str) -> bool:
    return bool(_FIRST_PERSON_RE.search(sentence or ""))


def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _mentions_brand(sentence: str, brand: str) -> bool:
    brand = (brand or "").strip()
    if len(brand) < 2:
        return False
    return bool(re.search(r"\b" + re.escape(brand) + r"\b", sentence or "", re.I))


def _founding_brand_tied(sentence: str, brand: str) -> bool:
    """A founding-year sentence counts only if it is about the audited brand.

    Rejects "Google was founded in 1998" (explicit third-party subject) while
    accepting first-person, brand-named, or unattributed founding statements.
    """
    if _first_person(sentence):
        return True
    if _mentions_brand(sentence, brand):
        return True
    m = _FOUNDING_SUBJECT_RE.search(sentence or "")
    if m:
        subj = m.group(1).strip()
        if brand and _norm_name(subj) == _norm_name(brand):
            return True
        if subj.lower() not in _GENERIC_SUBJECTS:
            return False  # explicit third-party subject -> not our brand
    return True  # unattributed founding statement -> assume the site's own brand


def _marketing_brand_tied(sentence: str, brand: str, page_class: str) -> bool:
    return (
        _first_person(sentence)
        or _mentions_brand(sentence, brand)
        or (page_class or "") in _MARKETING_PAGES
    )


def extract_site_claims(records) -> SiteClaims:
    sc = SiteClaims()
    recs = list(records)

    # Pass 1 — establish brand identity and authoritative schema signals first, so
    # brand-tie tests in pass 2 have a brand name to compare against.
    for r in recs:
        html = getattr(r, "rendered_html", "") or getattr(r, "raw_html", "")
        if not html:
            continue
        if not sc.brand_name:
            sc.brand_name = _brand_from(html)
        if not sc.has_sameas and re.search(r'"sameAs"\s*:', html):
            sc.has_sameas = True
        for y in _schema_founding_years(html):
            if y not in sc.founding_years:
                sc.founding_years.add(y)
                sc.claims.append(Claim("founding_year", y, getattr(r, "url", ""),
                                       evidence="Organization.foundingDate", brand_tied=True))
    brand = sc.brand_name

    # Pass 2 — mine free-text claims, only keeping those tied to the brand.
    for r in recs:
        html = getattr(r, "rendered_html", "") or getattr(r, "raw_html", "")
        if not html:
            continue
        url = getattr(r, "url", "")
        pclass = getattr(r, "page_class", "other")
        # Reuse the text extracted once at acquisition (shared page artifact)
        # instead of re-stripping this page's HTML again.
        try:
            from audit_common.artifacts import visible_text
            text = visible_text(r)
        except Exception:
            text = strip_text(html)

        for m in _FOUNDING_RE.finditer(text):
            y = m.group(1)
            sentence = _sentence_around(text, m.start())
            if not _founding_brand_tied(sentence, brand):
                continue
            sc.founding_years.add(y)
            sc.claims.append(Claim("founding_year", y, url, evidence=_snip(text, y),
                                   brand_tied=True))
        for m in _LEADERSHIP_RE.finditer(text):
            role, name = m.group(1), (m.group(2) or "").strip()
            toks = name.split()
            if not toks or toks[0].lower() in _NON_NAME_TOKENS or toks[-1].lower() in _NON_NAME_TOKENS:
                continue  # headline/verb noise, not a real person name
            sentence = _sentence_around(text, m.start())
            if not _marketing_brand_tied(sentence, brand, pclass):
                continue  # a leadership fact about some other entity, not this brand
            role_n = re.sub(r"\s+", " ", role).strip().lower()
            sc.leadership.setdefault(role_n, name)
            sc.claims.append(Claim("leadership", f"{role_n}: {name}", url,
                                   evidence=_snip(text, name), brand_tied=True))
        for status, pat in _STATUS_PATTERNS.items():
            if re.search(pat, text, re.I):
                sc.statuses.add(status)
                sc.claims.append(Claim("operating_status", status, url,
                                       evidence=_snip(text, status.split("_")[0])))
        low = text.lower()
        for phrase in _SUPERLATIVES:
            idx = low.find(phrase)
            if idx >= 0:
                sentence = _sentence_around(text, idx)
                supported = bool(_SUPPORT_CUE_RE.search(sentence))
                tied = _marketing_brand_tied(sentence, brand, pclass)
                sc.claims.append(Claim("marketing_superlative", phrase.strip(), url,
                                       evidence=sentence,
                                       strength="strong" if not supported else "normal",
                                       brand_tied=tied))
        for d in extract_dates(html, text=text):
            d.url = url
            d.page_class = pclass
            sc.dates.append(d)
    return sc


def _snip(text: str, needle: str, width: int = 80) -> str:
    idx = text.lower().find((needle or "").lower())
    if idx < 0:
        return text[:width]
    a, b = max(0, idx - width // 2), min(len(text), idx + len(needle) + width // 2)
    return ("…" if a > 0 else "") + text[a:b].strip() + ("…" if b < len(text) else "")


def _sentence_around(text: str, idx: int) -> str:
    start = max((text.rfind(c, 0, idx) for c in ".!?"), default=-1) + 1
    ends = [e for e in (text.find(c, idx) for c in ".!?") if e >= 0]
    end = min(ends) + 1 if ends else min(len(text), idx + 160)
    return text[start:end].strip()
