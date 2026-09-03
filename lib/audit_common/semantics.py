"""Generic resource/page semantic classifier and finding gate.

Every detector can ask: "is this URL actually in the semantic scope of this
check?" The pipeline is:

    raw observation
      → resource / page classification
      → semantic relevance
      → entity scope
      → confidence
      → finding

Classification is stack-agnostic: it uses file extensions, Content-Type,
body magic, and generic path *shapes*. It never keys off a hostname or a
site-specific directory such as ``/assets/``.

A technically true observation with low semantic relevance must not become a
high-severity finding. Out-of-scope resources (an SVG treated as a "page")
are dropped before a finding is minted.
"""

from __future__ import annotations

import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional, Set, Tuple

from .sampler import is_locale_home_path
from .schema import Confidence, Severity


# --------------------------------------------------------------------------
# Closed vocabularies
# --------------------------------------------------------------------------
RESOURCE_TYPES = (
    "html_document",
    "image",
    "svg",
    "video",
    "audio",
    "pdf",
    "font",
    "javascript",
    "css",
    "api_endpoint",
    "download",
    "archive",
    "other_binary",
    "unknown",
)

PAGE_TYPES = (
    "homepage",
    "landing_page",
    "product",
    "service",
    "article",
    "blog",
    "documentation",
    "category",
    "search",
    "contact",
    "about",
    "legal",
    "account",
    "utility",
    "error",
    "unknown",
)

# Page types a first-time human visitor would treat as "a page I landed on".
HUMAN_FACING_PAGE_TYPES: FrozenSet[str] = frozenset({
    "homepage", "landing_page", "product", "service", "article", "blog",
    "documentation", "category", "contact", "about", "legal", "search",
    "unknown",
})

NON_DOCUMENT_RESOURCES: FrozenSet[str] = frozenset({
    "image", "svg", "video", "audio", "pdf", "font", "javascript", "css",
    "api_endpoint", "download", "archive", "other_binary",
})

MEDIA_RESOURCES: FrozenSet[str] = frozenset({
    "image", "svg", "video", "audio", "font", "javascript", "css",
})

FETCH_OUTCOMES = (
    "ok",
    "confirmed_404",
    "gone_410",
    "server_5xx",
    "blocked_403",
    "authentication_required",
    "robots_restricted",
    "transient",
    "unsupported_resource",
    "other_client_4xx",
)

# Outcomes that support calling a link "broken".
BROKEN_OUTCOMES: FrozenSet[str] = frozenset({
    "confirmed_404", "gone_410", "server_5xx",
})


# --------------------------------------------------------------------------
# Extension / content-type / magic tables (generic, not site-specific)
# --------------------------------------------------------------------------
_EXT_RESOURCE = {
    ".html": "html_document", ".htm": "html_document", ".xhtml": "html_document",
    ".shtml": "html_document",
    ".svg": "svg", ".svgz": "svg",
    ".png": "image", ".jpg": "image", ".jpeg": "image", ".gif": "image",
    ".webp": "image", ".avif": "image", ".ico": "image", ".bmp": "image",
    ".tif": "image", ".tiff": "image",
    ".mp4": "video", ".webm": "video", ".mov": "video", ".m4v": "video",
    ".ogv": "video", ".mkv": "video",
    ".mp3": "audio", ".wav": "audio", ".ogg": "audio", ".m4a": "audio",
    ".aac": "audio", ".flac": "audio",
    ".pdf": "pdf",
    ".woff": "font", ".woff2": "font", ".ttf": "font", ".otf": "font",
    ".eot": "font",
    ".js": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".css": "css",
    ".zip": "archive", ".tar": "archive", ".gz": "archive", ".tgz": "archive",
    ".7z": "archive", ".rar": "archive",
    ".dmg": "download", ".exe": "download", ".msi": "download",
    ".pkg": "download", ".deb": "download", ".rpm": "download",
    ".apk": "download",
    ".ics": "download", ".vcs": "download", ".ical": "download",
    ".bin": "other_binary", ".wasm": "other_binary", ".map": "other_binary",
    ".json": "api_endpoint",
}

