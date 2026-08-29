# Access check catalog (crawl-access)

Each check maps to a `check_id` in `config/check_catalog.json` and is emitted by a
detector in `scripts/detectors.py`. Every detector is a pure function over a
`CrawlResult` and returns findings in one of three tiers:

- **confirmed** → `Status.FAIL`, `confidence=high` (finding, full severity)
- **likely** → `Status.FAIL`, `confidence=medium` (finding, capped severity)
- **informational** → `Status.PASS` (recorded, never a finding)

The mapping from handout requirement to check_id:

| # | Requirement | check_id | Tier | Predicate | FP guard |
|---|---|---|---|---|---|
| 1 | robots.txt availability | `access.robots_unreachable` | likely | robots.txt returns 5xx/timeout | 404 (no file) is allowed, not a defect |
| 1 | robots.txt availability | `access.robots_missing` | info | no robots.txt served (404) | absence is permitted → informational |
| 2 | robots.txt directives | `access.global_disallow_all` | confirmed | `User-agent: *` + `Disallow: /` | must be the wildcard group |
| 2 | robots.txt directives | `access.ai_ua_blocked` | confirmed | an AI UA disallowed at root while a generic bot is allowed | our own UA being blocked is only informational |
| 2 | robots.txt directives | `access.our_ua_blocked` | info | our audit UA is disallowed | never inferred as "AI blocked" |
| 3 | sitemap discovery | `access.sitemap_missing` | info / likely | no sitemap found (info); robots references a sitemap that 404s (likely) | plain absence never a defect |
| 4 | sitemap validity | `access.sitemap_invalid` | confirmed | sitemap served but not well-formed XML / wrong root | only when a sitemap is actually present |
| 5,13 | canonical URLs / conflicts | `access.canonical_to_error` | confirmed | canonical target returns 4xx/5xx | target must have been fetched |
| 5,13 | canonical URLs / conflicts | `access.canonical_inconsistent` | likely | canonical cross-host or canonical chain | cross-host may be intentional syndication |
| 6 | HTTP status codes | `access.homepage_unreachable` | confirmed | entry URL returns ≥400 | only the entry point |
| 6 | HTTP status codes | `access.server_errors` | confirmed | any crawled page returns 5xx | 4xx handled as broken links |
| 7 | redirect chains | `access.redirect_loop` | confirmed | a redirect cycle or over-limit chain | requires a real fetched response |
| 7 | redirect chains | `access.redirect_chain_long` | likely | ≥4 redirect hops | a single redirect is fine |
| 8,14 | internal reachability / broken links | `access.broken_internal_links` | confirmed | a linked internal URL returns 4xx/5xx when fetched | unfetched (budget-skipped) links are never "broken" |
| 9 | orphan-like pages | `access.orphan_pages` | likely | in sitemap, reachable only via sitemap, zero internal inbound links | important pages reported separately; homepage excluded |
| 10 | depth from homepage | `access.excessive_depth` | likely | ≥3 pages deeper than 4 clicks | requires several pages to avoid noise |
| 11,12 | noindex / X-Robots-Tag | `access.global_noindex` | confirmed | every crawled HTML page is noindex | needs ≥2 pages all noindex |
| 11,12 | noindex / X-Robots-Tag | `access.important_noindex` | confirmed | homepage or a key section is noindex | ordinary single-page noindex is ignored |
| 15 | important page discoverability | `access.important_page_unreachable` | likely | an important page (about/contact/pricing…) exists in sitemap but has no internal links | only for known-important patterns |
| 16 | crawl traps | `access.crawl_trap` | likely | repeating path segments or session parameters in internal URLs | needs repeating segments or ≥3 URLs |
| 17 | URL normalization | `access.url_normalization` | info | internal links mix uppercase host / index files / tracking params | reported as informational, not a defect |
| 18 | duplicate URL variants | `access.duplicate_url_variants` | likely | one page linked under ≥2 URL forms with no unifying canonical | suppressed when a canonical consolidates them |
| 19 | pagination discoverability | `access.pagination_issue` | likely | `rel=next` points to an errored or noindex page | absence of rel=next is not a defect |
| 20 | crawl budget protection | `access.crawl_budget_risk` | likely | large share of internal links carry many params / session ids | needs ≥10 heavy links and ≥30% ratio |

## Normalized CrawlResult
`scripts/crawl_result.py` defines `CrawlResult` (pages, robots, sitemaps, stats),
`CrawlPage`, `RobotsInfo`, and `SitemapInfo`. All are JSON-serializable via
`to_dict()`.

## Safety guarantees
See `SKILL.md`. The crawler is GET-only, robots-respecting, deduplicated, and
capped by pages / depth / redirects / wall-clock deadline.

## AI user-agents
See `references/ai_user_agents.md` and `config/ai_user_agents.json`.

## Notes
- `llms.txt` / `ai.txt` presence is beyond-defect (owned by the recommendation
  engine), never a defect finding here.
