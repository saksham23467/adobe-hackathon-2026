import importlib.util
import os

import _identity_paths as P  # noqa: F401  (sets sys.path)
import entitygraph as eg
import identitydetect
from audit_common.acquire import Cache, FixtureAcquirer, PageRecord
from audit_common.schema import Status


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def load(fixture):
    acq = FixtureAcquirer(os.path.join(P.FIXTURES, fixture))
    return [acq.fetch(e) for e in acq.pages()]


def rec(url, html, page_class="other", visible=None):
    return PageRecord(url=url, page_class=page_class, raw_html=html, rendered_html=html,
                      raw_text=visible or "", rendered_text=visible or "", fetched=True)


def fail_ids(records):
    return {o.check_id for o in identitydetect.run_all(records) if o.status == Status.FAIL}


# --------------------------------------------------------------------------
# Entity graph parsing (JSON-LD / microdata / RDFa)
# --------------------------------------------------------------------------
def test_jsonld_graph_and_links():
    html = ("<script type='application/ld+json'>"
            "{\"@type\":\"Product\",\"name\":\"W\",\"brand\":{\"@type\":\"Brand\",\"name\":\"Acme\"}}"
            "</script>")
    g = eg.build_graph([rec("https://s/p", html, "product")])
    assert g.products() and g.products()[0].name() == "W"
    assert g.brands() and g.brands()[0].name() == "Acme"
    assert "acme" in g.references(g.products()[0])


def test_microdata_parse():
    html = ("<div itemscope itemtype='https://schema.org/Product'>"
            "<span itemprop='name'>MD Widget</span></div>")
    nodes = eg.parse_microdata_rdfa(html, "https://s/md")
    assert any(n.is_a("product") and n.name() == "MD Widget" for n in nodes)


def test_rdfa_parse():
    html = "<div typeof='Organization'><span property='name'>RDFa Co</span></div>"
    nodes = eg.parse_microdata_rdfa(html, "https://s/rdfa")
    assert any(n.is_a("organization") and n.name() == "RDFa Co" for n in nodes)


def test_name_similarity():
    assert eg.names_equivalent("NimbusPay", "Nimbus Pay")
    assert eg.names_equivalent("Acme", "Acme Inc")
    assert not eg.names_similar("NimbusPay", "Nimbus Pay")  # same entity, not a clash
    assert eg.names_similar("NimbusPay", "NimbusBay")
    assert not eg.names_similar("Acme Robotics", "Globex Media")


# --------------------------------------------------------------------------
# Required scenarios
# --------------------------------------------------------------------------
def test_excellent_structured_data_zero_findings():
    assert fail_ids(load("schema-excellent")) == set()


def test_no_schema_but_clear_content_is_not_flagged():
    html = ("<h1>Cascade Coffee Roasters</h1>"
            "<p>We roast small-batch coffee in Portland and ship nationwide. Founded 2011. "
            "Order online or visit our cafe on Main Street, open daily from 7am.</p>")
    assert fail_ids([rec("https://cascade.example/", html, "home", visible=html)]) == set()


def test_malformed_jsonld():
    html = "<script type='application/ld+json'>{ \"@type\": \"Product\", \"name\": \"X\", }</script>"
    assert "identity.jsonld_malformed" in fail_ids([rec("https://s/m", html)])


def test_type_inappropriate_typo():
    html = "<script type='application/ld+json'>{\"@type\":\"Organisation\",\"name\":\"Acme\"}</script>"
    assert "identity.type_inappropriate" in fail_ids([rec("https://s/t", html)])


def test_schema_invalid_missing_required():
    html = ("<script type='application/ld+json'>"
            "{\"@type\":\"Product\",\"offers\":{\"@type\":\"Offer\",\"price\":\"10\"}}</script>")
    ids = fail_ids([rec("https://s/i", html, "product")])
    assert "identity.schema_invalid" in ids  # Product missing name + Offer missing priceCurrency