_TYPE_PREFIX = (
    ("image/svg", "svg"),
    ("image/", "image"),
    ("video/", "video"),
    ("audio/", "audio"),
    ("font/", "font"),
    ("application/font", "font"),
    ("application/pdf", "pdf"),
    ("text/css", "css"),
    ("text/javascript", "javascript"),
    ("application/javascript", "javascript"),
    ("application/x-javascript", "javascript"),
    ("application/json", "api_endpoint"),
    ("application/ld+json", "api_endpoint"),
    ("application/xml", "unknown"),  # may be a feed or sitemap; not HTML
    ("text/xml", "unknown"),
    ("text/html", "html_document"),
    ("application/xhtml", "html_document"),
    ("application/zip", "archive"),
    ("application/gzip", "archive"),
    ("application/octet-stream", "other_binary"),
)

# Generic path *shapes* that almost always mean an API, never a visitor page.
_API_PATH = re.compile(
    r"/(api|graphql|rest|rpc|jsonrpc)(/|$)|"
    r"/(v\d+)(/|$)|"
    r"/wp-json(/|$)|"
    r"\.json$",
    re.I,
)

_PAGE_PATTERNS = [
    # Utility first: a /fragments/promo path is a CMS include, not a landing page.
    # Generic path *shapes* only (never a hostname or a site-specific folder).
    ("utility", re.compile(
        r"/(sitemap|robots\.txt|feed|rss|atom|"
        r"fragments?|partials?|modals?|includes?|embeds?)(/|$)",
        re.I,
    )),
    ("about", re.compile(r"/(about|company|team|who-we-are|our-story)(/|$)", re.I)),
    ("contact", re.compile(r"/(contact|contacts|get-in-touch)(/|$)", re.I)),
    ("product", re.compile(r"/(products?|shop|store|item|p)/", re.I)),
    ("service", re.compile(r"/(services?)(/|$)", re.I)),
    ("blog", re.compile(r"/(blog)(/|$)", re.I)),
    ("article", re.compile(r"/(news|articles?|posts?|stories|press)(/|$)", re.I)),
    ("documentation", re.compile(r"/(docs?|documentation|guide|guides|help|support)(/|$)", re.I)),
    ("category", re.compile(r"/(categor(y|ies)|collections?|tags?|topics?)(/|$)", re.I)),
    ("search", re.compile(r"/(search|find)(/|$)|[?&](q|query|s)=", re.I)),
    ("legal", re.compile(r"/(legal|privacy|terms|tos|cookies?|imprint|gdpr)(/|$)", re.I)),
    ("account", re.compile(r"/(login|log-in|signin|sign-in|signup|sign-up|account|profile|cart|bag|basket|checkout|auth)(/|$)", re.I)),
    ("landing_page", re.compile(r"/(lp|landing|campaign|promo|offer|go)(/|$)", re.I)),
    ("error", re.compile(r"/(404|410|error|not-found|notfound)(/|$)", re.I)),
]

_INCOMPATIBLE_PAGE_TYPES = {
    frozenset({"homepage", "legal"}),
    frozenset({"homepage", "account"}),
    frozenset({"product", "legal"}),
    frozenset({"product", "account"}),
    frozenset({"service", "legal"}),
}


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SemanticClass:
    resource_type: str
    page_type: str = ""
    entity_scope: str = "unknown"   # site | page | resource | entity
    confidence: str = "medium"      # classification confidence
    evidence: Tuple[str, ...] = ()

    @property
    def is_html_document(self) -> bool:
        return self.resource_type == "html_document"

    @property
    def is_human_facing_page(self) -> bool:
        return self.is_html_document and (
            not self.page_type or self.page_type in HUMAN_FACING_PAGE_TYPES
        )


@dataclass
class CheckScope:
    """What a check is *about*. Detectors consult this before emitting."""
    resource_types: FrozenSet[str]
    page_types: FrozenSet[str] = field(default_factory=lambda: HUMAN_FACING_PAGE_TYPES)
    entity_scope: str = "page"
    # If True, a miss is "none" relevance (drop). If False, low relevance.
    required: bool = True


# Checks that operate on visitor-facing HTML pages (not assets).
_PAGE_ENGAGEMENT = CheckScope(
    resource_types=frozenset({"html_document"}),
    page_types=HUMAN_FACING_PAGE_TYPES,
    entity_scope="page",
    required=True,
)

