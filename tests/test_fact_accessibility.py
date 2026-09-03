import importlib.util
import os

import _fact_paths as P  # noqa: F401  (sets sys.path)
import factextract as fx
import factdetect
from audit_common.acquire import Cache, FixtureAcquirer, PageRecord
from audit_common.schema import Confidence, Status


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def load(fixture):
    acq = FixtureAcquirer(os.path.join(P.FIXTURES, fixture))
    return [acq.fetch(e) for e in acq.pages()]


def fail_ids_by_url(observations):
    out = {}
    for obs in observations:
        if obs.status != Status.FAIL:
            continue
        for u in obs.urls:
            out.setdefault(u, set()).add(obs.check_id)
    return out


def rec(url, html, page_class="other"):
    return PageRecord(url=url, page_class=page_class, raw_html=html, rendered_html=html, fetched=True)


# --------------------------------------------------------------------------
# Extractor unit tests
# --------------------------------------------------------------------------
def test_sections_track_hidden():
    html = ("<h2>FAQ</h2><div aria-hidden='true'><p>Answer body text here now.</p></div>"
            "<h2>About</h2><p>Visible about text.</p>")
    doc = fx.extract_doc(html)
    faq = [s for s in doc.sections if s.heading == "FAQ"][0]
    about = [s for s in doc.sections if s.heading == "About"][0]
    assert faq.hidden is True
    assert about.hidden is False
    assert faq.category() == "faq"


def test_jsonld_offer_prices_and_org_description():
    html = ("<script type='application/ld+json'>"
            "{\"@type\":\"Organization\",\"description\":\"We build tools.\"}</script>"
            "<script type='application/ld+json'>"
            "{\"@type\":\"Product\",\"offers\":{\"@type\":\"Offer\",\"price\":\"149.00\"}}</script>")
    doc = fx.extract_doc(html)
    assert doc.offer_prices() == {149.0}
    assert doc.org_description() == "We build tools."
    assert doc.has_type("Product")


def test_concrete_vs_vague_matchers():
    assert fx.find_prices("It costs $149.00 today") == {149.0}
    assert fx.find_prices("weighs 200 Nm") == set()
    assert fx.has_concrete_hours("Open Mon-Fri 9:00am-5:00pm")
    assert not fx.has_concrete_hours("We keep flexible hours")
    assert fx.has_concrete_address("1200 Blake Street, Denver 80202")
    assert fx.vague_hit("Contact us for pricing today", "pricing") == "contact us for pricing"


# --------------------------------------------------------------------------
# Contrast fixtures: clear == 0 findings, ambiguous == specific findings
# --------------------------------------------------------------------------
def test_clear_facts_yield_no_findings():
    obs = factdetect.run_all(load("facts-clear"))
    fails = [o for o in obs if o.status == Status.FAIL]
    assert fails == [], [(o.check_id, o.urls) for o in fails]


def test_ambiguous_facts_scenarios():
    obs = factdetect.run_all(load("facts-ambiguous"))
    by_url = fail_ids_by_url(obs)
    base = "https://vaguefacts.example"

    assert "fact.no_quotable_value_prop" in by_url.get(f"{base}/", set())
    assert "fact.ambiguous_value" in by_url.get(f"{base}/pricing", set())
    assert "fact.facts_locked_in_non_text" in by_url.get(f"{base}/plans", set())
    assert "fact.contradictory_values" in by_url.get(f"{base}/products/widget", set())
    assert "fact.not_attributed_to_entity" in by_url.get(f"{base}/products/gadget", set())
    assert "fact.behind_interaction" in by_url.get(f"{base}/faq", set())
    assert "fact.ambiguous_value" in by_url.get(f"{base}/hours", set())


def test_findings_carry_evidence_and_confidence():
    obs = factdetect.run_all(load("facts-ambiguous"))
    fails = [o for o in obs if o.status == Status.FAIL]
    assert fails
    for o in fails:
        assert o.evidence_sentence.strip()
        assert "mechanism" in o.metric and o.metric["mechanism"].strip()
        assert isinstance(o.metric.get("confidence_score"), float)
        assert o.confidence in (Confidence.HIGH, Confidence.MEDIUM)
        assert o.artifacts, o.check_id


# --------------------------------------------------------------------------
# Per-detector unit tests (deliberately clear / ambiguous inputs)
# --------------------------------------------------------------------------
def test_ambiguous_pricing_detector():
    html = "<h1>Pricing</h1><p>Get a quote from our friendly sales team.</p>"
    ids = {o.check_id for o in factdetect.run_all([rec("https://s/p", html)]) if o.status == Status.FAIL}
    assert ids == {"fact.ambiguous_value"}


