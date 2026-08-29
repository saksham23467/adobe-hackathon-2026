import _crawl_paths  # noqa: F401  (sets sys.path)
import urlnorm
import detectors
from detectors import Tier
from crawl_result import CrawlPage, CrawlResult, RedirectHop, RobotsInfo, SitemapInfo
from audit_common.schema import Confidence, Status


def mk_page(url, **kw):
    p = CrawlPage(url=urlnorm.normalize_url(url))
    p.fetched = kw.pop("fetched", True)
    p.is_html = kw.pop("is_html", True)
    p.status_code = kw.pop("status_code", 200)
    p.ok = 200 <= p.status_code < 300
    for k, v in kw.items():
        setattr(p, k, v)
    return p


def mk_result(pages=None, **kw):
    r = CrawlResult(site_host=kw.pop("site_host", "s.com"),
                    entry_url=kw.pop("entry_url", "https://s.com/"))
    for p in pages or []:
        r.pages[p.url] = p
    for k, v in kw.items():
        setattr(r, k, v)
    return r


class Ctx:
    def __init__(self, tokens):
        self.ai_agents = {"agents": [{"token": t} for t in tokens]}


def ids(findings):
    return {f.check_id for f in findings}


# 1. robots availability
def test_robots_unreachable_is_likely():
    r = mk_result(robots=RobotsInfo(url="u", reachable=False, status_code=503))
    f = detectors.detect_robots_availability(r)
    assert f[0].check_id == "access.robots_unreachable" and f[0].tier == Tier.LIKELY


def test_robots_missing_is_informational():
    r = mk_result(robots=RobotsInfo(url="u", reachable=True, available=False, status_code=404))
    f = detectors.detect_robots_availability(r)
    assert f[0].check_id == "access.robots_missing" and f[0].tier == Tier.INFORMATIONAL


# 2. robots directives
def test_global_disallow_all_confirmed():
    r = mk_result(robots=RobotsInfo(available=True, disallow_all_agents=["*"], raw="User-agent: *\nDisallow: /"))
    f = detectors.detect_robots_directives(r)
    assert f[0].check_id == "access.global_disallow_all" and f[0].tier == Tier.CONFIRMED


def test_ai_ua_blocked_confirmed():
    raw = "User-agent: *\nAllow: /\nUser-agent: GPTBot\nDisallow: /\n"
    r = mk_result(robots=RobotsInfo(available=True, raw=raw))
    f = detectors.detect_robots_directives(r, Ctx(["GPTBot", "ClaudeBot"]))
    assert ids(f) == {"access.ai_ua_blocked"}
    assert f[0].metric["blocked_agents"] == ["GPTBot"]


# 3 + 4. sitemap
def test_sitemap_missing_informational():
    f = detectors.detect_sitemap_discovery(mk_result())
    assert f[0].check_id == "access.sitemap_missing" and f[0].tier == Tier.INFORMATIONAL


def test_sitemap_broken_reference_is_likely():
    r = mk_result(robots=RobotsInfo(available=True, sitemaps=["https://s.com/sitemap.xml"]),
                  sitemaps=[SitemapInfo(url="https://s.com/sitemap.xml", available=False, status_code=404)])
    f = detectors.detect_sitemap_discovery(r)
    assert f[0].tier == Tier.LIKELY


def test_sitemap_unfetched_is_not_a_broken_pointer():
    r = mk_result(robots=RobotsInfo(available=True, sitemaps=["https://s.com/sitemap.xml"]),
                  sitemaps=[SitemapInfo(url="https://s.com/sitemap.xml", available=False, status_code=0)])
    f = detectors.detect_sitemap_discovery(r)
    assert not any(x.tier == Tier.LIKELY for x in f)


def test_sitemap_invalid_confirmed():
    r = mk_result(sitemaps=[SitemapInfo(url="u", available=True, well_formed=False, errors=["xml_parse_error"])])
    f = detectors.detect_sitemap_validity(r)
    assert f[0].check_id == "access.sitemap_invalid" and f[0].tier == Tier.CONFIRMED