CHECK_SCOPES: Dict[str, CheckScope] = {
    "engagement.dead_end_page": _PAGE_ENGAGEMENT,
    "engagement.unclear_navigation": _PAGE_ENGAGEMENT,
    "engagement.no_orientation": _PAGE_ENGAGEMENT,
    "engagement.no_ia_navigation": _PAGE_ENGAGEMENT,
    "engagement.content_hidden_interaction": _PAGE_ENGAGEMENT,
    "engagement.blocking_interstitial": _PAGE_ENGAGEMENT,
    "engagement.inconsistent_naming": _PAGE_ENGAGEMENT,
    "engagement.key_info_undiscoverable": _PAGE_ENGAGEMENT,
    "engagement.unexplained_jargon": _PAGE_ENGAGEMENT,
    "engagement.no_viewport_meta": _PAGE_ENGAGEMENT,
    "access.broken_internal_links": CheckScope(
        resource_types=frozenset({"html_document"}),  # source page
        page_types=HUMAN_FACING_PAGE_TYPES | frozenset({"error", "utility", "account"}),
        entity_scope="page",
        required=True,
    ),
    "access.canonical_inconsistent": CheckScope(
        resource_types=frozenset({"html_document"}),
        page_types=HUMAN_FACING_PAGE_TYPES | frozenset({"error"}),
        entity_scope="page",
        required=True,
    ),
    "access.canonical_to_error": CheckScope(
        resource_types=frozenset({"html_document"}),
        entity_scope="page",
        required=True,
    ),
}


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------
def _ext(url: str) -> str:
    path = urllib.parse.urlsplit(url).path or ""
    # Strip trailing slash and a trailing ;session=... that some CDNs add.
    base = os.path.basename(path.rstrip("/"))
    if "." not in base:
        return ""
    return "." + base.rsplit(".", 1)[-1].lower()


def _ctype(content_type: str) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _sniff_body(body: str) -> str:
    if not body:
        return ""
    head = body.lstrip()[:800]
    low = head[:200].lower()
    if low.startswith("%pdf"):
        return "pdf"
    if head.startswith("\x89PNG"):
        return "image"
    if head.startswith("\xff\xd8\xff"):
        return "image"
    if low.startswith("<svg") or (low.startswith("<?xml") and "<svg" in low):
        return "svg"
    if low.startswith("<!doctype html") or low.startswith("<html") or low.startswith("<!doctype html"):
        return "html_document"
    # A bare `<head`/`<body` fragment is still an HTML document.
    if low.startswith("<head") or low.startswith("<body") or low.startswith("<!doctype"):
        return "html_document"
    return ""


def _from_content_type(ct: str) -> str:
    if not ct:
        return ""
    for prefix, kind in _TYPE_PREFIX:
        if ct.startswith(prefix):
            return kind
    return ""


def looks_like_html_partial(html: str) -> bool:
    """True for a chrome-less HTML snippet (include/partial), not a full page.

    A full document (doctype or ``<html>``) is never classified this way —
    path shape has to decide those. This only catches bare fragments that
    leaked into the page graph.
    """
    if not html or len(html.strip()) < 20:
        return False
    if len(html) > 4000:
        return False
    low = html[:6000].lower()
    # A real document root (doctype / html / head / body) is not a fragment,
    # even when the CMS omitted a nav chrome.
    if "<!doctype" in low or "<html" in low or "<head" in low or "<body" in low:
        return False
    if "<nav" in low or 'role="navigation"' in low:
        return False
    # An <h1> is a document title — treat as a page even without chrome
    # (a terms/legal stub is a dead-end page, not a CMS include).
    if "<h1" in low:
        return False
    return any(tag in low for tag in ("<div", "<section", "<article", "<p", "<h2", "<span"))


def classify_page_type(
    url: str,
    *,
    status_code: int = 0,
    title: str = "",
    html: str = "",
) -> str:
    """Classify an HTML document into a page type. Generic path + status."""
    if status_code in (401, 403, 404, 407, 410) or (500 <= status_code < 600):
        return "error"
    parts = urllib.parse.urlsplit(url)
    path = parts.path or "/"
    if path in ("", "/") or is_locale_home_path(path):
        return "homepage"
    blob = path
    if parts.query:
        blob = path + "?" + parts.query
    for label, pat in _PAGE_PATTERNS:
        if pat.search(blob):
            return label
    title_l = (title or "").lower()
    if title_l.startswith("404") or "not found" in title_l or "page not found" in title_l:
        return "error"
    if looks_like_html_partial(html):
        return "utility"
    return "unknown"


