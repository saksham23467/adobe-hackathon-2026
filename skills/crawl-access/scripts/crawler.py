"""Bounded, read-only, robots-respecting crawler engine.

Design guarantees:
  * GET-only, credentials never attached, robots.txt honored.
  * Hard caps: max pages, max depth, max redirects, wall-clock deadline.
  * URL deduplication by lossy key; no infinite crawls.
  * Records exact evidence: status, redirect chain, headers, canonical, robots
    directives, link graph — into a normalized CrawlResult.

The engine is fetcher-agnostic. LiveFetcher hits the network through the shared
SafetyGate; FixtureFetcher and DictFetcher serve local data for hermetic tests.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.parse
import urllib.robotparser
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_LIB = os.path.join(_ROOT, "lib")
for _p in (_HERE, _LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import urlnorm  # noqa: E402
from html_parse import parse_html  # noqa: E402
from crawl_result import (  # noqa: E402
    CrawlPage,
    CrawlResult,
    CrawlStats,
    RedirectHop,
    RobotsInfo,
    SitemapInfo,
)
from audit_common.net import (  # noqa: E402
    is_public_http_url,
    looks_like_xml,
    same_registrable_site,
    trap_query,
)
from audit_common.semantics import worth_crawling  # noqa: E402
from audit_common.curlfetch import http_get  # noqa: E402


# --------------------------------------------------------------------------
# Fetcher abstraction
# --------------------------------------------------------------------------
@dataclass
class RawResponse:
    requested_url: str
    final_url: str = ""
    status: int = 0                       # 0 = not fetched / unknown
    headers: Dict[str, str] = field(default_factory=dict)
    body: str = ""
    content_type: str = ""
    redirect_chain: List[RedirectHop] = field(default_factory=list)
    error: str = ""


class LiveFetcher:
    """Network fetcher. Every request is curl GET through SafetyGate."""

    def __init__(self, gate, max_redirects: int = 5) -> None:
        self.gate = gate
        self.max_redirects = max_redirects

    def fetch(self, url: str, count_page: bool = True, accept: Optional[str] = None) -> RawResponse:
        resp = RawResponse(requested_url=url, final_url=url)
        self.gate.assert_readonly("GET")
        # Ledger every attempted network request (proves dedup: duplicates -> 0).
        try:
            self.gate.note_request(url)
        except Exception:
            pass
        if not is_public_http_url(url, resolve=True):
            resp.error = "blocked_host"
            return resp
        path = urllib.parse.urlsplit(url).path.rstrip("/") or "/"
        fetching_robots = path.endswith("robots.txt")
        if (
            not fetching_robots
            and self.gate.config.respect_robots
            and not self.gate.can_fetch(url)
        ):
            resp.error = "robots_disallow_our_ua"
            return resp
        if count_page and not self.gate.note_page_fetch():
            resp.error = "page_cap_reached"
            return resp
        if self.gate.budget_exhausted():
            resp.error = "budget_exhausted"
            return resp
        host = urllib.parse.urlsplit(url).netloc
        self.gate.rate.wait(host)
        accept = accept or "text/html,application/xhtml+xml,application/xml,text/xml;q=0.9,*/*;q=0.8"
        _t0 = time.monotonic()
        try:
            got = http_get(
                url,
                user_agent=self.gate.config.user_agent,
                timeout_s=self.gate.config.request_timeout_s,
                max_bytes=self.gate.config.max_bytes_per_page,
                max_redirects=self.max_redirects,
                accept=accept,
            )
            resp.status = got.status
            resp.final_url = got.final_url or url
            resp.headers = got.headers
            resp.content_type = got.content_type
            resp.body = got.body
            resp.error = got.error
            resp.redirect_chain = [
                RedirectHop(from_url=a or url, to_url=b, status=c)
                for a, b, c in got.redirect_chain
            ]
            if fetching_robots:
                if got.error and not (200 <= resp.status < 300):
                    status = "missing" if 400 <= resp.status < 500 else "unreachable"
                    self.gate.cache_robots(url, "", status)
                else:
                    status = "ok" if (200 <= resp.status < 300 and resp.body.strip()) else "missing"
                    self.gate.cache_robots(url, resp.body if status == "ok" else "", status)
            return resp
        except Exception as e:  # noqa: BLE001
            resp.error = f"fetch_error:{type(e).__name__}"
            if fetching_robots:
                self.gate.cache_robots(url, "", "unreachable")
        finally:
            try:
                self.gate.add_fetch_time(time.monotonic() - _t0)
            except Exception:
                pass
        return resp


class DictFetcher:
    """In-memory fetcher for unit tests. mapping: url -> dict(status, body, ...)."""

    def __init__(self, mapping: Dict[str, Dict]) -> None:
        self.mapping = {urlnorm.normalize_url(k): v for k, v in mapping.items()}

    def fetch(self, url: str, count_page: bool = True, accept: Optional[str] = None) -> RawResponse:
        key = urlnorm.normalize_url(url)
        entry = self.mapping.get(key)
        if entry is None:
            return RawResponse(requested_url=url, final_url=url, status=0, error="unmapped")
        chain = [
            RedirectHop(from_url=h[0], to_url=h[1], status=h[2])
            for h in entry.get("redirect_chain", [])
        ]
        return RawResponse(
            requested_url=url,
            final_url=entry.get("final_url", key),
            status=int(entry.get("status", 200)),
            headers={k.lower(): v for k, v in entry.get("headers", {}).items()},
            body=entry.get("body", ""),
            content_type=entry.get("content_type", "text/html" if entry.get("body") else ""),
            redirect_chain=chain,
            error=entry.get("error", ""),
        )


class FixtureFetcher:
    """Serves a local fixture directory (pages.json: pages[] + aux[])."""

    def __init__(self, fixture_dir: str) -> None:
        import json

        self.dir = fixture_dir
        with open(os.path.join(fixture_dir, "pages.json"), "r", encoding="utf-8") as fh:
            index = json.load(fh)
        self.map: Dict[str, Dict] = {}
        for entry in index.get("pages", []) + index.get("aux", []):
            self.map[urlnorm.normalize_url(entry["url"])] = entry

    def _read(self, name: str) -> str:
        with open(os.path.join(self.dir, name), "r", encoding="utf-8") as fh:
            return fh.read()

    def fetch(self, url: str, count_page: bool = True, accept: Optional[str] = None) -> RawResponse:
        key = urlnorm.normalize_url(url)
        entry = self.map.get(key)
        if entry is None:
            return RawResponse(requested_url=url, final_url=url, status=0, error="not_in_fixture")
        # Follow a simulated redirect if declared.
        chain: List[RedirectHop] = []
        hops = 0
        while entry is not None and entry.get("redirect_to") and hops < 10:
            target = urlnorm.normalize_url(entry["redirect_to"], base=key)
            chain.append(RedirectHop(from_url=key, to_url=target, status=int(entry.get("status", 301))))
            key = target
            entry = self.map.get(key)
            hops += 1
        if entry is None:
            return RawResponse(requested_url=url, final_url=key, status=0, redirect_chain=chain, error="not_in_fixture")
        hdrs = {k.lower(): v for k, v in (entry.get("headers") or {}).items()}
        ct = hdrs.get("content-type") or entry.get("content_type", "text/html")
        body = self._read(entry["file"]) if entry.get("file") else entry.get("body", "")
        return RawResponse(
            requested_url=url,
            final_url=key,
            status=int(entry.get("status", 200)),
            headers=hdrs or {"content-type": ct},
            body=body,
            content_type=ct,
            redirect_chain=chain,
        )


# --------------------------------------------------------------------------
# Robots + sitemap parsing
# --------------------------------------------------------------------------
def parse_robots(raw: str) -> Dict[str, object]:
    """Parse robots.txt into groups, sitemaps, and disallow-all agents."""
    sitemaps: List[str] = []
    disallow_all: List[str] = []
    current_agents: List[str] = []
    group_started = False
    agent_disallows: Dict[str, List[str]] = {}
    for line in raw.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field, _, value = line.partition(":")
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            if group_started:
                current_agents = []
                group_started = False
            current_agents.append(value)
            agent_disallows.setdefault(value, [])
        elif field == "disallow":
            group_started = True
            for ag in current_agents:
                agent_disallows.setdefault(ag, []).append(value)
        elif field == "allow":
            group_started = True
        elif field == "sitemap":
            sitemaps.append(value)
    for ag, rules in agent_disallows.items():
        if any(r == "/" for r in rules):
            disallow_all.append(ag)
    return {"sitemaps": sitemaps, "disallow_all": disallow_all, "agent_disallows": agent_disallows}


def parse_sitemap(url: str, resp: RawResponse, url_cap: int = 200) -> SitemapInfo:
    import xml.etree.ElementTree as ET

    info = SitemapInfo(url=url, fetched=(resp.status != 0), status_code=resp.status)
    if resp.error == "robots_disallow_our_ua":
        # We were not permitted to fetch it; we cannot judge it broken.
        info.kind = "robots_disallowed"
        info.errors.append("robots_disallowed")
        return info
    if resp.status == 0 or resp.error:
        return info
    if 200 <= resp.status < 300:
        info.available = True
    if not (200 <= resp.status < 300) or not resp.body.strip():
        return info
    if not looks_like_xml(resp.body, resp.content_type or resp.headers.get("content-type", "")):
        # Soft-404 HTML at /sitemap.xml is "not a sitemap", not "invalid XML".
        info.available = False
        info.kind = "not_xml"
        info.errors.append("not_xml")
        return info
    try:
        root = ET.fromstring(resp.body.encode("utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        info.well_formed = False
        info.kind = "invalid"
        info.errors.append(f"xml_parse_error:{type(e).__name__}")
        return info
    info.well_formed = True
    tag = root.tag.split("}")[-1].lower()
    if tag == "sitemapindex":
        info.kind = "sitemapindex"
        for sm in root:
            for child in sm:
                if child.tag.split("}")[-1].lower() == "loc" and child.text:
                    info.child_sitemaps.append(child.text.strip())
    elif tag == "urlset":
        info.kind = "urlset"
        for u in root:
            for child in u:
                if child.tag.split("}")[-1].lower() == "loc" and child.text:
                    if len(info.urls) < url_cap:
                        info.urls.append(child.text.strip())
                    info.url_count += 1
    else:
        info.kind = "invalid"
        info.well_formed = False
        info.errors.append(f"unexpected_root:{tag}")
    return info


# --------------------------------------------------------------------------
# Crawler
# --------------------------------------------------------------------------
@dataclass
class CrawlLimits:
    max_pages: int = 40
    max_depth: int = 5
    max_redirects: int = 5
    deadline_s: float = 120.0
    sitemap_fetch_cap: int = 5
    max_workers: int = 1  # >1 enables bounded, polite, level-synchronous concurrency


class Crawler:
    def __init__(
        self,
        fetcher,
        entry_url: str,
        limits: Optional[CrawlLimits] = None,
        user_agent: str = "brand-ai-readiness-audit",
        clock: Callable[[], float] = time.monotonic,
        respect_robots: bool = True,
    ) -> None:
        self.fetcher = fetcher
        self.entry_url = urlnorm.normalize_url(entry_url)
        self.site_host = urllib.parse.urlsplit(self.entry_url).netloc
        self.limits = limits or CrawlLimits()
        self.user_agent = user_agent
        self._clock = clock
        self._start = clock()
        self._rp: Optional[urllib.robotparser.RobotFileParser] = None
        self._robots_reachable = True
        self._respect_robots = respect_robots

    # -- helpers --------------------------------------------------------
    def _time_up(self) -> bool:
        return (self._clock() - self._start) >= self.limits.deadline_s

    def _can_fetch(self, url: str) -> bool:
        if not self._respect_robots:
            return True
        # Fail closed when robots.txt is unreachable (5xx / network error).
        if not self._robots_reachable:
            return False
        if self._rp is None:
            return True  # missing robots → no rules
        try:
            return self._rp.can_fetch(self.user_agent, url)
        except Exception:
            return False

    def _on_site(self, url: str) -> bool:
        return urlnorm.same_site(url, self.site_host) or same_registrable_site(url, self.site_host)

    def _origin(self) -> str:
        p = urllib.parse.urlsplit(self.entry_url)
        return f"{p.scheme}://{p.netloc}"

    # -- robots ---------------------------------------------------------
    def _load_robots(self) -> RobotsInfo:
        robots_url = urllib.parse.urljoin(self._origin() + "/", "robots.txt")
        resp = self.fetcher.fetch(
            robots_url, count_page=False, accept="text/plain,*/*;q=0.1"
        )
        info = RobotsInfo(url=robots_url, fetched=(resp.status != 0), status_code=resp.status)
        if resp.error and resp.status == 0:
            # Fixture/test maps with no robots.txt are "missing", not 5xx.
            if resp.error in ("not_in_fixture", "unmapped"):
                info.reachable = True
                info.available = False
                info.error = resp.error
                self._robots_reachable = True
                return info
            info.reachable = False
            info.error = resp.error
            self._robots_reachable = False
            return info
        if resp.status >= 500:
            info.reachable = False
            info.error = f"status_{resp.status}"
            self._robots_reachable = False
            return info
        self._robots_reachable = True
        if 200 <= resp.status < 300 and resp.body.strip():
            info.available = True
            info.raw = resp.body
            parsed = parse_robots(resp.body)
            info.sitemaps = list(parsed["sitemaps"])  # type: ignore[index]
            info.disallow_all_agents = list(parsed["disallow_all"])  # type: ignore[index]
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(resp.body.splitlines())
            self._rp = rp
            info.our_ua_blocked_root = not rp.can_fetch(self.user_agent, "/")
        return info

    # -- sitemaps -------------------------------------------------------
    def _load_sitemaps(self, robots: RobotsInfo) -> List[SitemapInfo]:
        candidates: List[str] = [
            u for u in robots.sitemaps if self._on_site(u) and is_public_http_url(u)
        ]
        # Only guess /sitemap.xml when robots listed none. A HTML 200 there is
        # "not a sitemap", not an invalid-sitemap defect.
        if not candidates:
            default_sm = urllib.parse.urljoin(self._origin() + "/", "sitemap.xml")
            if is_public_http_url(default_sm):
                candidates.append(default_sm)
        results: List[SitemapInfo] = []
        fetched = 0
        queue = deque(candidates)
        seen_sm = set()
        while queue and fetched < self.limits.sitemap_fetch_cap and not self._time_up():
            sm_url = queue.popleft()
            if sm_url in seen_sm:
                continue
            seen_sm.add(sm_url)
            if not self._on_site(sm_url) or not is_public_http_url(sm_url):
                continue
            resp = self.fetcher.fetch(
                sm_url, count_page=False,
                accept="application/xml,text/xml,application/xhtml+xml,*/*;q=0.1",
            )
            info = parse_sitemap(sm_url, resp)
            results.append(info)
            fetched += 1
            for child in info.child_sitemaps:
                if child not in seen_sm and self._on_site(child) and is_public_http_url(child):
                    queue.append(child)
        return results

    # -- main crawl -----------------------------------------------------
    def run(self) -> CrawlResult:
        result = CrawlResult(site_host=self.site_host, entry_url=self.entry_url)
        result.limits = {
            "max_pages": self.limits.max_pages,
            "max_depth": self.limits.max_depth,
            "max_redirects": self.limits.max_redirects,
            "deadline_s": self.limits.deadline_s,
        }
        stats = CrawlStats()

        result.robots = self._load_robots()
        result.sitemaps = self._load_sitemaps(result.robots)
        sitemap_urls = []
        for sm in result.sitemaps:
            sitemap_urls.extend(sm.urls)

        seen_keys: set = set()
        queued_keys: set = set()
        queued_keys.add(urlnorm.dedup_key(self.entry_url))
        max_workers = max(1, int(getattr(self.limits, "max_workers", 1) or 1))

        def _enqueue(bucket, u: str, depth: Optional[int], source: str, parent: Optional[str]) -> None:
            key = urlnorm.dedup_key(u)
            if key in seen_keys or key in queued_keys:
                stats.urls_deduped += 1  # dedup working: same page reached again
                existing = result.pages.get(urlnorm.normalize_url(u))
                if existing and parent and parent not in existing.discovered_from:
                    existing.discovered_from.append(parent)
                return
            if urlnorm.has_session_param(u) or urlnorm.repeating_segments(u) or trap_query(u):
                return
            if not self._on_site(u):
                return
            # Non-document URLs stay on the parent as internal_links but must
            # not consume a crawl slot (svg/image/video/js/css/pdf/api).
            if source != "seed" and not worth_crawling(u):
                queued_keys.add(key)
                stats.urls_skipped_nondocument += 1
                return
            queued_keys.add(key)
            bucket.append((u, depth, source, parent))

        def _process(page: CrawlPage, depth: Optional[int], resp, next_bucket) -> None:
            self._record_response(page, resp)
            if page.fetched:
                stats.pages_crawled += 1
                if depth is not None:
                    stats.max_depth_reached = max(stats.max_depth_reached, depth)
            result.pages[page.url] = page
            if page.is_html and depth is not None and depth < self.limits.max_depth and not page.nofollow:
                for link in page.internal_links:
                    _enqueue(next_bucket, link, depth + 1, "link", page.url)

        if max_workers <= 1:
            # -- Sequential BFS (default; identical to the original behavior) --
            queue: deque = deque()
            queue.append((self.entry_url, 0, "seed", None))
            while queue:
                if stats.pages_crawled >= self.limits.max_pages:
                    stats.page_cap_hit = True
                    break
                if self._time_up():
                    stats.budget_hit = True
                    break
                url, depth, source, parent = queue.popleft()
                norm = urlnorm.normalize_url(url)
                key = urlnorm.dedup_key(norm)
                if key in seen_keys:
                    page = result.pages.get(norm)
                    if page and parent and parent not in page.discovered_from:
                        page.discovered_from.append(parent)
                    continue
                seen_keys.add(key)
                page = CrawlPage(url=norm, requested_url=url, depth=depth)
                page.sources.append(source)
                if parent:
                    page.discovered_from.append(parent)
                if source == "sitemap":
                    page.in_sitemap = True
                if not self._can_fetch(norm):
                    page.error = "robots_disallow"
                    stats.robots_disallowed_count += 1
                    result.pages[norm] = page
                    continue
                stats.pages_requested += 1
                resp = self.fetcher.fetch(norm)
                _process(page, depth, resp, queue)
        else:
            # -- Bounded, polite, deterministic level-synchronous BFS --------
            # Each BFS wavefront is fetched concurrently (network latency
            # overlaps), while the per-host RateLimiter still spaces dispatch by
            # min_delay so politeness is preserved. Results are processed in
            # deterministic level order, so depth/link-graph output is identical
            # regardless of thread scheduling.
            frontier = [(self.entry_url, 0, "seed", None)]
            while frontier:
                if self._time_up():
                    stats.budget_hit = True
                    break
                remaining = self.limits.max_pages - stats.pages_crawled
                if remaining <= 0:
                    stats.page_cap_hit = True
                    break
                to_fetch: List[str] = []
                pending: Dict[str, "tuple"] = {}
                for url, depth, source, parent in frontier:
                    if len(to_fetch) >= remaining:
                        stats.page_cap_hit = True
                        break
                    norm = urlnorm.normalize_url(url)
                    key = urlnorm.dedup_key(norm)
                    if key in seen_keys:
                        page = result.pages.get(norm)
                        if page and parent and parent not in page.discovered_from:
                            page.discovered_from.append(parent)
                        continue
                    seen_keys.add(key)
                    page = CrawlPage(url=norm, requested_url=url, depth=depth)
                    page.sources.append(source)
                    if parent:
                        page.discovered_from.append(parent)
                    if source == "sitemap":
                        page.in_sitemap = True
                    if not self._can_fetch(norm):
                        page.error = "robots_disallow"
                        stats.robots_disallowed_count += 1
                        result.pages[norm] = page
                        continue
                    pending[norm] = (page, depth)
                    to_fetch.append(norm)
                responses = self._fetch_batch(to_fetch, max_workers)
                if to_fetch:
                    stats.max_concurrency = max(
                        stats.max_concurrency, min(max_workers, len(to_fetch))
                    )
                next_frontier: List["tuple"] = []
                for norm in to_fetch:  # deterministic: level (BFS) order
                    page, depth = pending[norm]
                    stats.pages_requested += 1
                    _process(page, depth, responses.get(norm), next_frontier)
                frontier = next_frontier

        # Fetch sitemap-only URLs that were not reached via links (for orphan detection),
        # within remaining budget.
        for u in sitemap_urls:
            if stats.pages_crawled >= self.limits.max_pages:
                stats.page_cap_hit = True
                break
            if self._time_up():
                stats.budget_hit = True
                break
            if not self._on_site(u) or not is_public_http_url(u):
                continue
            norm = urlnorm.normalize_url(u)
            key = urlnorm.dedup_key(norm)
            if not worth_crawling(u):
                if key not in seen_keys:
                    seen_keys.add(key)
                    stats.urls_skipped_nondocument += 1
                continue
            if key in seen_keys:
                existing = result.pages.get(norm)
                if existing:
                    existing.in_sitemap = True
                continue
            seen_keys.add(key)
            page = CrawlPage(url=norm, requested_url=u, depth=None, in_sitemap=True)
            page.sources.append("sitemap")
            if not self._can_fetch(norm):
                page.error = "robots_disallow"
                stats.robots_disallowed_count += 1
                result.pages[norm] = page
                continue
            stats.pages_requested += 1
            resp = self.fetcher.fetch(norm)
            self._record_response(page, resp)
            if page.fetched:
                stats.pages_crawled += 1
            result.pages[norm] = page

        # Mark in_sitemap flag on any crawled page that also appears in a sitemap.
        sm_keys = {urlnorm.dedup_key(u) for u in sitemap_urls}
        for p in result.pages.values():
            if urlnorm.dedup_key(p.url) in sm_keys:
                p.in_sitemap = True

        # Compute inbound internal link counts from the link graph.
        self._compute_inbound(result)

        stats.elapsed_s = round(self._clock() - self._start, 3)
        result.stats = stats
        return result

    # -- batched fetch --------------------------------------------------
    def _fetch_batch(self, norms: List[str], max_workers: int) -> Dict[str, RawResponse]:
        """Fetch several URLs concurrently. The fetcher/gate still enforce
        robots, per-host politeness, byte caps, budget, and read-only rules."""
        if not norms:
            return {}
        if max_workers <= 1 or len(norms) == 1:
            return {n: self.fetcher.fetch(n) for n in norms}
        out: Dict[str, RawResponse] = {}
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(max_workers, len(norms))) as ex:
            futs = {ex.submit(self.fetcher.fetch, n): n for n in norms}
            for fut, n in futs.items():
                try:
                    out[n] = fut.result()
                except Exception as e:  # noqa: BLE001
                    out[n] = RawResponse(requested_url=n, final_url=n,
                                         error=f"fetch_error:{type(e).__name__}")
        return out

    # -- response -> page ----------------------------------------------
    def _record_response(self, page: CrawlPage, resp: RawResponse) -> None:
        page.status_code = resp.status
        page.final_url = resp.final_url or page.url
        page.headers = resp.headers
        page.redirect_chain = resp.redirect_chain
        page.content_type = resp.content_type or resp.headers.get("content-type", "")
        page.error = resp.error
        page.body = resp.body or ""
        page.fetched = resp.status != 0 and not (resp.error and resp.status == 0)
        page.ok = 200 <= resp.status < 300
        try:
            from audit_common.semantics import classify_resource
            _cls = classify_resource(
                page.url, content_type=page.content_type, body=page.body,
                status_code=page.status_code, headers=page.headers,
            )
            page.is_html = _cls.resource_type == "html_document"
        except Exception:
            page.is_html = "html" in (page.content_type or "").lower()

        # X-Robots-Tag (header) noindex/nofollow.
        xrt = resp.headers.get("x-robots-tag", "").lower()
        page.x_robots_tag = xrt

        if page.is_html and resp.body:
            parsed = parse_html(resp.body)
            base = parsed.base_href or page.final_url
            page.title = parsed.title
            page.meta_robots = parsed.meta_robots
            if parsed.canonical:
                page.canonical = urlnorm.normalize_url(parsed.canonical, base=base)
            if parsed.rel_next:
                page.rel_next = urlnorm.normalize_url(parsed.rel_next, base=base)
            if parsed.rel_prev:
                page.rel_prev = urlnorm.normalize_url(parsed.rel_prev, base=base)
            internal, external = [], 0
            for href in parsed.hrefs:
                if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
                    continue
                absu = urlnorm.normalize_url(href, base=base)
                if not urlnorm.is_http_url(absu):
                    continue
                if self._on_site(absu):
                    internal.append(absu)
                else:
                    external += 1
            # Deduplicate internal links preserving order.
            page.internal_links = list(dict.fromkeys(internal))
            page.external_link_count = external

        combined = (page.meta_robots + " " + page.x_robots_tag).lower()
        page.noindex = "noindex" in combined
        page.nofollow = "nofollow" in combined

    def _compute_inbound(self, result: CrawlResult) -> None:
        inbound: Dict[str, int] = {}
        for page in result.pages.values():
            for link in page.internal_links:
                key = urlnorm.normalize_url(link)
                inbound[key] = inbound.get(key, 0) + 1
        for norm, page in result.pages.items():
            page.inbound_internal = inbound.get(norm, 0)