# 5 + 13. canonical
def test_canonical_to_error_confirmed():
    target = mk_page("https://s.com/gone", status_code=404)
    page = mk_page("https://s.com/a", canonical="https://s.com/gone")
    r = mk_result([page, target])
    f = detectors.detect_canonical(r)
    assert "access.canonical_to_error" in ids(f)


def test_canonical_cross_host_is_likely():
    page = mk_page("https://s.com/a", canonical="https://other.com/a")
    f = detectors.detect_canonical(mk_result([page]))
    assert f[0].check_id == "access.canonical_inconsistent" and f[0].tier == Tier.LIKELY


# 6. http status
def test_homepage_error_confirmed():
    home = mk_page("https://s.com/", status_code=500)
    f = detectors.detect_http_status(mk_result([home]))
    assert "access.homepage_unreachable" in ids(f)


def test_server_errors_confirmed():
    home = mk_page("https://s.com/")
    bad = mk_page("https://s.com/x", status_code=503)
    f = detectors.detect_http_status(mk_result([home, bad]))
    assert "access.server_errors" in ids(f)


# 7. redirects
def test_long_redirect_chain_likely():
    hops = [RedirectHop(f"/{i}", f"/{i+1}", 301) for i in range(4)]
    page = mk_page("https://s.com/a", redirect_chain=hops)
    f = detectors.detect_redirects(mk_result([page]))
    assert f[0].check_id == "access.redirect_chain_long" and f[0].tier == Tier.LIKELY


def test_redirect_loop_confirmed():
    hops = [RedirectHop("/a", "/b", 301), RedirectHop("/b", "/a", 301)]
    page = mk_page("https://s.com/a", redirect_chain=hops)
    f = detectors.detect_redirects(mk_result([page]))
    assert f[0].check_id == "access.redirect_loop" and f[0].tier == Tier.CONFIRMED


# 8 + 14. broken internal links
def test_broken_internal_links_confirmed():
    home = mk_page("https://s.com/", internal_links=["https://s.com/dead"])
    dead = mk_page("https://s.com/dead", status_code=404)
    f = detectors.detect_broken_internal_links(mk_result([home, dead]))
    assert f[0].check_id == "access.broken_internal_links"
    assert f[0].metric["targets"]["https://s.com/dead"] == 404


def test_unfetched_link_is_not_broken():
    home = mk_page("https://s.com/", internal_links=["https://s.com/unknown"])
    unknown = mk_page("https://s.com/unknown", fetched=False, status_code=0)
    f = detectors.detect_broken_internal_links(mk_result([home, unknown]))
    assert f == []


# 9. orphans
def test_orphan_pages_likely():
    home = mk_page("https://s.com/")
    orphan = mk_page("https://s.com/lonely", in_sitemap=True, depth=None, inbound_internal=0)
    f = detectors.detect_orphan_pages(mk_result([home, orphan]))
    assert f[0].check_id == "access.orphan_pages" and f[0].tier == Tier.LIKELY


# 10. depth
def test_excessive_depth_likely():
    pages = [mk_page("https://s.com/")] + [mk_page(f"https://s.com/d{i}", depth=6) for i in range(3)]
    f = detectors.detect_depth(mk_result(pages))
    assert f[0].check_id == "access.excessive_depth"


def test_shallow_site_no_depth_finding():
    pages = [mk_page("https://s.com/"), mk_page("https://s.com/a", depth=1)]
    assert detectors.detect_depth(mk_result(pages)) == []


# 11 + 12. noindex
def test_global_noindex_confirmed():
    pages = [mk_page("https://s.com/", noindex=True), mk_page("https://s.com/b", noindex=True)]
    f = detectors.detect_noindex(mk_result(pages))
    assert f[0].check_id == "access.global_noindex"


def test_important_noindex_confirmed():
    pages = [mk_page("https://s.com/"), mk_page("https://s.com/about", noindex=True)]
    f = detectors.detect_noindex(mk_result(pages))
    assert f[0].check_id == "access.important_noindex"