def classify_resource(
    url: str,
    *,
    content_type: str = "",
    body: str = "",
    status_code: int = 0,
    headers: Optional[Dict[str, str]] = None,
) -> SemanticClass:
    """Classify a URL/resource. Prefer body magic, then Content-Type, then ext."""
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    cts = [_ctype(content_type), _ctype(headers.get("content-type", ""))]
    # Prefer a non-HTML Content-Type when the two disagree: assets are often
    # served with a generic fallback type on one of the two signals.
    ct = ""
    from_ct = ""
    for candidate in cts:
        kind = _from_content_type(candidate)
        if kind and kind != "html_document":
            ct, from_ct = candidate, kind
            break
    if not from_ct:
        ct = next((c for c in cts if c), "")
        from_ct = _from_content_type(ct)
    evidence = []

    sniffed = _sniff_body(body or "")
    ext = _ext(url)
    from_ext = _EXT_RESOURCE.get(ext, "")

    # Body magic wins when it contradicts a generic "text/html" guess on an
    # SVG/XML asset (servers sometimes mislabel vector files).
    resource = ""
    if sniffed == "svg" and from_ct in ("", "html_document", "unknown", "svg"):
        resource, evidence = "svg", ("body:svg",)
    elif sniffed == "html_document":
        resource, evidence = "html_document", ("body:html",)
    elif sniffed in ("pdf", "image"):
        resource, evidence = sniffed, (f"body:{sniffed}",)
    elif from_ct:
        resource, evidence = from_ct, (f"content-type:{ct}",)
    elif from_ext:
        resource, evidence = from_ext, (f"extension:{ext}",)

    path = urllib.parse.urlsplit(url).path or ""
    if (not resource or resource == "unknown") and _API_PATH.search(path):
        resource, evidence = "api_endpoint", ("path:api-shape",)
    if resource == "api_endpoint" and from_ext == "html_document":
        resource, evidence = "html_document", ("extension:.html",)

    if not resource:
        # No extension, no type, no magic: a path like /about is an HTML page
        # until proven otherwise. A path like /v1/users with no type stays API.
        if _API_PATH.search(path):
            resource, evidence = "api_endpoint", ("path:api-shape",)
        elif ext:
            resource, evidence = "unknown", ("extension-unmapped",)
        else:
            resource, evidence = "html_document", ("path:no-extension",)

    disp = (headers.get("content-disposition") or "").lower()
    if "attachment" in disp and resource == "html_document":
        resource, evidence = "download", ("content-disposition:attachment",)

    page_type = ""
    entity_scope = "resource"
    conf = "high" if len(evidence) >= 1 and evidence[0].startswith(("body:", "content-type:", "extension:")) else "medium"
    if resource == "html_document":
        page_type = classify_page_type(url, status_code=status_code, html=body or "")
        entity_scope = "page"
        if page_type == "homepage":
            entity_scope = "site"
    return SemanticClass(
        resource_type=resource,
        page_type=page_type,
        entity_scope=entity_scope,
        confidence=conf,
        evidence=tuple(evidence),
    )


def classify_record(record: Any) -> SemanticClass:
    """Classify a PageRecord / CrawlPage / anything with url + optional body."""
    headers = getattr(record, "headers", None) or {}
    if not isinstance(headers, dict):
        headers = {}
    body = (
        getattr(record, "raw_html", "")
        or getattr(record, "rendered_html", "")
        or getattr(record, "body", "")
        or ""
    )
    ct = getattr(record, "content_type", "") or headers.get("content-type", "")
    return classify_resource(
        getattr(record, "url", "") or "",
        content_type=ct,
        body=body,
        status_code=int(getattr(record, "status_code", 0) or 0),
        headers=headers,
    )


def classify_href(href: str) -> SemanticClass:
    """Classify a link target from the URL alone (no fetch)."""
    return classify_resource(href or "")


def worth_crawling(url: str) -> bool:
    """True if this URL should consume a crawl slot.

    Classifies from the URL alone. Non-document resources (svg, image, video,
    font, js, css, pdf, archives, API payloads) stay recorded as links on the
    parent page but are not fetched — fetching them cannot change an
    HTML-page finding and wastes the page budget.

    Unknown extension-less paths are treated as HTML until proven otherwise.
    """
    if not url:
        return False
    cls = classify_href(url)
    return cls.resource_type not in NON_DOCUMENT_RESOURCES


# --------------------------------------------------------------------------
# Link role (navigation vs media/embed)
# --------------------------------------------------------------------------
def link_role(href: str, label: str = "") -> str:
    """Return navigation | media_asset | download | skip.

    A link to a video/image/font/js/css file is a media/embed asset, not a
    navigation element — even when the ``<a>`` has no text.
    """
    h = (href or "").strip()
    if not h or h == "#" or h.startswith(("mailto:", "tel:", "javascript:", "data:")):
        return "skip"
    cls = classify_href(h)
    if cls.resource_type in MEDIA_RESOURCES:
        return "media_asset"
    if cls.resource_type in ("pdf", "download", "archive"):
        return "download"
    if cls.resource_type == "api_endpoint":
        return "media_asset"
    return "navigation"