def test_contradiction_dates():
    html = ("<script type='application/ld+json'>{\"@type\":\"Article\",\"headline\":\"H\","
            "\"datePublished\":\"2026-02-10\",\"dateModified\":\"2026-01-01\"}</script>")
    assert "identity.schema_contradiction" in fail_ids([rec("https://s/a", html, "article")])


def test_stale_visible_schema_mismatch():
    html = ("<script type='application/ld+json'>{\"@type\":\"Product\",\"name\":\"Atlas Wrench\","
            "\"brand\":{\"@type\":\"Brand\",\"name\":\"Atlas Tools\"},"
            "\"offers\":{\"@type\":\"Offer\",\"price\":\"149.00\",\"priceCurrency\":\"USD\"}}</script>")
    visible = "Titan Wrench is our flagship tool and it now costs $199.00 after the latest update."
    assert "identity.schema_visible_mismatch" in fail_ids([rec("https://s/v", html, "product", visible=visible)])


def test_multiple_brands_similar_names():
    html = ("<script type='application/ld+json'>{\"@type\":\"Organization\",\"name\":\"NimbusPay\","
            "\"url\":\"https://s/\"}</script>"
            "<script type='application/ld+json'>{\"@type\":\"Organization\",\"name\":\"NimbusBay\","
            "\"url\":\"https://s/\"}</script>")
    ids = fail_ids([rec("https://s/", html, "home")])
    assert "identity.entity_ambiguous" in ids


def test_duplicate_conflicting_entities():
    html = ("<script type='application/ld+json'>{\"@type\":\"Organization\",\"name\":\"Acme Robotics\","
            "\"url\":\"https://s/\"}</script>"
            "<script type='application/ld+json'>{\"@type\":\"Organization\",\"name\":\"Globex Media\","
            "\"url\":\"https://s/\"}</script>")
    assert "identity.duplicate_entity" in fail_ids([rec("https://s/", html, "home")])


def test_entity_unlinked():
    html = ("<script type='application/ld+json'>{\"@type\":\"Organization\",\"name\":\"Acme\","
            "\"url\":\"https://s/\"}</script>"
            "<script type='application/ld+json'>{\"@type\":\"Product\",\"name\":\"Widget\"}</script>")
    assert "identity.entity_unlinked" in fail_ids([rec("https://s/", html, "product")])


def test_sameas_broken():
    html = ("<script type='application/ld+json'>{\"@type\":\"Organization\",\"name\":\"Acme\","
            "\"sameAs\":[\"not a url\",\"https://x.com/acme\"]}</script>")
    assert "identity.sameas_broken" in fail_ids([rec("https://s/", html, "home")])


def test_canonical_entity_mismatch():
    html = ("<script type='application/ld+json'>{\"@type\":\"Organization\",\"name\":\"Acme\","
            "\"url\":\"https://other-domain.com/\"}</script>")
    assert "identity.canonical_entity_mismatch" in fail_ids([rec("https://example.com/", html, "home")])


def test_findings_carry_full_evidence():
    html = "<script type='application/ld+json'>{ \"@type\": \"Product\", }</script>"
    obs = [o for o in identitydetect.run_all([rec("https://s/m", html)]) if o.status == Status.FAIL]
    assert obs
    for o in obs:
        for key in ("affected_entity", "detected_mismatch", "expected_relationship", "mechanism", "fix"):
            assert o.metric.get(key), (o.check_id, key)
        assert o.evidence_sentence.strip()
        assert o.artifacts


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------
def _load_entry():
    path = os.path.join(P.IDENTITY_SCRIPTS, "detect.py")
    spec = importlib.util.spec_from_file_location("identity_detect_entry", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_entrypoint_over_cache():
    entry = _load_entry()
    cache = Cache()
    for r in load("schema-excellent"):
        cache.put(r)

    class Ctx:
        pass

    ctx = Ctx()
    ctx.cache = cache
    assert [o for o in entry.detect(ctx) if o.status == Status.FAIL] == []


def test_entrypoint_empty_cache_is_safe():
    entry = _load_entry()

    class Ctx:
        pass

    ctx = Ctx()
    ctx.cache = Cache()
    assert entry.detect(ctx) == []
