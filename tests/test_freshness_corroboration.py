import datetime as dt
import importlib.util
import os

import _fresh_paths as P  # noqa: F401  (sets sys.path)
import claims as C
import corroborate
import freshdetect
from audit_common.acquire import Cache, FixtureAcquirer, PageRecord
from audit_common.schema import Status

NOW = dt.date(2026, 8, 29)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def rec(url, html, page_class="other"):
    return PageRecord(url=url, page_class=page_class, raw_html=html, rendered_html=html, fetched=True)


def fail_ids(records, corr=None):
    obs = freshdetect.run_all(records, corr=corr, now=NOW)
    return {o.check_id for o in obs if o.status == Status.FAIL}


class MockCorr:
    def __init__(self, result):
        self.result = result

    def lookup(self, name):
        return self.result


def R(**kw):
    return corroborate.CorroborationResult(**kw)


def load(fixture):
    acq = FixtureAcquirer(os.path.join(P.FIXTURES, fixture))
    return [acq.fetch(e) for e in acq.pages()]


# --------------------------------------------------------------------------
# Claim / date extraction
# --------------------------------------------------------------------------
def test_extract_dates_distinguishes_copyright():
    html = ("<script type='application/ld+json'>{\"@type\":\"Article\","
            "\"dateModified\":\"2016-03-01\"}</script><footer>© 2026 Acme</footer>")
    dates = C.extract_dates(html)
    kinds = {d.kind for d in dates}
    assert "dateModified" in kinds and "copyright" in kinds
    assert len(C.content_dates(dates)) == 1  # copyright excluded


def test_extract_claims_founding_leadership_superlative():
    html = ("<p>Founded in 2011. Our CEO is Alice Smith.</p>"
            "<p>We are the #1 provider in the world.</p>"
            "<p>Voted best bakery by City Times 2025.</p>")
    sc = C.extract_site_claims([rec("https://s/", html)])
    assert "2011" in sc.founding_years
    assert sc.leadership.get("ceo") == "Alice Smith"
    strong = [c for c in sc.claims if c.category == "marketing_superlative" and c.strength == "strong"]
    supported = [c for c in sc.claims if c.category == "marketing_superlative" and c.strength == "normal"]
    assert any("#1" in c.value or "number one" in c.value for c in strong)
    assert supported  # the "voted best ... 2025" claim is treated as supported


# --------------------------------------------------------------------------
# The six required scenarios
# --------------------------------------------------------------------------
def test_strongly_corroborated_brand_no_findings():
    html = ("<script type='application/ld+json'>{\"@type\":\"Organization\",\"name\":\"Acme\","
            "\"sameAs\":[\"https://www.wikidata.org/wiki/Q1\"]}</script>"
            "<p>Founded in 2010, Acme makes durable tools.</p>")
    corr = MockCorr(R(matched=True, confidence="high", label="Acme", candidate_count=1,
                      prominent_candidates=1, claims={"founding_year": "2010"},
                      source_url="https://www.wikidata.org/wiki/Q1"))
    assert fail_ids([rec("https://acme.example/", html, "home")], corr) == set()


def test_contradictory_information():
    home = rec("https://s/", "<p>Founded in 2010. We build tools.</p>", "home")
    about = rec("https://s/about", "<p>Established in 2012 by our team.</p>", "other")
    assert "freshness.self_contradiction" in fail_ids([home, about])


def test_stale_page():
    html = ("<script type='application/ld+json'>{\"@type\":\"Article\",\"headline\":\"H\","
            "\"dateModified\":\"2016-03-01\"}</script><p>Guide.</p>")
    assert "freshness.content_stale" in fail_ids([rec("https://s/post", html, "article")])


def test_new_company_not_penalized():
    # Recent copyright only, no old content dates, no superlatives, no external match.
    html = "<p>Sprout Labs launched in 2025 to help gardeners.</p><footer>© 2026 Sprout Labs</footer>"
    corr = MockCorr(R(matched=False, candidate_count=0))
    assert fail_ids([rec("https://sprout.example/", html, "home")], corr) == set()
    # And with no backend at all it is still not penalized.
    assert fail_ids([rec("https://sprout.example/", html, "home")], None) == set()


