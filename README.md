# brand-ai-readiness-audit

A read-only Agent Skill Marketplace. Given a public URL, it measures why an AI system (or a first-time visitor) cannot **discover, extract, attribute, or act on** a brand, then emits one JSON audit report: findings with evidence strings and ranked `suggested_action`s.

It does not modify the target site. It does not call an LLM. Detectors are deterministic Python. Live fetches use **curl** (stdlib urllib only if curl is missing). Playwright and Selenium are not used.

The two halves of the problem are kept separate in scoring and in skills:

- **Discoverability** — can a machine be admitted, reach representative pages, and read quotable facts?
- **Engagement** — once a page is fetched, can a visitor orient and take a next step?

`marketplace.json` names `audit-orchestrator` as the sole entrypoint.

---

## Architecture

```
                    https://example.com
                             |
                             v
                 +-----------------------+
                 |  audit-orchestrator   |   validate URL, SafetyGate, budget
                 +-----------+-----------+
                             |
              1. crawl-access (owns the network crawl)
                             |
                             v
                    CrawlResult + sample plan
                             |
              2. render-extract (curl HTML; no browser)
                             |
                             v
              shared Cache  (curl-fetched HTML, once)
                     /    |    |    \
                    /     |    |     \
                   v      v    v      v
         fact-     structured  engage-  freshness-
         access.   identity    ment     corroboration*
                             |
                             v
              compile  ->  reconcile  ->  recommendation-engine
                             |
                             v
                    one schema-valid JSON report

  * freshness is fail-soft and skipped if the time/network budget is gone.
  Cache-consuming skills do not re-fetch. recommendation-engine does not detect.
```

---

## Why multiple skills

A single detector that mixed robots, JSON-LD, JS gaps, and UX would produce overlapping, unranked “issues” and hide which **mechanism** failed.

Each skill answers one question with its own evidence type:

| Skill | Question | Evidence type |
|---|---|---|
| `crawl-access` | Can a crawler be let in and reach important URLs? | HTTP status, robots, sitemap, redirects, canonicals |
| `render-extract` | Does JS hide the text a no-JS fetcher would store? | raw HTML vs rendered DOM token/heading gap |
| `fact-accessibility` | Can a concrete fact be extracted and quoted? | visible text, images, JSON-LD prices/hours |
| `structured-identity` | Is the brand entity machine-attributable? | JSON-LD / microdata / RDFa graph |
| `engagement-orientation` | Where does the visitor journey break? | headings, nav, CTAs, viewport, overlays |
| `freshness-corroboration` | Are dated/strong claims stale or self-contradictory? | on-page dates/claims; optional 1 external lookup |
| `recommendation-engine` | What should change, where, and in what order? | compiled findings (no new observations) |
| `audit-orchestrator` | Compose, cap, reconcile, validate, emit. | catalog + report schema |

Separation is not organizational. It is data-dependency and failure-mode isolation:

- Access defects (blocked UA, `Disallow: /`) make later content findings untrustworthy; they gate recommendations.
- Render gaps explain missing facts without calling every SPA a defect.
- Fact extractability is independent of schema presence (schema absence is not a finding in `structured-identity`).
- Identity conflicts are graph problems, not “missing H1” problems.
- Engagement is DOM-journey, not crawl status.
- Freshness is the only skill allowed to leave the origin, and only under a hard external-call cap.
- Recommendations are a transform. Mixing them into detectors would couple severity to “what to do first.”

---

## What each skill detects

**`crawl-access`** — robots.txt availability and directives (including AI user-agents and `Disallow: /`), sitemap presence/validity, homepage reachability, 5xx, redirect loops/chains, confirmed broken internal HTML links (404/410/5xx; 403 is access-blocked, not automatically broken), noindex on important pages, orphans, crawl traps, duplicate URL variants, pagination, crawl-budget risk. Also builds the page-class sample the rest of the audit uses.

**`render-extract`** — empty initial app shells, text locked in images/canvas, and hidden DOM text, measured from the curl-fetched HTML. A server-rendered page whose text is already in that HTML is not a defect. No headless browser.

**`fact-accessibility`** — vague pricing/hours/location with no concrete value; facts only in images; schema price ≠ visible price; product-page price with no Product/Offer attribution; fact sections hidden from the accessibility tree; homepage with no quotable identity sentence; informational images missing alt.

