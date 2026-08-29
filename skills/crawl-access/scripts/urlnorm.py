"""URL normalization, deduplication, variant, and crawl-trap utilities.

These are the primitives the crawler and detectors use to avoid re-fetching the
same page, to detect duplicate URL variants, and to spot crawl traps. Standard
library only.

Two distinct notions:
  * normalize_url(url, base) -> a canonical absolute URL used for fetching and
    display (resolves relative links, lowercases host, drops fragments and
    default ports; preserves path case and query semantics).
  * dedup_key(url) -> a lossy key used ONLY to decide whether two URLs are the
    "same page" for crawl deduplication and duplicate-variant detection
    (ignores scheme, leading www, trailing slash, default index files, and
    common tracking parameters).
"""

from __future__ import annotations

import posixpath
import re
import urllib.parse
from typing import List, Set

_DEFAULT_PORTS = {"http": "80", "https": "443"}
_INDEX_FILES = ("index.html", "index.htm", "index.php", "default.aspx", "index.asp")

# Query parameters that never change page content (analytics / ad click ids).
_TRACKING_PARAMS = re.compile(
    r"^(utm_[a-z]+|gclid|fbclid|mc_[a-z]+|_ga|ref|ref_src|igshid|yclid|msclkid|"
    r"cmpid|campaign|source|medium)$",
    re.I,
)

# Parameters that strongly suggest a per-session / infinite URL space.
_SESSION_PARAMS = re.compile(
    r"^(sid|sessionid|session_id|phpsessid|jsessionid|aspsessionid|sess|s|"
    r"token|nonce)$",
    re.I,
)


def is_http_url(url: str) -> bool:
    try:
        p = urllib.parse.urlsplit(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def _clean_netloc(scheme: str, netloc: str) -> str:
    # Strip credentials (we never crawl authenticated URLs) and default ports.
    netloc = netloc.split("@")[-1]
    host, _, port = netloc.partition(":")
    host = host.lower().rstrip(".")
    if port and _DEFAULT_PORTS.get(scheme) == port:
        port = ""
    return host + (f":{port}" if port else "")


def _normalize_path(path: str) -> str:
    if not path:
        return "/"
    # Collapse ../ and //; keep it absolute.
    path = re.sub(r"/{2,}", "/", path)
    normalized = posixpath.normpath(path)
    if normalized == ".":
        normalized = "/"
    # posixpath.normpath drops a trailing slash; restore it when the input had one.
    if path.endswith("/") and not normalized.endswith("/"):
        normalized += "/"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def normalize_url(url: str, base: str = "") -> str:
    """Resolve and canonicalize a URL for fetching/display.

    Non-lossy w.r.t. content: preserves path case and query, drops only the
    fragment, default ports, and empty query/params.
    """
    url = (url or "").strip()
    if base:
        url = urllib.parse.urljoin(base, url)
    parts = urllib.parse.urlsplit(url)
    scheme = parts.scheme.lower()
    netloc = _clean_netloc(scheme, parts.netloc)
    path = _normalize_path(parts.path)
    # Drop empty query string and fragment.
    query = parts.query
    return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))


def strip_tracking_query(query: str) -> str:
    if not query:
        return ""
    kept = []
    for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True):
        if _TRACKING_PARAMS.match(k):
            continue
        kept.append((k, v))
    kept.sort()
    return urllib.parse.urlencode(kept)


def dedup_key(url: str) -> str:
    """Lossy key: two URLs with the same key are treated as the same page.

    Ignores scheme, leading www, trailing slash, default index files, and
    tracking parameters. Used for crawl dedup and duplicate-variant detection.
    """
    parts = urllib.parse.urlsplit(normalize_url(url))
    host = parts.netloc
    if host.startswith("www."):
        host = host[4:]
    path = parts.path or "/"
    # Drop a trailing default index file.
    for idx in _INDEX_FILES:
        if path.endswith("/" + idx):
            path = path[: -len(idx)]
            break
        if path == "/" + idx:
            path = "/"
            break
    # Normalize trailing slash (except root).
    if len(path) > 1:
        path = path.rstrip("/")
        if path == "":
            path = "/"
    query = strip_tracking_query(parts.query)
    key = host + path
    if query:
        key += "?" + query
    return key


def same_site(url: str, site_host: str) -> bool:
    """True if url belongs to the same site (host or its www/apex variant)."""
    host = urllib.parse.urlsplit(normalize_url(url)).netloc
    host = host.split(":")[0]
    site = site_host.split(":")[0].lower()
    h = host.lower()
    h_apex = h[4:] if h.startswith("www.") else h
    s_apex = site[4:] if site.startswith("www.") else site
    return h_apex == s_apex


def variant_kind(url: str) -> List[str]:
    """Return the normalization variants a URL exhibits (for reporting)."""
    parts = urllib.parse.urlsplit(url)
    kinds = []
    host = parts.netloc.lower()
    if host.startswith("www."):
        kinds.append("www")
    if parts.scheme == "http":
        kinds.append("http")
    path = parts.path
    if path.endswith("/") and path != "/":
        kinds.append("trailing_slash")
    for idx in _INDEX_FILES:
        if path.endswith(idx):
            kinds.append("index_file")
            break
    if parts.netloc != parts.netloc.lower():
        kinds.append("uppercase_host")
    if parts.query and strip_tracking_query(parts.query) != parts.query:
        kinds.append("tracking_params")
    return kinds


def has_session_param(url: str) -> bool:
    q = urllib.parse.urlsplit(url).query
    for k, _ in urllib.parse.parse_qsl(q, keep_blank_values=True):
        if _SESSION_PARAMS.match(k):
            return True
    return False


def param_count(url: str) -> int:
    q = urllib.parse.urlsplit(url).query
    return len(urllib.parse.parse_qsl(q, keep_blank_values=True))


def repeating_segments(url: str, threshold: int = 3) -> bool:
    """Detect an identical path segment repeated >= threshold times (a trap)."""
    path = urllib.parse.urlsplit(url).path
    segs = [s for s in path.split("/") if s]
    if not segs:
        return False
    counts = {}
    for s in segs:
        counts[s] = counts.get(s, 0) + 1
        if counts[s] >= threshold:
            return True
    # Also detect a repeated adjacent pair pattern like /a/b/a/b/a/b
    if len(segs) >= 6:
        for size in (2, 3):
            window = segs[:size]
            reps = 1
            i = size
            while i + size <= len(segs) and segs[i : i + size] == window:
                reps += 1
                i += size
            if reps >= threshold:
                return True
    return False


def path_depth(url: str) -> int:
    path = urllib.parse.urlsplit(url).path
    return len([s for s in path.split("/") if s])


def all_variants(url: str) -> Set[str]:
    """The set of common variant URLs pointing at the same page (for tests)."""
    parts = urllib.parse.urlsplit(normalize_url(url))
    host = parts.netloc
    apex = host[4:] if host.startswith("www.") else host
    www = host if host.startswith("www.") else "www." + host
    path = parts.path or "/"
    base_paths = {path}
    if path != "/" and not path.endswith("/"):
        base_paths.add(path + "/")
    variants = set()
    for scheme in ("http", "https"):
        for h in (apex, www):
            for p in base_paths:
                variants.add(urllib.parse.urlunsplit((scheme, h, p, parts.query, "")))
    return variants
