"""Normalized CrawlResult structures.

Everything the crawler learns about a site is recorded here as plain, typed,
JSON-serializable data. Detectors are pure functions over a CrawlResult, so they
are trivial to unit-test and never touch the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class RedirectHop:
    from_url: str
    to_url: str
    status: int


@dataclass
class CrawlPage:
    url: str                       # normalized absolute URL (the crawl key)
    requested_url: str = ""        # the URL as first discovered
    final_url: str = ""            # after following redirects
    status_code: int = 0           # 0 = not fetched / unknown
    fetched: bool = False          # a real response was received
    ok: bool = False               # 2xx and fetched
    content_type: str = ""
    is_html: bool = False
    depth: Optional[int] = None    # BFS depth from homepage via links; None if only via sitemap
    redirect_chain: List[RedirectHop] = field(default_factory=list)
    headers: Dict[str, str] = field(default_factory=dict)
    x_robots_tag: str = ""
    meta_robots: str = ""
    noindex: bool = False
    nofollow: bool = False
    canonical: str = ""            # resolved absolute canonical, if present
    internal_links: List[str] = field(default_factory=list)
    external_link_count: int = 0
    inbound_internal: int = 0      # number of internal pages linking here
    discovered_from: List[str] = field(default_factory=list)
    sources: List[str] = field(default_factory=list)  # {"link","sitemap","seed"}
    in_sitemap: bool = False
    rel_next: str = ""
    rel_prev: str = ""
    title: str = ""
    error: str = ""
    body: str = ""  # raw response body (HTML/XML); used to hydrate the shared cache

    def to_dict(self) -> Dict:
        d = dict(self.__dict__)
        d["redirect_chain"] = [h.__dict__ for h in self.redirect_chain]
        return d


@dataclass
class RobotsInfo:
    url: str = ""
    fetched: bool = False
    available: bool = False        # a real robots.txt was served (2xx)
    status_code: int = 0
    reachable: bool = True         # False if 5xx/timeout (crawlers may treat as disallow-all)
    raw: str = ""
    sitemaps: List[str] = field(default_factory=list)
    disallow_all_agents: List[str] = field(default_factory=list)  # UAs with Disallow: /
    our_ua_blocked_root: bool = False
    ai_agents_blocked: List[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> Dict:
        return dict(self.__dict__)


@dataclass
class SitemapInfo:
    url: str
    fetched: bool = False
    available: bool = False
    status_code: int = 0
    well_formed: bool = False
    kind: str = "unknown"          # urlset | sitemapindex | invalid | unknown
    url_count: int = 0
    urls: List[str] = field(default_factory=list)      # capped sample
    child_sitemaps: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return dict(self.__dict__)


@dataclass
class CrawlStats:
    pages_crawled: int = 0
    pages_requested: int = 0
    max_depth_reached: int = 0
    elapsed_s: float = 0.0
    budget_hit: bool = False
    page_cap_hit: bool = False
    robots_disallowed_count: int = 0
    urls_deduped: int = 0          # internal links skipped because already seen/queued
    urls_skipped_nondocument: int = 0  # svg/image/video/js/css/… not fetched
    max_concurrency: int = 1       # peak in-flight fetches observed

    def to_dict(self) -> Dict:
        return dict(self.__dict__)


@dataclass
class CrawlResult:
    site_host: str
    entry_url: str
    pages: Dict[str, CrawlPage] = field(default_factory=dict)  # keyed by normalized url
    robots: RobotsInfo = field(default_factory=RobotsInfo)
    sitemaps: List[SitemapInfo] = field(default_factory=list)
    stats: CrawlStats = field(default_factory=CrawlStats)
    limits: Dict = field(default_factory=dict)

    # -- convenience accessors used by detectors -----------------------
    def fetched_pages(self) -> List[CrawlPage]:
        return [p for p in self.pages.values() if p.fetched]

    def html_pages(self) -> List[CrawlPage]:
        return [p for p in self.pages.values() if p.fetched and p.is_html]

    def sitemap_urls(self) -> List[str]:
        urls: List[str] = []
        for sm in self.sitemaps:
            urls.extend(sm.urls)
        return urls

    def get(self, url: str) -> Optional[CrawlPage]:
        return self.pages.get(url)

    def to_dict(self) -> Dict:
        return {
            "site_host": self.site_host,
            "entry_url": self.entry_url,
            "pages": {k: p.to_dict() for k, p in self.pages.items()},
            "robots": self.robots.to_dict(),
            "sitemaps": [s.to_dict() for s in self.sitemaps],
            "stats": self.stats.to_dict(),
            "limits": self.limits,
        }
