"""Tiered detectors over a CrawlResult.

Each detector is a pure function: (CrawlResult, ctx) -> list[CrawlFinding].
Findings carry a tier so we never emit a "confirmed defect" for the mere
absence of a best practice:

    confirmed     -> provably broken (becomes a high-confidence finding)
    likely        -> strong signal, some benign explanations exist (capped finding)
    informational -> noteworthy but not a defect (recorded, never a finding)

The module also converts findings into normalized Observations for the
orchestrator. Standard library only.
"""

from __future__ import annotations

import os
import sys
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_LIB = os.path.join(_ROOT, "lib")
for _p in (_HERE, _LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import urlnorm  # noqa: E402
from crawl_result import CrawlResult  # noqa: E402
from audit_common.schema import (  # noqa: E402
    Concern,
    Confidence,
    Method,
    Observation,
    Status,
)
from audit_common.semantics import (  # noqa: E402
    blocked_access_sentence,
    classify_fetch_outcome,
    classify_record,
    classify_resource,
    evaluate_canonical,
    in_scope,
    is_confirmed_broken,
)


class Tier(str, Enum):
    CONFIRMED = "confirmed"
    LIKELY = "likely"
    INFORMATIONAL = "informational"
    INCONCLUSIVE = "inconclusive"


@dataclass
class CrawlFinding:
    check_id: str
    tier: Tier
    title: str
    evidence: str
    metric: Dict = field(default_factory=dict)
    urls: List[str] = field(default_factory=list)
    method: Method = Method.HTTP
    resource_type: str = "html_document"
    page_type: str = ""
    entity_scope: str = "page"
    semantic_relevance: str = "high"
    evidence_tier: str = ""

    def to_observation(self) -> Observation:
        if self.tier == Tier.CONFIRMED:
            status, conf = Status.FAIL, Confidence.HIGH
        elif self.tier == Tier.LIKELY:
            status, conf = Status.FAIL, Confidence.MEDIUM
        elif self.tier == Tier.INCONCLUSIVE:
            status, conf = Status.INCONCLUSIVE, Confidence.LOW
        else:
            status, conf = Status.PASS, Confidence.LOW
        etier = self.evidence_tier or self.tier.value
        return Observation(
            check_id=self.check_id,
            concern=Concern.CRAWL_ACCESS,
            method=self.method,
            status=status,
            confidence=conf,
            metric={**self.metric, "tier": self.tier.value},
            urls=self.urls[:20],
            sample_n=len(self.urls),
            evidence_sentence=self.evidence,
            resource_type=self.resource_type,
            page_type=self.page_type,
            entity_scope=self.entity_scope,
            semantic_relevance=self.semantic_relevance,
            evidence_tier=etier,
        )


# Important-page URL patterns (used conservatively, not to demand their presence).
# Deliberately excludes high-cardinality catalog/feed roots such as /product,
# /products and /blog: on a real site those hold thousands of leaf pages, so a
# single noindex or orphan among them is routine, not an "important page" defect.
_IMPORTANT_PATTERNS = ("/about", "/contact", "/pricing", "/services",
                       "/faq", "/support", "/help")

_LONG_REDIRECT_THRESHOLD = 4
_DEEP_THRESHOLD = 4
_MANY_PARAMS = 4


def _robots_quote(page) -> str:
    meta = (getattr(page, "meta_robots", "") or "").strip()
    hdr = (getattr(page, "x_robots_tag", "") or "").strip()
    if meta:
        return f'<meta name="robots" content="{meta}">'
    if hdr:
        return f"X-Robots-Tag: {hdr}"
    return "a noindex directive"


def _is_important(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path.lower().rstrip("/") or "/"
    # Match the page itself (/about), a sub-path (/about/team) or a hyphenated
    # variant (/about-us) — but never a bare prefix collision (/aboutthe-... or
    # /supported-devices), which the old `startswith(p)` test wrongly accepted.
    return any(
        path == p or path.startswith(p + "/") or path.startswith(p + "-")
        for p in _IMPORTANT_PATTERNS
    )


_AI_PROBE_PATHS = (
    "/", "/about", "/about/", "/products", "/products/", "/pricing", "/pricing/",
    "/blog", "/blog/", "/docs", "/docs/",
)


def compute_ai_blocked(robots_raw: str, ai_tokens: List[str],
                       paths: Optional[List[str]] = None) -> List[str]:
    """AI user-agents disallowed on important paths while Googlebot is allowed."""
    if not robots_raw.strip():
        return []
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(robots_raw.splitlines())
    probes = list(paths or _AI_PROBE_PATHS)
    try:
        baseline_blocked = all(not rp.can_fetch("Googlebot", p) for p in probes)
    except Exception:
        baseline_blocked = False
    if baseline_blocked:
        return []  # global disallow — reported elsewhere
    blocked = []
    for token in ai_tokens:
        try:
            if any(not rp.can_fetch(token, p) for p in probes):
                blocked.append(token)
        except Exception:
            continue
    return blocked


# --------------------------------------------------------------------------
# 1. robots.txt availability
# --------------------------------------------------------------------------
def detect_robots_availability(result: CrawlResult, ctx=None) -> List[CrawlFinding]:
    r = result.robots
    if not r.reachable:
        return [CrawlFinding(
            check_id="access.robots_unreachable",
            tier=Tier.LIKELY,
            title="robots.txt is unreachable (server error)",
            evidence=f"robots.txt at {r.url} returned status {r.status_code}; some crawlers treat an errored robots.txt as disallow-all.",
            metric={"status": r.status_code},
            urls=[r.url],
            method=Method.ROBOTS,
        )]
    if not r.available:
        return [CrawlFinding(
            check_id="access.robots_missing",
            tier=Tier.INFORMATIONAL,
            title="No robots.txt present",
            evidence=f"No robots.txt was served at {r.url} (status {r.status_code}); this is permitted and means crawlers may access everything.",
            metric={"status": r.status_code},
            urls=[r.url],
            method=Method.ROBOTS,
        )]
    return []


# --------------------------------------------------------------------------
# 2. robots.txt directives
# --------------------------------------------------------------------------
def detect_robots_directives(result: CrawlResult, ctx=None) -> List[CrawlFinding]:
    findings: List[CrawlFinding] = []
    r = result.robots
    if not r.available:
        return findings
    if "*" in r.disallow_all_agents:
        findings.append(CrawlFinding(
            check_id="access.global_disallow_all",
            tier=Tier.CONFIRMED,
            title="robots.txt blocks all crawlers (Disallow: / for *)",
            evidence="robots.txt contains `User-agent: *` with `Disallow: /` — every compliant crawler, including AI assistants, is blocked from the entire site.",
            metric={"disallow_all_agents": r.disallow_all_agents},
            urls=[r.url],
            method=Method.ROBOTS,
        ))
        return findings  # subsumes AI-specific blocking

    ai_tokens = []
    if ctx is not None and getattr(ctx, "ai_agents", None):
        ai_tokens = [a["token"] for a in ctx.ai_agents.get("agents", [])]
    blocked = compute_ai_blocked(r.raw, ai_tokens)
    if blocked:
        findings.append(CrawlFinding(
            check_id="access.ai_ua_blocked",
            tier=Tier.CONFIRMED,
            title="AI assistant crawlers are blocked in robots.txt",
            evidence=f"robots.txt disallows {len(blocked)} AI fetcher(s) on content paths while Googlebot is allowed: {', '.join(blocked)}.",
            metric={"blocked_agents": blocked},
            urls=[r.url],
            method=Method.ROBOTS,
        ))
    elif r.our_ua_blocked_root:
        findings.append(CrawlFinding(
            check_id="access.our_ua_blocked",
            tier=Tier.INFORMATIONAL,
            title="This audit's user-agent is disallowed at root",
            evidence="robots.txt disallows this tool's user-agent; results reflect only what was permitted. Other crawlers may have different access.",
            urls=[r.url],
            method=Method.ROBOTS,
        ))
    return findings


# --------------------------------------------------------------------------
# 3. sitemap discovery
# --------------------------------------------------------------------------
def detect_sitemap_discovery(result: CrawlResult, ctx=None) -> List[CrawlFinding]:
    available = [s for s in result.sitemaps if s.available]
    if available:
        return []
    # A robots-disallowed sitemap exists but we were not permitted to fetch it —
    # that is not a broken sitemap, so it is never a defect.
    disallowed = [s for s in result.sitemaps if getattr(s, "kind", "") == "robots_disallowed"]
    if disallowed and not [s for s in result.sitemaps if getattr(s, "kind", "") != "robots_disallowed"]:
        return [CrawlFinding(
            check_id="access.sitemap_missing",
            tier=Tier.INFORMATIONAL,
            title="Sitemap present but disallowed to this crawler",
            evidence=(f"robots.txt references {len(disallowed)} sitemap(s) under a path this crawler is "
                      f"disallowed from fetching (e.g. {disallowed[0].url}); the sitemap likely exists."),
            metric={"disallowed": [s.url for s in disallowed]},
            urls=[s.url for s in disallowed],
            method=Method.SITEMAP,
        )]
    # A sitemap explicitly referenced in robots but not served is a broken pointer.
    referenced = set(result.robots.sitemaps)
    # Only a real HTTP error is a broken pointer. status 0 means we never
    # received a response (budget, fixture gap, network) — not a 404.
    broken_ref = [
        s for s in result.sitemaps
        if s.url in referenced and not s.available
        and getattr(s, "kind", "") != "robots_disallowed"
        and int(getattr(s, "status_code", 0) or 0) >= 400
    ]
    if broken_ref:
        return [CrawlFinding(
            check_id="access.sitemap_missing",
            tier=Tier.LIKELY,
            title="robots.txt references a sitemap that is not served",
            evidence=f"robots.txt lists {len(broken_ref)} sitemap(s) that returned non-2xx: {', '.join(s.url for s in broken_ref)}.",
            metric={"status": [s.status_code for s in broken_ref]},
            urls=[s.url for s in broken_ref],
            method=Method.SITEMAP,
        )]
    return [CrawlFinding(
        check_id="access.sitemap_missing",
        tier=Tier.INFORMATIONAL,
        title="No XML sitemap found",
        evidence="No sitemap was found in robots.txt or at /sitemap.xml. A sitemap helps crawlers discover deep pages, but its absence is not itself a defect.",
        method=Method.SITEMAP,
    )]


# --------------------------------------------------------------------------
# 4. sitemap validity
# --------------------------------------------------------------------------
def detect_sitemap_validity(result: CrawlResult, ctx=None) -> List[CrawlFinding]:
    findings: List[CrawlFinding] = []
    referenced = set(result.robots.sitemaps)
    for s in result.sitemaps:
        # HTML at a guessed /sitemap.xml is not a defect. Only flag documents
        # that were advertised as sitemaps (or actually parsed as XML) and broke.
        if getattr(s, "kind", "") == "not_xml":
            continue
        if s.available and not s.well_formed:
            findings.append(CrawlFinding(
                check_id="access.sitemap_invalid",
                tier=Tier.CONFIRMED,
                title="Sitemap is present but not valid XML",
                evidence=f"Sitemap {s.url} was served (status {s.status_code}) but failed XML parsing: {', '.join(s.errors) or 'malformed'}.",
                metric={"errors": s.errors, "kind": s.kind},
                urls=[s.url],
                method=Method.SITEMAP,
            ))
    return findings


# --------------------------------------------------------------------------
# 5 + 13. canonical URLs and conflicts
# --------------------------------------------------------------------------
def detect_canonical(result: CrawlResult, ctx=None) -> List[CrawlFinding]:
    findings: List[CrawlFinding] = []
    cross_origin: List[str] = []
    cross_sub_review: List[str] = []
    cross_sub_conflict: List[str] = []
    to_error: List[str] = []
    chained: List[str] = []
    for page in result.html_pages():
        if not page.canonical:
            continue
        src_cls = classify_record(page)
        if not in_scope("access.canonical_inconsistent", src_cls):
            continue
        canon = page.canonical
        target = result.get(urlnorm.normalize_url(canon))
        tgt_cls = classify_record(target) if target is not None else classify_resource(canon)
        ev = evaluate_canonical(
            page.url, canon,
            site_host=result.site_host,
            source_cls=src_cls,
            target_cls=tgt_cls,
            target_status=int(getattr(target, "status_code", 0) or 0) if target else 0,
            target_fetched=bool(getattr(target, "fetched", False)) if target else False,
        )
        # Error-target is a confirmed conflict regardless of host relation.
        if ev["verdict"] == "conflict" and ev.get("reason", "").startswith("canonical target returns"):
            to_error.append(page.url)
            continue
        if ev["relation"] == "same_host":
            if target and target.canonical and urlnorm.dedup_key(target.canonical) != urlnorm.dedup_key(canon):
                chained.append(page.url)
            continue
        if ev["relation"] == "cross_subdomain":
            if ev["verdict"] == "conflict":
                cross_sub_conflict.append(page.url)
            else:
                cross_sub_review.append(page.url)
            continue
        # cross-origin (different registrable domain)
        if ev["verdict"] == "conflict":
            cross_origin.append(page.url)
    if to_error:
        findings.append(CrawlFinding(
            check_id="access.canonical_to_error",
            tier=Tier.CONFIRMED,
            title="Canonical URL points to an error page",
            evidence=f"{len(to_error)} page(s) declare a canonical URL that returns a 4xx/5xx status — search/AI systems may drop the page entirely.",
            metric={"count": len(to_error)},
            urls=to_error,
            method=Method.HTTP,
            evidence_tier="confirmed",
            semantic_relevance="high",
        ))
    if chained:
        findings.append(CrawlFinding(
            check_id="access.canonical_inconsistent",
            tier=Tier.LIKELY,
            title="Canonical chain (canonical points to a differently-canonicalized page)",
            evidence=f"{len(chained)} page(s) canonicalize to a URL that itself canonicalizes elsewhere, creating an ambiguous canonical chain.",
            metric={"count": len(chained)},
            urls=chained,
            method=Method.HTTP,
            evidence_tier="likely",
            semantic_relevance="high",
        ))
    if cross_origin:
        findings.append(CrawlFinding(
            check_id="access.canonical_inconsistent",
            tier=Tier.LIKELY,
            title="Canonical URL points to a different registrable domain",
            evidence=(
                f"{len(cross_origin)} page(s) declare a canonical on a different "
                f"registrable domain; source and target do not share a site identity, "
                f"so indexers may drop this origin."
            ),
            metric={"count": len(cross_origin), "relation": "cross_origin"},
            urls=cross_origin,
            method=Method.HTTP,
            evidence_tier="likely",
            semantic_relevance="medium",
        ))
    if cross_sub_conflict:
        findings.append(CrawlFinding(
            check_id="access.canonical_inconsistent",
            tier=Tier.LIKELY,
            title="Cross-subdomain canonical joins unrelated content",
            evidence=(
                f"{len(cross_sub_conflict)} page(s) canonicalize to a sibling "
                f"subdomain whose page type/path is unrelated to the source — "
                f"review whether they represent the same entity."
            ),
            metric={"count": len(cross_sub_conflict), "relation": "cross_subdomain"},
            urls=cross_sub_conflict,
            method=Method.HTTP,
            evidence_tier="likely",
            semantic_relevance="medium",
        ))
    if cross_sub_review:
        findings.append(CrawlFinding(
            check_id="access.canonical_inconsistent",
            tier=Tier.LIKELY,
            title="Cross-subdomain canonical needs review",
            evidence=(
                f"{len(cross_sub_review)} page(s) canonicalize to a sibling host "
                f"on the same registrable domain. This is often intentional "
                f"(locale, shop, or blog host) and is not automatically a defect; "
                f"confirm that source and target represent the same entity/content."
            ),
            metric={"count": len(cross_sub_review), "relation": "cross_subdomain",
                    "verdict": "review"},
            urls=cross_sub_review,
            method=Method.HTTP,
            evidence_tier="review",
            semantic_relevance="low",
            entity_scope="site",
        ))
    return findings


# --------------------------------------------------------------------------
# 6. HTTP status codes (homepage + server errors)
# --------------------------------------------------------------------------
def detect_http_status(result: CrawlResult, ctx=None) -> List[CrawlFinding]:
    findings: List[CrawlFinding] = []
    home = result.get(result.entry_url)
    if home is not None and home.fetched and home.status_code >= 400:
        findings.append(CrawlFinding(
            check_id="access.homepage_unreachable",
            tier=Tier.CONFIRMED,
            title="Homepage returns an error status",
            evidence=f"The homepage {home.url} returned HTTP {home.status_code}; if the entry point fails, the whole site is effectively invisible.",
            metric={"status": home.status_code},
            urls=[home.url],
        ))
    server_errors = [p.url for p in result.fetched_pages() if 500 <= p.status_code < 600]
    if server_errors:
        findings.append(CrawlFinding(
            check_id="access.server_errors",
            tier=Tier.CONFIRMED,
            title="Pages return server errors (5xx)",
            evidence=f"{len(server_errors)} crawled page(s) returned a 5xx server error.",
            metric={"count": len(server_errors)},
            urls=server_errors,
        ))
    return findings


# --------------------------------------------------------------------------
# 7. redirect chains
# --------------------------------------------------------------------------
def detect_redirects(result: CrawlResult, ctx=None) -> List[CrawlFinding]:
    findings: List[CrawlFinding] = []
    loops: List[str] = []
    longs: List[str] = []
    for page in result.fetched_pages():
        chain = page.redirect_chain
        if not chain:
            if page.error == "too_many_redirects":
                loops.append(page.url)
            continue
        seen_urls = {chain[0].from_url}
        looped = False
        for hop in chain:
            # A cycle shows up when a redirect target revisits a URL already seen.
            if hop.to_url in seen_urls:
                looped = True
                break
            seen_urls.add(hop.to_url)
        if looped or page.error == "too_many_redirects":
            loops.append(page.url)
        elif len(chain) >= _LONG_REDIRECT_THRESHOLD:
            longs.append(page.url)
    if loops:
        findings.append(CrawlFinding(
            check_id="access.redirect_loop",
            tier=Tier.CONFIRMED,
            title="Redirect loop detected",
            evidence=f"{len(loops)} URL(s) enter a redirect loop or exceed the redirect limit, so the content is never reached.",
            metric={"count": len(loops)},
            urls=loops,
        ))
    if longs:
        findings.append(CrawlFinding(
            check_id="access.redirect_chain_long",
            tier=Tier.LIKELY,
            title="Long redirect chains",
            evidence=f"{len(longs)} URL(s) redirect {_LONG_REDIRECT_THRESHOLD}+ times before resolving, wasting crawl budget and risking drop-off.",
            metric={"threshold": _LONG_REDIRECT_THRESHOLD, "count": len(longs)},
            urls=longs,
        ))
    return findings


# --------------------------------------------------------------------------
# 8 + 14. broken internal links
# --------------------------------------------------------------------------
def detect_broken_internal_links(result: CrawlResult, ctx=None) -> List[CrawlFinding]:
    broken: Dict[str, Dict] = {}
    blocked: Dict[str, Dict] = {}
    for page in result.html_pages():
        src_cls = classify_record(page)
        if not in_scope("access.broken_internal_links", src_cls):
            continue
        for link in page.internal_links:
            target = result.get(urlnorm.normalize_url(link))
            if target is None:
                continue
            tgt_cls = classify_record(target)
            robots_blocked = (getattr(target, "error", "") or "") in (
                "robots_disallow", "robots_disallow_our_ua",
            )
            outcome = classify_fetch_outcome(
                int(getattr(target, "status_code", 0) or 0),
                error=getattr(target, "error", "") or "",
                robots_blocked=robots_blocked,
                resource_type=tgt_cls.resource_type,
            )
            if not target.fetched and not robots_blocked:
                continue
            rec = {
                "status": int(getattr(target, "status_code", 0) or 0),
                "outcome": outcome,
                "resource_type": tgt_cls.resource_type,
                "sources": [],
            }
            if is_confirmed_broken(outcome):
                slot = broken.setdefault(target.url, rec)
                if page.url not in slot["sources"]:
                    slot["sources"].append(page.url)
            elif outcome in (
                "blocked_403", "authentication_required",
                "robots_restricted", "transient",
            ):
                slot = blocked.setdefault(target.url, rec)
                if page.url not in slot["sources"]:
                    slot["sources"].append(page.url)
    out: List[CrawlFinding] = []
    if broken:
        html_broken = {
            u: d for u, d in broken.items()
            if (d.get("resource_type") or "html_document") == "html_document"
        }
        # A 404 image/video is a missing file, not a broken discoverable page.
        # Old failure: Adobe-class sites led with .mp4 404s as high findings.
        if html_broken:
            sample_url = sorted(html_broken)[0]
            sample = html_broken[sample_url]
            srcs = sample.get("sources") or []
            src_bit = f"{srcs[0]} → " if srcs else ""
            out.append(CrawlFinding(
                check_id="access.broken_internal_links",
                tier=Tier.CONFIRMED,
                title="Broken internal links (404/410/5xx targets)",
                evidence=(
                    f"{len(html_broken)} HTML page(s) return 404, 410, or 5xx "
                    f"(e.g. {src_bit}{sample_url} → HTTP {sample['status']})."
                ),
                metric={
                    "targets": {u: d["status"] for u, d in html_broken.items()},
                    "outcomes": {u: d["outcome"] for u, d in html_broken.items()},
                    "html_targets": len(html_broken),
                },
                urls=list(html_broken.keys()),
                evidence_tier="confirmed",
                semantic_relevance="high",
            ))
    if blocked:
        sample_url = sorted(blocked.keys())[0]
        sample = blocked[sample_url]
        out.append(CrawlFinding(
            check_id="access.broken_internal_links",
            tier=Tier.INCONCLUSIVE,
            title="Linked resource access could not be confirmed",
            evidence=blocked_access_sentence(
                sample_url, sample["status"], sample["outcome"]
            ) + (
                f" {len(blocked)} linked resource(s) returned an access/"
                f"auth/robots/transient outcome, not a confirmed broken page."
            ),
            metric={
                "targets": {u: d["status"] for u, d in blocked.items()},
                "outcomes": {u: d["outcome"] for u, d in blocked.items()},
            },
            urls=list(blocked.keys()),
            evidence_tier="review",
            semantic_relevance="low",
        ))
    return out


# --------------------------------------------------------------------------
# 9. orphan-like pages
# --------------------------------------------------------------------------
def detect_orphan_pages(result: CrawlResult, ctx=None) -> List[CrawlFinding]:
    orphans: List[str] = []
    for page in result.html_pages():
        if page.url == result.entry_url:
            continue
        if page.in_sitemap and page.depth is None and page.inbound_internal == 0 and not _is_important(page.url):
            orphans.append(page.url)
    if not orphans:
        return []
    return [CrawlFinding(
        check_id="access.orphan_pages",
        tier=Tier.LIKELY,
        title="Orphan-like pages (in sitemap, no internal links)",
        evidence=f"{len(orphans)} page(s) appear in the sitemap but have no internal links pointing to them, so crawlers relying on link-following may miss them.",
        metric={"count": len(orphans)},
        urls=orphans,
    )]


# --------------------------------------------------------------------------
# 10. depth from homepage
# --------------------------------------------------------------------------
def detect_depth(result: CrawlResult, ctx=None) -> List[CrawlFinding]:
    deep = [p.url for p in result.html_pages() if p.depth is not None and p.depth > _DEEP_THRESHOLD]
    if len(deep) < 3:
        return []
    return [CrawlFinding(
        check_id="access.excessive_depth",
        tier=Tier.LIKELY,
        title="Content buried deep from the homepage",
        evidence=f"{len(deep)} page(s) are more than {_DEEP_THRESHOLD} clicks from the homepage; deep content is crawled less reliably.",
        metric={"threshold": _DEEP_THRESHOLD, "count": len(deep)},
        urls=deep,
    )]


# --------------------------------------------------------------------------
# 11 + 12. noindex / X-Robots-Tag
# --------------------------------------------------------------------------
def detect_noindex(result: CrawlResult, ctx=None) -> List[CrawlFinding]:
    findings: List[CrawlFinding] = []
    html = result.html_pages()
    indexable = [p for p in html]
    noindexed = [p for p in html if p.noindex]
    if len(indexable) >= 2 and len(noindexed) == len(indexable):
        quote = _robots_quote(noindexed[0])
        findings.append(CrawlFinding(
            check_id="access.global_noindex",
            tier=Tier.CONFIRMED,
            title="Every crawled page is set to noindex",
            evidence=(
                f"All {len(indexable)} crawled HTML pages carry noindex "
                f"(e.g. {noindexed[0].url} has {quote})."
            ),
            metric={"count": len(noindexed), "quoted_robots": quote},
            urls=[p.url for p in noindexed],
        ))
        return findings
    important = [p for p in noindexed if p.url == result.entry_url or _is_important(p.url)]
    if important:
        quote = _robots_quote(important[0])
        findings.append(CrawlFinding(
            check_id="access.important_noindex",
            tier=Tier.CONFIRMED,
            title="Important page(s) set to noindex",
            evidence=(
                f"{len(important)} important page(s) carry noindex "
                f"(e.g. {important[0].url} has {quote})."
            ),
            metric={"count": len(important), "quoted_robots": quote},
            urls=[p.url for p in important],
        ))
    return findings


# --------------------------------------------------------------------------
# 15. important page discoverability
# --------------------------------------------------------------------------
def detect_important_pages(result: CrawlResult, ctx=None) -> List[CrawlFinding]:
    unreachable: List[str] = []
    for page in result.html_pages():
        if page.url == result.entry_url:
            continue
        if _is_important(page.url) and page.in_sitemap and page.depth is None and page.inbound_internal == 0:
            unreachable.append(page.url)
    if not unreachable:
        return []
    return [CrawlFinding(
        check_id="access.important_page_unreachable",
        tier=Tier.LIKELY,
        title="Important pages are not reachable via internal links",
        evidence=f"{len(unreachable)} important page(s) (e.g. about/contact/pricing) exist in the sitemap but have no internal links, so link-following crawlers may never reach them.",
        metric={"count": len(unreachable)},
        urls=unreachable,
    )]


# --------------------------------------------------------------------------
# 16. crawl traps
# --------------------------------------------------------------------------
def detect_crawl_traps(result: CrawlResult, ctx=None) -> List[CrawlFinding]:
    trap_urls: List[str] = []
    reasons: Dict[str, int] = {"repeating_segments": 0, "session_param": 0}
    candidates = set()
    for page in result.html_pages():
        for link in page.internal_links:
            candidates.add(link)
    # Sort so evidence/affected-URL order is deterministic across processes
    # (set iteration order over strings varies with hash randomization).
    for u in sorted(candidates):
        if urlnorm.repeating_segments(u):
            trap_urls.append(u)
            reasons["repeating_segments"] += 1
        elif urlnorm.has_session_param(u):
            trap_urls.append(u)
            reasons["session_param"] += 1
    if len(trap_urls) < 3 and reasons["repeating_segments"] == 0:
        return []
    return [CrawlFinding(
        check_id="access.crawl_trap",
        tier=Tier.LIKELY,
        title="Potential crawl trap (infinite/near-infinite URL space)",
        evidence=f"Detected {len(trap_urls)} internal URL(s) with repeating path segments or session parameters, which can trap crawlers in an unbounded URL space.",
        metric={"reasons": reasons, "count": len(trap_urls)},
        urls=trap_urls,
    )]


# --------------------------------------------------------------------------
# 17. URL normalization problems
# --------------------------------------------------------------------------
def detect_url_normalization(result: CrawlResult, ctx=None) -> List[CrawlFinding]:
    kinds: Dict[str, int] = {}
    examples: List[str] = []
    for page in result.html_pages():
        for link in page.internal_links:
            vks = urlnorm.variant_kind(link)
            for vk in vks:
                if vk in ("uppercase_host", "index_file", "tracking_params"):
                    kinds[vk] = kinds.get(vk, 0) + 1
                    if len(examples) < 20:
                        examples.append(link)
    if not kinds:
        return []
    return [CrawlFinding(
        check_id="access.url_normalization",
        tier=Tier.INFORMATIONAL,
        title="Inconsistent URL forms in internal links",
        evidence=f"Internal links use inconsistent URL forms ({', '.join(f'{k}:{v}' for k, v in kinds.items())}); consistent URLs reduce duplicate crawling.",
        metric={"kinds": kinds},
        urls=examples,
    )]


# --------------------------------------------------------------------------
# 18. duplicate URL variants
# --------------------------------------------------------------------------
def detect_duplicate_variants(result: CrawlResult, ctx=None) -> List[CrawlFinding]:
    # The crawler deduplicates by lossy key, so it fetches each page once. The
    # observable defect is that the SAME page is *linked* under several distinct
    # URL forms while no canonical consolidates them.
    forms: Dict[str, set] = {}
    for page in result.html_pages():
        forms.setdefault(urlnorm.dedup_key(page.url), set()).add(page.url)
        for link in page.internal_links:
            norm = urlnorm.normalize_url(link)
            forms.setdefault(urlnorm.dedup_key(norm), set()).add(norm)
    dup_urls: List[str] = []
    for key, variants in forms.items():
        if len(variants) < 2:
            continue
        # If the served page declares a canonical, variants are consolidated.
        crawled = next((result.get(v) for v in variants if result.get(v)), None)
        if crawled and crawled.canonical:
            continue
        dup_urls.extend(sorted(variants))
    if not dup_urls:
        return []
    return [CrawlFinding(
        check_id="access.duplicate_url_variants",
        tier=Tier.LIKELY,
        title="Same content served at multiple URL variants without a single canonical",
        evidence=f"{len(dup_urls)} URL variant(s) serve the same content without agreeing on one canonical URL, splitting signals and wasting crawl budget.",
        metric={"count": len(dup_urls)},
        urls=dup_urls,
    )]


# --------------------------------------------------------------------------
# 19. pagination discoverability
# --------------------------------------------------------------------------
def detect_pagination(result: CrawlResult, ctx=None) -> List[CrawlFinding]:
    issues: List[str] = []
    for page in result.html_pages():
        if page.rel_next:
            target = result.get(urlnorm.normalize_url(page.rel_next))
            if target and target.fetched and (400 <= target.status_code < 600 or target.noindex):
                issues.append(page.url)
    if not issues:
        return []
    return [CrawlFinding(
        check_id="access.pagination_issue",
        tier=Tier.LIKELY,
        title="Pagination points to an unreachable or noindex page",
        evidence=f"{len(issues)} paginated page(s) link (rel=next) to a page that errors or is noindex, breaking discovery of later pages in the series.",
        metric={"count": len(issues)},
        urls=issues,
    )]


# --------------------------------------------------------------------------
# 20. crawl budget protection
# --------------------------------------------------------------------------
def detect_crawl_budget(result: CrawlResult, ctx=None) -> List[CrawlFinding]:
    candidates = set()
    for page in result.html_pages():
        candidates.update(page.internal_links)
    if len(candidates) < 10:
        return []
    heavy = [u for u in sorted(candidates)
             if urlnorm.param_count(u) >= _MANY_PARAMS or urlnorm.has_session_param(u)]
    ratio = len(heavy) / max(1, len(candidates))
    if len(heavy) >= 10 and ratio >= 0.3:
        return [CrawlFinding(
            check_id="access.crawl_budget_risk",
            tier=Tier.LIKELY,
            title="Crawl budget risk from parameterized URLs",
            evidence=f"{len(heavy)} of {len(candidates)} internal links ({ratio:.0%}) carry many query parameters or session ids, which can explode the crawlable URL space and starve important pages.",
            metric={"heavy": len(heavy), "total": len(candidates), "ratio": round(ratio, 2)},
            urls=heavy[:20],
        )]
    return []


ALL_DETECTORS = [
    detect_robots_availability,
    detect_robots_directives,
    detect_sitemap_discovery,
    detect_sitemap_validity,
    detect_canonical,
    detect_http_status,
    detect_redirects,
    detect_broken_internal_links,
    detect_orphan_pages,
    detect_depth,
    detect_noindex,
    detect_important_pages,
    detect_crawl_traps,
    detect_url_normalization,
    detect_duplicate_variants,
    detect_pagination,
    detect_crawl_budget,
]


def run_all(result: CrawlResult, ctx=None) -> List[CrawlFinding]:
    findings: List[CrawlFinding] = []
    for det in ALL_DETECTORS:
        try:
            findings.extend(det(result, ctx))
        except Exception:  # noqa: BLE001 -- one detector must not sink the audit
            continue
    return findings


def to_observations(findings: List[CrawlFinding]) -> List[Observation]:
    return [f.to_observation() for f in findings]