**`structured-identity`** — malformed JSON-LD; unrecognized `@type`; missing required properties; internal schema contradiction; schema value absent from visible text; duplicate primary Organization entities; similar brand names; Product unlinked from Organization; malformed `sameAs`; entity `url` host on a different registrable site. A site with no schema and clear text produces zero findings here.

**`engagement-orientation`** — blocking interstitials (consent banners excluded), no H1 and no primary CTA, no IA navigation, orientation content hidden, empty/vague links, inconsistent primary-nav labels for the same destination, dead-end pages, key page classes unreachable from a real homepage, missing/locking viewport.

**`freshness-corroboration`** — stale dated content, on-site contradictions (founding year / leadership / operating status), unsupported superlatives, and — only if a backend and budget exist — a conflict or name-collision against one independent lookup. No external coverage is not a finding.

**`recommendation-engine`** — does not detect. Attaches a mechanism-specific action to each compiled finding and, when a concern has no live defect, a small set of page-class-gated proactive items.

---

## How the orchestrator composes them

`skills/audit-orchestrator/scripts/run_audit.py`:

1. Validate the target (`http`/`https`, public host). Construct `SafetyGate` from `config/settings.json`.
2. Run detection skills in order. Each `detect(ctx)` returns `Observation`s. A skill exception or exhausted budget is recorded in `limitations`; the run continues.
3. **Compile** (`scripts/compile.py`) — drop unknown `check_id`s; only `fail` becomes a `Finding`; `inconclusive` → `limitations`.
4. **Reconcile** (`scripts/reconcile.py`) — merge overlapping checks on intersecting URLs; raise confidence one step when independent concerns agree; mark contradictions `contested` and lower confidence. Does not invent certainty.
5. **Recommend** — `recommendation-engine` attaches `suggested_action` and optional `beyond_defect_recommendations`.
6. **Present** (`scripts/presentation.py`) — scores, headline, `top_priorities`, string `evidence` (handout floor). Validate against `config/report_schema.json`.

Partial runs still emit a valid report.

---

## How evidence is collected

One crawl, one cache.

- `crawl-access` fetches with GET (HEAD where used), honors robots for our UA, records status, redirect hops, headers, canonical, robots stanza, sitemap URLs.
- Sampled pages are stored as `PageRecord`: URL, status, raw HTML, optional rendered HTML, extracted text, page class.
- Later skills read that cache only. They do not open new connections to the origin (freshness may make at most one bounded external lookup).
- Each `Observation` carries `check_id`, `status`, `confidence`, `urls`, `method`, a metric payload, and an `evidence_sentence`. After presentation, the report field `evidence` is a **string**; structured extras sit on `evidence_detail`.

There is no LLM paraphrase of evidence. Sentences are assembled from measured fields (counts, statuses, snippets, graph names).

---

## Severity and confidence

Detectors emit a **tier**, not a free-form severity:

| Detector tier | Observation status | Confidence |
|---|---|---|
| confirmed | `fail` | high |
| likely | `fail` | medium |
| informational | `pass` (not a finding) | low |
| check could not run | `inconclusive` (limitation) | — |

Catalog `default_severity` is applied at compile time, then **capped by confidence**:

| Confidence | Maximum severity |
|---|---|
| high | critical |
| medium | high |
| low | medium |

Semantic relevance can cap further (e.g. a low-relevance observation cannot be high-severity). Reconciliation may raise confidence one level on independent corroboration, or lower it and set `contested=true` on contradiction. Severity is recomputed after that, still under the cap.

Priority of the *action* is a separate score. A medium-severity, site-wide, low-effort unblock can outrank a high-severity, high-effort, single-page rewrite.

---

## How recommendations are generated

`scripts/templates.py` maps each catalog `check_id` to a mechanism-specific template: what, where, why (the detected mechanism), how, effort, expected effect.

```
priority_score = 100 × impact × severity_w × confidence_w × scope × effort_blend × dep_mult
```

Buckets: ≥65 critical, ≥42 high, else medium. Access blockers (bot/index/home unreachable) and render blockers (empty shell / JS-only primary content) are prerequisites; dependents list them and are down-weighted until resolved.

`do_first` is true when the finding is `critical`, or when there are no unresolved dependencies, effort is low/medium, and `priority_score ≥ 42`. Duplicate fixes (same `check_id`) collapse to one recommendation.

Beyond-defect items are emitted only if that concern currently has no defect and the item matches sampled page classes. They are not generic SEO filler.

---

## How false positives are controlled

