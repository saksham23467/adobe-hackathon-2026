import json
import os

import _crawl_paths as P  # noqa: F401  (sets sys.path)
import detectors
import urlnorm
from crawler import Crawler, CrawlLimits, DictFetcher, FixtureFetcher


def _ai_ctx():
    with open(os.path.join(P.ROOT, "config", "ai_user_agents.json"), "r", encoding="utf-8") as fh:
        agents = json.load(fh)

    class _Ctx:
        ai_agents = agents

    return _Ctx()


HOME = (
    "<html><head><link rel='canonical' href='https://s.com/'></head>"
    "<body><a href='/p2'>two</a> <a href='/missing'>missing</a></body></html>"
)
P2 = "<html><body><a href='/'>home</a></body></html>"


def _dict_site():
    return DictFetcher({
        "https://s.com/robots.txt": {"status": 200, "body": "User-agent: *\nAllow: /\n", "content_type": "text/plain"},
        "https://s.com/": {"status": 200, "body": HOME, "content_type": "text/html"},
        "https://s.com/p2": {"status": 200, "body": P2, "content_type": "text/html"},
        "https://s.com/missing": {"status": 404, "body": "nope", "content_type": "text/html"},
    })


def test_bfs_crawls_dedups_and_records_depth():
    result = Crawler(_dict_site(), "https://s.com/", limits=CrawlLimits(max_pages=20)).run()
    assert result.robots.available is True
    home = result.get("https://s.com/")
    p2 = result.get("https://s.com/p2")
    missing = result.get("https://s.com/missing")
    assert home.depth == 0
    assert p2.depth == 1
    assert "https://s.com/p2" in home.internal_links
    assert missing.status_code == 404 and missing.fetched and not missing.ok
    assert result.stats.pages_crawled >= 3


def test_crawler_skips_nondocument_urls_but_keeps_them_as_links():
    class CountingFetcher(DictFetcher):
        def __init__(self, mapping):
            super().__init__(mapping)
            self.fetched = []

        def fetch(self, url, count_page=True, accept=None):
            self.fetched.append(url)
            return super().fetch(url, count_page=count_page, accept=accept)

    home = (
        "<html><body>"
        "<a href='/about'>About</a>"
        "<a href='/logo.svg'>Logo</a>"
        "<a href='/demo.mp4'>Watch</a>"
        "<a href='/app.js'>js</a>"
        "</body></html>"
    )
    mapping = {
        "https://s.com/robots.txt": {"status": 200, "body": "User-agent: *\nAllow: /\n",
                                     "content_type": "text/plain"},
        "https://s.com/": {"status": 200, "body": home, "content_type": "text/html"},
        "https://s.com/about": {"status": 200, "body": "<html><body>About</body></html>",
                                "content_type": "text/html"},
        "https://s.com/logo.svg": {"status": 200, "body": "<svg></svg>",
                                   "content_type": "image/svg+xml"},
        "https://s.com/demo.mp4": {"status": 200, "body": "MP4", "content_type": "video/mp4"},
        "https://s.com/app.js": {"status": 200, "body": "console.log(1)",
                                 "content_type": "application/javascript"},
    }
    fetcher = CountingFetcher(mapping)
    result = Crawler(fetcher, "https://s.com/", limits=CrawlLimits(max_pages=20)).run()
    home_page = result.get("https://s.com/")
    assert home_page is not None
    assert "https://s.com/logo.svg" in home_page.internal_links
    assert "https://s.com/demo.mp4" in home_page.internal_links
    fetched = " ".join(fetcher.fetched)
    assert "logo.svg" not in fetched
    assert "demo.mp4" not in fetched
    assert "app.js" not in fetched
    assert result.get("https://s.com/about") is not None
    assert result.get("https://s.com/logo.svg") is None
    assert result.stats.urls_skipped_nondocument >= 3


def test_broken_link_detected_from_dict_site():
    result = Crawler(_dict_site(), "https://s.com/", limits=CrawlLimits()).run()
    ids = {f.check_id for f in detectors.run_all(result, None)}
    assert "access.broken_internal_links" in ids