# --------------------------------------------------------------------------
# Fetch-outcome classification (broken-link gate)
# --------------------------------------------------------------------------
def classify_fetch_outcome(
    status_code: int,
    *,
    error: str = "",
    robots_blocked: bool = False,
    resource_type: str = "",
) -> str:
    if robots_blocked or error in ("robots_disallow", "robots_disallow_our_ua"):
        return "robots_restricted"
    if error and int(status_code or 0) == 0:
        return "transient"
    code = int(status_code or 0)
    if code == 403:
        return "blocked_403"
    if code in (401, 407):
        return "authentication_required"
    if code == 404:
        return "confirmed_404"
    if code == 410:
        return "gone_410"
    if 500 <= code < 600:
        return "server_5xx"
    if code == 429:
        return "transient"
    if 400 <= code < 500:
        return "other_client_4xx"
    if resource_type in NON_DOCUMENT_RESOURCES and 200 <= code < 300:
        return "ok"
    return "ok"


def is_confirmed_broken(outcome: str) -> bool:
    return outcome in BROKEN_OUTCOMES


def blocked_access_sentence(url: str, status_code: int, outcome: str) -> str:
    """Wording for access that is *not* proven broken."""
    if outcome == "blocked_403":
        return (
            f"Access to this linked resource returned 403; accessibility to "
            f"external crawlers could not be confirmed ({url})."
        )
    if outcome == "authentication_required":
        return (
            f"Access to this linked resource requires authentication "
            f"(HTTP {status_code}); it is not a confirmed broken link ({url})."
        )
    if outcome == "robots_restricted":
        return (
            f"This linked resource is disallowed by robots.txt, so it was not "
            f"fetched; that is not evidence the URL is broken ({url})."
        )
    if outcome == "transient":
        return (
            f"Fetching this linked resource failed transiently; a broken link "
            f"was not confirmed ({url})."
        )
    return (
        f"Access to this linked resource returned HTTP {status_code}; "
        f"a broken link was not confirmed ({url})."
    )


# --------------------------------------------------------------------------
# Canonical relationship (no automatic cross-subdomain defect)
# --------------------------------------------------------------------------
def _path_tokens(url: str) -> Set[str]:
    path = urllib.parse.urlsplit(url).path or ""
    skip = {"index.html", "index.htm", "index", "www"}
    return {t.lower() for t in path.split("/") if t and t.lower() not in skip}


def paths_related(a: str, b: str) -> bool:
    ta, tb = _path_tokens(a), _path_tokens(b)
    if not ta and not tb:
        return True
    if ta & tb:
        return True
    pa = urllib.parse.urlsplit(a).path.rstrip("/")
    pb = urllib.parse.urlsplit(b).path.rstrip("/")
    if pa and pb and (pa.startswith(pb) or pb.startswith(pa)):
        return True
    return False