def test_ambiguous_company_name():
    html = ("<script type='application/ld+json'>{\"@type\":\"Organization\",\"name\":\"Apollo\"}</script>"
            "<p>Apollo helps teams ship software.</p>")
    corr = MockCorr(R(matched=True, confidence="low", label="Apollo", candidate_count=6,
                      prominent_candidates=3, claims={}, source_url="https://www.wikidata.org/wiki/Q9"))
    assert "identity.entity_ambiguous" in fail_ids([rec("https://apollo.example/", html, "home")], corr)


def test_unsupported_marketing_claim():
    html = "<p>We are the world's best and #1 provider, unmatched by anyone.</p>"
    assert "freshness.unsupported_claim" in fail_ids([rec("https://s/", html, "home")])


def test_supported_claim_not_flagged():
    html = "<p>Voted best bakery by City Times in 2025.</p>"
    assert "freshness.unsupported_claim" not in fail_ids([rec("https://s/", html, "home")])


# --------------------------------------------------------------------------
# External conservatism
# --------------------------------------------------------------------------
def test_external_conflict_flagged():
    html = ("<script type='application/ld+json'>{\"@type\":\"Organization\",\"name\":\"Acme\"}</script>"
            "<p>Founded in 2010. Acme is great.</p>")
    corr = MockCorr(R(matched=True, confidence="high", label="Acme", candidate_count=1,
                      prominent_candidates=1, claims={"founding_year": "1999"},
                      source_url="https://www.wikidata.org/wiki/Q1"))
    ids = fail_ids([rec("https://acme.example/", html, "home")], corr)
    assert "freshness.claim_conflict" in ids


def test_absence_is_never_a_finding():
    html = "<p>Founded in 2010. Acme is great.</p>"
    corr = MockCorr(R(matched=False, candidate_count=0))
    assert fail_ids([rec("https://acme.example/", html, "home")], corr) == set()


def test_no_backend_is_inconclusive_not_finding():
    obs = freshdetect.run_all([rec("https://s/", "<p>Founded in 2010.</p>", "home")], corr=None, now=NOW)
    ext = [o for o in obs if o.check_id == "freshness.claim_conflict"]
    assert ext and ext[0].status == Status.INCONCLUSIVE


def test_findings_carry_classification():
    obs = freshdetect.run_all(
        [rec("https://s/", "<script type='application/ld+json'>{\"@type\":\"Article\",\"headline\":\"H\","
                           "\"dateModified\":\"2016-03-01\"}</script>", "article")],
        corr=None, now=NOW)
    stale = [o for o in obs if o.check_id == "freshness.content_stale" and o.status == Status.FAIL][0]
    assert stale.metric.get("classification") == "stale-risk"
    assert stale.metric.get("mechanism") and stale.metric.get("fix")


# --------------------------------------------------------------------------
# FixtureCorroborator + entrypoint
# --------------------------------------------------------------------------
def test_fixture_corroborator_reads_json():
    corr = corroborate.FixtureCorroborator(
        os.path.join(P.FIXTURES, "fresh-corroborated", "corroboration.json"))
    res = corr.lookup("Northwind Tools")
    assert res.matched and res.claims.get("founding_year") == "2015"


def _load_entry():
    path = os.path.join(P.FRESH_SCRIPTS, "detect.py")
    spec = importlib.util.spec_from_file_location("fresh_detect_entry", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ctx(fixture):
    cache = Cache()
    for r in load(fixture):
        cache.put(r)

    class Ctx:
        pass

    ctx = Ctx()
    ctx.cache = cache
    ctx.config = {}
    ctx.fixture_dir = os.path.join(P.FIXTURES, fixture)
    return ctx


def test_entrypoint_corroborated_zero_findings():
    entry = _load_entry()
    obs = entry.detect(_ctx("fresh-corroborated"))
    assert [o for o in obs if o.status == Status.FAIL] == []


def test_entrypoint_ambiguous_flags_entity():
    entry = _load_entry()
    obs = entry.detect(_ctx("fresh-ambiguous"))
    ids = {o.check_id for o in obs if o.status == Status.FAIL}
    assert "identity.entity_ambiguous" in ids