def test_single_ordinary_noindex_is_not_flagged():
    pages = [mk_page("https://s.com/"), mk_page("https://s.com/promo", noindex=True)]
    assert detectors.detect_noindex(mk_result(pages)) == []


# 15. important page discoverability
def test_important_page_unreachable_likely():
    home = mk_page("https://s.com/")
    about = mk_page("https://s.com/about", in_sitemap=True, depth=None, inbound_internal=0)
    f = detectors.detect_important_pages(mk_result([home, about]))
    assert f[0].check_id == "access.important_page_unreachable"


# 16. crawl traps
def test_crawl_trap_likely():
    home = mk_page("https://s.com/", internal_links=["https://s.com/cal/2020/cal/2020/cal/2020/"])
    f = detectors.detect_crawl_traps(mk_result([home]))
    assert f[0].check_id == "access.crawl_trap"


# 17. url normalization
def test_url_normalization_informational():
    home = mk_page("https://s.com/", internal_links=["https://s.com/a/index.html"])
    f = detectors.detect_url_normalization(mk_result([home]))
    assert f[0].check_id == "access.url_normalization" and f[0].tier == Tier.INFORMATIONAL


# 18. duplicate variants
def test_duplicate_variants_likely():
    home = mk_page("https://s.com/", internal_links=["https://s.com/a", "https://s.com/a/"])
    a = mk_page("https://s.com/a", canonical="")
    f = detectors.detect_duplicate_variants(mk_result([home, a]))
    assert f[0].check_id == "access.duplicate_url_variants"


def test_duplicate_variants_suppressed_by_canonical():
    home = mk_page("https://s.com/", internal_links=["https://s.com/a", "https://s.com/a/"])
    a = mk_page("https://s.com/a", canonical="https://s.com/a")
    assert detectors.detect_duplicate_variants(mk_result([home, a])) == []


# 19. pagination
def test_pagination_issue_likely():
    p1 = mk_page("https://s.com/list", rel_next="https://s.com/list?page=2")
    p2 = mk_page("https://s.com/list?page=2", noindex=True)
    f = detectors.detect_pagination(mk_result([p1, p2]))
    assert f[0].check_id == "access.pagination_issue"


# 20. crawl budget
def test_crawl_budget_risk_likely():
    links = [f"https://s.com/p?sid=s{i}&a=1&b=2&c=3" for i in range(12)]
    home = mk_page("https://s.com/", internal_links=links)
    f = detectors.detect_crawl_budget(mk_result([home]))
    assert f[0].check_id == "access.crawl_budget_risk"


# tier -> observation mapping
def test_tier_to_observation_mapping():
    from detectors import CrawlFinding
    confirmed = CrawlFinding("access.x", Tier.CONFIRMED, "t", "e").to_observation()
    likely = CrawlFinding("access.x", Tier.LIKELY, "t", "e").to_observation()
    info = CrawlFinding("access.x", Tier.INFORMATIONAL, "t", "e").to_observation()
    inconclusive = CrawlFinding("access.x", Tier.INCONCLUSIVE, "t", "e").to_observation()
    assert confirmed.status == Status.FAIL and confirmed.confidence == Confidence.HIGH
    assert likely.status == Status.FAIL and likely.confidence == Confidence.MEDIUM
    assert info.status == Status.PASS and info.confidence == Confidence.LOW
    assert inconclusive.status == Status.INCONCLUSIVE


def test_clean_result_yields_no_findings():
    pages = [
        mk_page("https://s.com/", canonical="https://s.com/", internal_links=["https://s.com/a"], depth=0),
        mk_page("https://s.com/a", canonical="https://s.com/a", depth=1, inbound_internal=1),
    ]
    r = mk_result(pages, robots=RobotsInfo(available=True, raw="User-agent: *\nAllow: /"),
                  sitemaps=[SitemapInfo(url="u", available=True, well_formed=True, kind="urlset",
                                        urls=["https://s.com/", "https://s.com/a"], url_count=2)])
    assert detectors.run_all(r, Ctx(["GPTBot"])) == []