def test_no_false_positive_when_price_is_concrete():
    html = "<h1>Pricing</h1><p>Plans start at $19 per month. Get a quote for enterprise.</p>"
    ids = {o.check_id for o in factdetect.run_all([rec("https://s/p2", html)]) if o.status == Status.FAIL}
    assert "fact.ambiguous_value" not in ids


def test_facts_locked_in_image_detector():
    html = "<h2>Pricing</h2><img src='/img/price.png' alt='' width='800' height='600'>"
    ids = {o.check_id for o in factdetect.run_all([rec("https://s/l", html)]) if o.status == Status.FAIL}
    assert "fact.facts_locked_in_non_text" in ids


def test_contradictory_price_detector():
    html = ("<script type='application/ld+json'>{\"@type\":\"Product\","
            "\"offers\":{\"@type\":\"Offer\",\"price\":\"149.00\"}}</script>"
            "<h1>Widget</h1><p>Price: $199.00</p>")
    ids = {o.check_id for o in factdetect.run_all([rec("https://s/c", html, "product")]) if o.status == Status.FAIL}
    assert "fact.contradictory_values" in ids


def test_not_attributed_price_detector():
    html = "<h1>Gadget</h1><p>Price: $59.00</p><button>Add to cart</button>"
    ids = {o.check_id for o in factdetect.run_all([rec("https://s/n", html, "product")]) if o.status == Status.FAIL}
    assert "fact.not_attributed_to_entity" in ids


def test_whenever_marketing_is_not_opening_hours():
    html = ("<h1>Create</h1><p>Create whenever you're inspired. "
            "Join whenever works for you and start a project.</p>")
    ids = {o.check_id for o in factdetect.run_all([rec("https://s/", html, "home")]) if o.status == Status.FAIL}
    assert "fact.ambiguous_value" not in ids


def test_promo_cashback_on_homepage_is_not_product_price():
    html = ("<h1>Shop</h1><p>Get ₹10001 cashback with no cost EMI. "
            "Minimum transaction ₹10000. Buy now.</p>")
    ids = {o.check_id for o in factdetect.run_all([rec("https://s/", html, "home")]) if o.status == Status.FAIL}
    assert "fact.not_attributed_to_entity" not in ids


def test_fragment_include_images_are_not_alt_findings():
    imgs = "".join(f"<img src='/i{i}.png' width='800' height='600'>" for i in range(4))
    html = f"<div>{imgs}</div>"
    ids = {o.check_id for o in factdetect.run_all(
        [rec("https://s/fragments/promo", html)]
    ) if o.status == Status.FAIL}
    assert "fact.informational_images_no_alt" not in ids


def test_behind_interaction_detector():
    hidden = "<div style='display:none'><p>" + ("policy detail " * 20) + "</p></div>"
    html = f"<h2>Returns policy</h2>{hidden}"
    ids = {o.check_id for o in factdetect.run_all([rec("https://s/b", html)]) if o.status == Status.FAIL}
    assert "fact.behind_interaction" in ids


def test_identity_not_textual_detector():
    html = ("<h1>Dream bigger today</h1><p>Bold moments, endless momentum, and a crowd that keeps "
            "pushing every single hour across the whole wide world.</p>")
    obs = factdetect.run_all([rec("https://s/", html, "home")])
    ids = {o.check_id for o in obs if o.status == Status.FAIL}
    assert "fact.no_quotable_value_prop" in ids


def test_identity_present_when_description_exists():
    html = "<h1>Home</h1><p>Acme provides durable field tools for engineers worldwide every day.</p>"
    obs = factdetect.run_all([rec("https://s/", html, "home")])
    ids = {o.check_id for o in obs if o.status == Status.FAIL}
    assert "fact.no_quotable_value_prop" not in ids


def test_images_no_alt_detector():
    imgs = "".join(f"<img src='/photo{i}.jpg' width='800' height='600'>" for i in range(4))
    html = f"<h1>Gallery</h1>{imgs}"
    ids = {o.check_id for o in factdetect.run_all([rec("https://s/g", html)]) if o.status == Status.FAIL}
    assert "fact.informational_images_no_alt" in ids


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------
def _load_entry():
    path = os.path.join(P.FACT_SCRIPTS, "detect.py")
    spec = importlib.util.spec_from_file_location("fact_detect_entry", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_entrypoint_runs_over_cache():
    entry = _load_entry()
    cache = Cache()
    for r in load("facts-ambiguous"):
        cache.put(r)

    class Ctx:
        pass

    ctx = Ctx()
    ctx.cache = cache
    obs = entry.detect(ctx)
    assert any(o.status == Status.FAIL for o in obs)


def test_entrypoint_empty_cache_is_safe():
    entry = _load_entry()

    class Ctx:
        pass

    ctx = Ctx()
    ctx.cache = Cache()
    assert entry.detect(ctx) == []