def evaluate_canonical(
    source_url: str,
    canonical_url: str,
    *,
    site_host: str = "",
    source_cls: Optional[SemanticClass] = None,
    target_cls: Optional[SemanticClass] = None,
    target_status: int = 0,
    target_fetched: bool = False,
) -> Dict[str, Any]:
    """Interpret a canonical without assuming a different host is a defect.

    Returns relation, verdict (ok|review|conflict), relevance, reason.
    """
    from .net import hostname_of, same_registrable_site

    src_host = hostname_of(source_url)
    can_host = hostname_of(canonical_url)
    same_host = src_host == can_host
    same_reg = same_registrable_site(canonical_url, src_host)

    if target_fetched and target_status in (404, 410):
        return {
            "relation": "same_host" if same_host else ("cross_subdomain" if same_reg else "cross_origin"),
            "verdict": "conflict",
            "relevance": "high",
            "reason": "canonical target returns 404/410",
            "evidence_tier": "confirmed",
        }
    if target_fetched and 500 <= target_status < 600:
        return {
            "relation": "same_host" if same_host else ("cross_subdomain" if same_reg else "cross_origin"),
            "verdict": "conflict",
            "relevance": "high",
            "reason": "canonical target returns 5xx",
            "evidence_tier": "confirmed",
        }

    if same_host:
        return {
            "relation": "same_host",
            "verdict": "ok",
            "relevance": "high",
            "reason": "canonical host matches the source page",
            "evidence_tier": "informational",
        }

    src_cls = source_cls or classify_resource(source_url)
    tgt_cls = target_cls or (classify_resource(canonical_url) if canonical_url else None)
    related = paths_related(source_url, canonical_url)
    types = frozenset(t for t in (
        src_cls.page_type if src_cls else "",
        tgt_cls.page_type if tgt_cls else "",
    ) if t)
    type_conflict = types in _INCOMPATIBLE_PAGE_TYPES

    if same_reg:
        # Sibling subdomain of the same registrable domain — often intentional
        # (www ↔ shop, www ↔ blog, regional hosts). Review, don't auto-fail
        # unless the target is clearly a different entity/content.
        if type_conflict and not related:
            return {
                "relation": "cross_subdomain",
                "verdict": "conflict",
                "relevance": "medium",
                "reason": "cross-subdomain canonical joins incompatible page types with unrelated paths",
                "evidence_tier": "likely",
            }
        return {
            "relation": "cross_subdomain",
            "verdict": "review",
            "relevance": "low",
            "reason": (
                "canonical points to a sibling host on the same registrable "
                "domain; this is often intentional syndication or locale "
                "canonicalization and needs review, not an automatic defect"
            ),
            "evidence_tier": "review",
        }

    # Different registrable domain. Usually a real inconsistency (wrong
    # origin, leftover preview host) unless we cannot judge at all.
    return {
        "relation": "cross_origin",
        "verdict": "conflict",
        "relevance": "medium",
        "reason": "canonical points at a different registrable domain",
        "evidence_tier": "likely",
    }


# --------------------------------------------------------------------------
# Gate: is this resource in scope for this check?
# --------------------------------------------------------------------------
def in_scope(check_id: str, cls: SemanticClass) -> bool:
    """True if this resource/page is in the semantic scope of ``check_id``."""
    spec = CHECK_SCOPES.get(check_id)
    if spec is None:
        return True
    if cls.resource_type not in spec.resource_types:
        return False
    if spec.page_types and cls.page_type and cls.page_type not in spec.page_types:
        return False
    return True


def semantic_relevance(check_id: str, cls: SemanticClass) -> str:
    """high | medium | low | none — drives severity capping."""
    spec = CHECK_SCOPES.get(check_id)
    if spec is None:
        return "high"
    if cls.resource_type in NON_DOCUMENT_RESOURCES and spec.entity_scope == "page":
        return "none"
    if not in_scope(check_id, cls):
        return "none" if spec.required else "low"
    if cls.page_type in ("utility", "error", "account") and check_id.startswith("engagement."):
        return "low"
    return "high"


def apply_severity_gate(
    severity: Severity,
    confidence: Confidence,
    relevance: str = "high",
) -> Tuple[Severity, Confidence]:
    """Cap severity so low-relevance observations cannot read as high/critical.

    Confidence is also capped: low relevance → at most medium confidence.
    """
    cap = {
        Confidence.HIGH: Severity.CRITICAL,
        Confidence.MEDIUM: Severity.HIGH,
        Confidence.LOW: Severity.MEDIUM,
    }[confidence]
    if severity.rank > cap.rank:
        severity = cap
    rel = (relevance or "high").lower()
    if rel == "none":
        return Severity.MEDIUM, Confidence.LOW
    if rel == "low":
        if confidence.rank > Confidence.MEDIUM.rank:
            confidence = Confidence.MEDIUM
        if severity.rank > Severity.MEDIUM.rank:
            severity = Severity.MEDIUM
    elif rel == "medium" and severity.rank > Severity.HIGH.rank:
        severity = Severity.HIGH
    return severity, confidence


def annotate(obs: Any, cls: SemanticClass, check_id: str = "") -> Any:
    """Stamp an Observation (or duck-type) with semantic fields."""
    cid = check_id or getattr(obs, "check_id", "")
    rel = semantic_relevance(cid, cls)
    obs.resource_type = cls.resource_type
    obs.page_type = cls.page_type
    obs.entity_scope = cls.entity_scope
    obs.semantic_relevance = rel
    if not getattr(obs, "evidence_tier", ""):
        obs.evidence_tier = {
            "high": "confirmed",
            "medium": "likely",
            "low": "review",
            "none": "informational",
        }.get(rel, "likely")
    return obs


def should_emit_finding(relevance: str) -> bool:
    return (relevance or "high") != "none"
