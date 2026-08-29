"""Network safety helpers: public-HTTP only, same-site, XML sniffing.

Used by the live fetcher and crawler so sitemap URLs cannot SSRF into
link-local/metadata ranges and so HTML soft-404s are not treated as sitemaps.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.parse
from typing import Optional

_PRIVATE_HOSTS = {"localhost", "localhost.localdomain"}
_MULTI_TLD = {
    "co.uk", "org.uk", "ac.uk", "gov.uk",
    "com.au", "net.au", "org.au",
    "co.jp", "ne.jp", "or.jp", "ac.jp",
    "co.in", "com.br", "com.mx", "co.nz", "co.za",
}

_PAGINATION_PARAMS = re.compile(
    r"^(page|pagenum|pageno|pg|offset|start|from|cursor|after|before)$",
    re.I,
)


def hostname_of(url: str) -> str:
    host = urllib.parse.urlsplit(url).netloc.split("@")[-1]
    host = host.split(":")[0].lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host


def registrable_domain(host: str) -> str:
    host = (host or "").lower().split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    parts = [p for p in host.split(".") if p]
    if len(parts) < 2:
        return host
    last2 = ".".join(parts[-2:])
    if last2 in _MULTI_TLD and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last2


def same_registrable_site(url: str, site_host: str) -> bool:
    return registrable_domain(hostname_of(url)) == registrable_domain(site_host)


def _is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return bool(
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def is_blocked_host(host: str) -> bool:
    host = (host or "").lower().split(":")[0].rstrip(".")
    if host in _PRIVATE_HOSTS or host.endswith(".localhost"):
        return True
    if _is_private_ip(host):
        return True
    return False


def resolve_is_private(host: str, timeout_s: float = 1.0) -> bool:
    """True if DNS resolves to a non-public address. Fail closed on DNS errors."""
    if is_blocked_host(host):
        return True
    try:
        old = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout_s)
        try:
            infos = socket.getaddrinfo(host, None)
        finally:
            socket.setdefaulttimeout(old)
    except Exception:
        return True
    for info in infos:
        ip = info[4][0]
        if _is_private_ip(ip):
            return True
    return False


def is_public_http_url(url: str, resolve: bool = False) -> bool:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    host = hostname_of(url)
    if not host or is_blocked_host(host):
        return False
    if resolve and resolve_is_private(host):
        return False
    return True


def looks_like_xml(body: str, content_type: str = "") -> bool:
    ct = (content_type or "").lower()
    if "html" in ct and "xml" not in ct:
        return False
    if "xml" in ct:
        return True
    s = (body or "").lstrip("\ufeff \t\r\n")
    if not s:
        return False
    head = s[:200].lower()
    return (
        s.startswith("<?xml")
        or "<urlset" in head
        or "<sitemapindex" in head
        or head.startswith("<rss")
        or head.startswith("<feed")
    )


def is_pagination_param(name: str) -> bool:
    return bool(_PAGINATION_PARAMS.match(name or ""))


def trap_query(url: str) -> Optional[str]:
    """Return a trap reason if the URL should not be enqueued, else None."""
    q = urllib.parse.urlsplit(url).query
    pairs = urllib.parse.parse_qsl(q, keep_blank_values=True)
    if len(pairs) >= 6:
        return "too_many_params"
    for k, _ in pairs:
        if is_pagination_param(k):
            return "pagination"
    return None