- **Closed taxonomy** — unknown `check_id`s are dropped.
- **Fail-only findings** — absence of a best practice is not a defect unless a detector proves a concrete mechanism (e.g. a vague phrase *and* no concrete price).
- **Informational ≠ finding** — missing robots.txt, heavy-but-SSR JavaScript, etc.
- **Semantic gating** — assets, HTML fragments, calendar/download hrefs, 403s, unfetched sitemap URLs are not treated as broken pages or empty nav.
- **No hostname rules** — no `adobe.com` / `apple.com` exceptions. Guards are path-shape and content-context (locale `/{cc}` as home; cashback/EMI not a product price; sibling registrable hosts not a canonical clash; “Acme” vs “Acme Support” is a qualified variant).
- **Reconcile** — overlapping skills on the same URLs become one finding; disagreement is `contested`, not a second defect.
- **Generalization tests** — `tests/adversarial/` measures precision/recall on synthetic sites; clean fixtures must stay silent.

---

## robots.txt and safety

Enforced by `lib/audit_common/safety.py` and the crawler, not by convention.

- Methods: GET and HEAD only. `SafetyGate.assert_readonly` rejects anything else.
- Schemes: `http` / `https`. Private/link-local/loopback hosts are blocked.
- `respect_robots: true` (default). `urllib.robotparser` is applied to **our** user-agent: `brand-ai-readiness-audit/1.0 (+read-only audit bot)`. Disallowed URLs are not fetched and are not recorded as 404s.
- No `Authorization` / `Cookie` headers. No login, no writes, no form posts.
- Per-host delay (`min_delay_s`, default 0.5s). Byte cap per response (3 MB).
- Freshness is the only off-origin caller and is capped (`max_external_calls`, default 8; typically one entity lookup).

A robots 5xx is treated as fail-closed for *our* fetches (limitation / access signal), not invented content.

---

## Runtime constraints

Defaults in `config/settings.json` (wall-clock target under 5 minutes):

| Cap | Default |
|---|---|
| Total budget | 270 s |
| Crawl deadline | 120 s |
| Pages fetched | 40 |
| Sample analyzed | ≤ 30 (≤ 3 per page class) |
| Rendered pages | 8 |
| Crawl depth | 5 |
| Redirect hops | 5 |
| Concurrency | 4 |
| Request timeout | 10 s |
| External calls | 8 |
| Bytes / page | 3 000 000 |

If the budget expires mid-pipeline, remaining skills are skipped, listed in `limitations`, and a valid report is still written. Render checks use the curl HTML; they do not start a browser.

---

## How to run an audit

Python 3 and `curl` on PATH. No pip packages required. No Playwright or Selenium.

```bash
# live (respects robots, writes JSON to stdout)
python3 skills/audit-orchestrator/scripts/run_audit.py https://example.com

# live, write a file
python3 skills/audit-orchestrator/scripts/run_audit.py https://example.com --out report.json

# hermetic (no network) — uses saved HTML under tests/fixtures/
python3 skills/audit-orchestrator/scripts/run_audit.py --fixture tests/fixtures/clean-brand

# optional settings override
python3 skills/audit-orchestrator/scripts/run_audit.py https://example.com --config config/settings.json
```

Report floor (Adobe handout): `site`, `audited_at`, `summary.{total_findings,critical,high,medium}`, and `findings[]` each with `id`, `title`, `severity`, **`evidence` (string)**, `suggested_action.{summary,priority}`. Additional fields are extensions.

---

## How to run tests

```bash
python3 tests/run_tests.py
```

Stdlib runner. Covers skill detectors, compile/reconcile/recommend, marketplace/`SKILL.md` compliance, adversarial precision/recall, and hermetic pipeline smoke. No network.

---

## How to package the submission

The marketplace root **is** this directory (`marketplace.json` must be at the zip root).

```bash
python3 scripts/package_submission.py
# optional: python3 scripts/package_submission.py --out /tmp/brand-ai-readiness-audit.zip
```

The script includes `marketplace.json`, `README.md`, `requirements.txt`, `skills/`, `lib/`, `config/` (not `_tmp*`), `tests/`, and `scripts/`. It excludes `runs/`, `.git/`, `__pycache__/`, `.venv/`, `.DS_Store`, and local experiment configs. It refuses to write an archive ≥ 50 MB.

`entrypoint` in `marketplace.json` is `audit-orchestrator`. There is exactly one entrypoint skill.

---

## License

MIT. Declared on each `SKILL.md`.