def test_max_pages_cap_enforced():
    # A page linking to many others, but cap at 2 pages fetched.
    links = " ".join(f"<a href='/p{i}'>{i}</a>" for i in range(10))
    mapping = {"https://s.com/": {"status": 200, "body": f"<html><body>{links}</body></html>", "content_type": "text/html"}}
    for i in range(10):
        mapping[f"https://s.com/p{i}"] = {"status": 200, "body": "<html><body>x</body></html>", "content_type": "text/html"}
    result = Crawler(DictFetcher(mapping), "https://s.com/", limits=CrawlLimits(max_pages=2)).run()
    assert result.stats.pages_crawled <= 2
    assert result.stats.page_cap_hit is True


def test_max_depth_limits_enqueue():
    mapping = {
        "https://s.com/": {"status": 200, "body": "<a href='/a'>a</a>", "content_type": "text/html"},
        "https://s.com/a": {"status": 200, "body": "<a href='/b'>b</a>", "content_type": "text/html"},
        "https://s.com/b": {"status": 200, "body": "<a href='/c'>c</a>", "content_type": "text/html"},
        "https://s.com/c": {"status": 200, "body": "end", "content_type": "text/html"},
    }
    result = Crawler(DictFetcher(mapping), "https://s.com/", limits=CrawlLimits(max_depth=1)).run()
    # Depth 0 (home) and depth 1 (/a) fetched; /b at depth 2 must not be enqueued.
    assert result.get("https://s.com/a") is not None
    assert result.get("https://s.com/b") is None


def test_robots_disallow_is_respected():
    mapping = {
        "https://s.com/robots.txt": {"status": 200, "body": "User-agent: *\nDisallow: /private/\n", "content_type": "text/plain"},
        "https://s.com/": {"status": 200, "body": "<a href='/private/x'>x</a> <a href='/public'>ok</a>", "content_type": "text/html"},
        "https://s.com/private/x": {"status": 200, "body": "secret", "content_type": "text/html"},
        "https://s.com/public": {"status": 200, "body": "ok", "content_type": "text/html"},
    }
    result = Crawler(DictFetcher(mapping), "https://s.com/", limits=CrawlLimits()).run()
    private = result.get("https://s.com/private/x")
    assert private is not None and private.error == "robots_disallow" and not private.fetched
    assert result.get("https://s.com/public").fetched is True


def test_long_redirect_chain_flagged():
    hops = [
        ("https://s.com/old", "https://s.com/r1", 301),
        ("https://s.com/r1", "https://s.com/r2", 301),
        ("https://s.com/r2", "https://s.com/r3", 301),
        ("https://s.com/r3", "https://s.com/new", 301),
    ]
    mapping = {
        "https://s.com/": {"status": 200, "body": "<a href='/old'>o</a>", "content_type": "text/html"},
        "https://s.com/old": {"status": 200, "body": "landed", "content_type": "text/html",
                              "final_url": "https://s.com/new", "redirect_chain": hops},
    }
    result = Crawler(DictFetcher(mapping), "https://s.com/", limits=CrawlLimits()).run()
    ids = {f.check_id for f in detectors.run_all(result, None)}
    assert "access.redirect_chain_long" in ids


# ---- End-to-end against fixtures ---------------------------------------
def test_clean_fixture_crawls_and_has_zero_findings():
    fixture = os.path.join(P.FIXTURES, "clean-brand")
    result = Crawler(FixtureFetcher(fixture), "https://cleanbrand.example/", limits=CrawlLimits()).run()
    assert len(result.html_pages()) == 5
    assert result.robots.available is True
    assert len(result.sitemaps[0].urls) == 5
    findings = detectors.run_all(result, None)
    assert findings == [], [f.check_id for f in findings]


def test_broken_fixture_surfaces_expected_defects():
    fixture = os.path.join(P.FIXTURES, "broken-brand")
    result = Crawler(FixtureFetcher(fixture), "https://broken.example/", limits=CrawlLimits()).run()
    ids = {f.check_id for f in detectors.run_all(result, _ai_ctx())}
    for expected in (
        "access.ai_ua_blocked",
        "access.broken_internal_links",
        "access.important_noindex",
        "access.orphan_pages",
    ):
        assert expected in ids, (expected, ids)
