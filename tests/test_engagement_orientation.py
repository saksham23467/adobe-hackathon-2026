"""Tests for the engagement-orientation skill (visitor-journey audit).

Covers the journey model (journey.py), each tiered detector (engagedetect.py),
the clean-site zero-findings invariant, the broken fixture, and the entrypoint.
"""

import importlib.util
import os

import _engagement_paths as P  # noqa: F401  (sets sys.path)
import engagedetect as E
import journey as J
from audit_common.acquire import Cache, FixtureAcquirer, PageRecord
from audit_common.schema import Confidence, Status


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def load(fixture):
    acq = FixtureAcquirer(os.path.join(P.FIXTURES, fixture))
    return [acq.fetch(e) for e in acq.pages()]


def rec(url, html, page_class="other"):
    return PageRecord(url=url, page_class=page_class, raw_html=html,
                      rendered_html=html, raw_text="", rendered_text="", fetched=True)


def fail_ids(records):
    return {o.check_id for o in E.run_all(records) if o.status == Status.FAIL}


def by_id(records):
    out = {}
    for o in E.run_all(records):
        out.setdefault(o.check_id, []).append(o)
    return out


# --------------------------------------------------------------------------
# Journey model (parsing)
# --------------------------------------------------------------------------
def test_parse_nav_headings_viewport_and_cta():
    html = ("<head><meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<title>Acme</title></head><body><header><nav aria-label='Primary'>"
            "<a href='/products'>Products</a></nav></header>"
            "<main><h1>Durable field tools</h1>"
            "<a href='/products'>Get started</a></main></body>")
    pv = J.parse_page("https://acme.example/", html, "home")
    assert pv.title == "Acme"
    assert pv.has_visible_h1() and "Durable field tools" in pv.visible_h1s
    assert pv.has_nav and not pv.has_breadcrumb
    assert pv.viewport and not pv.viewport_blocks_zoom
    assert pv.has_primary_cta()  # "Get started"
    assert "/products" in pv.link_paths()


def test_breadcrumb_links_excluded_from_primary_nav_naming():
    html = ("<header><nav aria-label='Primary'><a href='/p/widget'>Products</a></nav></header>"
            "<main><nav aria-label='Breadcrumb'><a href='/'>Home</a> / "
            "<a href='/p/widget'>Widget</a></nav><h1>Widget</h1></main>")
    pv = J.parse_page("https://acme.example/p/widget", html, "product")
    labels = {lab for path, lab in pv.nav_dest_labels() if path == "/p/widget"}
    # Primary nav label only; breadcrumb "Widget" is not a naming conflict source.
    assert labels == {"Products"}
    assert pv.has_breadcrumb


def test_hidden_h1_detected():
    html = "<main><h1 hidden>Secret value prop</h1><p>text</p></main>"
    pv = J.parse_page("https://x.example/", html, "home")
    assert not pv.has_visible_h1()
    assert pv.hidden_h1_only()


def test_a11y_homepage_h1_is_not_hidden_orientation():
    html = "<main><h1 hidden>Adobe homepage</h1><p>Welcome to the product.</p></main>"
    ids = fail_ids([rec("https://x.example/", html, "home")])
    assert "engagement.content_hidden_interaction" not in ids


def test_real_hidden_value_prop_is_still_flagged():
    html = "<main><h1 hidden>Secret value prop</h1><p>text</p></main>"
    ids = fail_ids([rec("https://x.example/", html, "home")])
    assert "engagement.content_hidden_interaction" in ids


def test_sibling_host_viewport_is_not_the_audited_site():
    home = rec(
        "https://www.brand.example/",
        "<head><meta name='viewport' content='width=device-width'></head>"
        "<body><header><nav aria-label='Primary'><a href='/'>Home</a></nav></header>"
        "<main><h1>Tools</h1><a href='/p'>Get started</a></main></body>",
        "home",
    )
    sibling = rec(
        "https://shop.brand.example/",
        "<head><meta name='viewport' content='width=device-width, user-scalable=no'>"
        "</head><body><h1>Shop</h1></body>",
        "other",
    )
    assert "engagement.no_viewport_meta" not in fail_ids([home, sibling])


def test_internal_vs_external_and_contact_affordance():
    html = ("<a href='/about'>About</a><a href='https://other.example/x'>Ext</a>"
            "<a href='mailto:hi@x.example'>Email</a>")
    pv = J.parse_page("https://x.example/", html, "home")
    paths = pv.link_paths()
    assert "/about" in paths and "/x" not in paths
    assert pv.contact_affordance()


def test_viewport_blocks_zoom():
    html = "<meta name='viewport' content='width=device-width, user-scalable=no'>"
    pv = J.parse_page("https://x.example/", html, "home")
    assert pv.viewport_blocks_zoom


# --------------------------------------------------------------------------
# Detectors (crafted, minimal inputs)
# --------------------------------------------------------------------------
def test_blocking_interstitial():
    html = ("<div role='dialog' aria-modal='true' style='position:fixed'>"
            "<h2>Sign up to continue</h2></div><h1>Home</h1>")
    assert "engagement.blocking_interstitial" in fail_ids([rec("https://x/", html, "home")])


def test_consent_banner_is_not_blocking():
    html = ("<div role='dialog' aria-modal='true' style='position:fixed'>"
            "<p>We use cookies to improve your experience.</p></div>"
            "<h1>Home</h1><a href='/p'>Get started</a>")
    assert "engagement.blocking_interstitial" not in fail_ids([rec("https://x/", html, "home")])


def test_no_orientation_when_no_h1_and_no_cta():
    html = "<main><p>just some text</p></main>"
    assert "engagement.no_orientation" in fail_ids([rec("https://x/", html, "home")])


def test_orientation_ok_with_h1_only():
    html = "<main><h1>We make durable tools</h1><p>text</p></main>"
    assert "engagement.no_orientation" not in fail_ids([rec("https://x/", html, "home")])


def test_no_ia_navigation():
    html = "<main><h1>Home</h1><a href='/p'>Buy now</a></main>"
    ids = fail_ids([rec("https://x/", html, "home")])
    assert "engagement.no_ia_navigation" in ids


def test_content_hidden_interaction():
    html = "<main><h1 hidden>What we do</h1><nav><a href='/a'>A</a></nav></main>"
    assert "engagement.content_hidden_interaction" in fail_ids([rec("https://x/", html, "home")])


def test_unclear_navigation_empty_and_vague():
    html = ("<nav><a href='/gallery'><img src='i.png'></a>"
            "<a href='#'>click here</a><a href='/a'>About</a></nav><h1>Home</h1>")
    obs = by_id([rec("https://x/", html, "home")])
    tiers = {o.confidence for o in obs.get("engagement.unclear_navigation", [])}
    assert Confidence.HIGH in tiers  # empty link
    assert Confidence.MEDIUM in tiers  # vague "click here"


def test_inconsistent_naming_confirmed():
    a = "<nav aria-label='Primary'><a href='/plans'>Pricing</a></nav><h1>A</h1>"
    b = "<nav aria-label='Primary'><a href='/plans'>Plans</a></nav><h1>B</h1>"
    ids = fail_ids([rec("https://x/", a, "home"), rec("https://x/b", b, "about")])
    assert "engagement.inconsistent_naming" in ids


def test_consistent_naming_not_flagged():
    a = "<nav aria-label='Primary'><a href='/plans'>Plans</a></nav><h1>A</h1>"
    b = "<nav aria-label='Primary'><a href='/plans'>Plans</a></nav><h1>B</h1>"
    ids = fail_ids([rec("https://x/", a, "home"), rec("https://x/b", b, "about")])
    assert "engagement.inconsistent_naming" not in ids


def test_a11y_window_suffix_is_same_nav_label():
    a = "<nav aria-label='Primary'><a href='/help'>Shopping Help</a></nav><h1>A</h1>"
    b = ("<nav aria-label='Primary'><a href='/help'>Shopping Help (opens in new window)</a>"
         "</nav><h1>B</h1>")
    ids = fail_ids([rec("https://x/", a, "home"), rec("https://x/b", b, "about")])
    assert "engagement.inconsistent_naming" not in ids


def test_calendar_file_link_is_not_unclear_nav():
    html = ("<nav><a href='/v/events/event.ics'></a>"
            "<a href='/about'>About</a></nav><h1>Home</h1>")
    ids = fail_ids([rec("https://x/", html, "home")])
    assert "engagement.unclear_navigation" not in ids


def test_dead_end_page():
    home = "<nav><a href='/t'>Terms</a></nav><h1>Home</h1><a href='/x'>Go</a>"
    terms = "<h1>Terms</h1><p>no way out</p>"
    ids = fail_ids([rec("https://x/", home, "home"), rec("https://x/t", terms, "other")])
    assert "engagement.dead_end_page" in ids


def test_key_info_undiscoverable_but_not_absence():
    # product page exists but home does not link it -> flagged
    home = "<nav><a href='/about'>About</a></nav><h1>Home</h1>"
    prod = "<nav><a href='/about'>About</a></nav><h1>Gadget</h1>"
    ids = fail_ids([rec("https://x/", home, "home"), rec("https://x/p", prod, "product")])
    assert "engagement.key_info_undiscoverable" in ids


def test_product_only_sample_skips_home_relative_checks():
    # A product-page sample has no real homepage. Home-relative checks must
    # not invent "not linked from home" / "no site IA" defects.
    a = "<h1>Photoshop</h1><p>Photo editor.</p>"
    b = "<h1>Illustrator</h1><p>Vector editor.</p>"
    ids = fail_ids([
        rec("https://x/products/photoshop.html", a, "product"),
        rec("https://x/products/illustrator.html", b, "product"),
    ])
    assert "engagement.key_info_undiscoverable" not in ids
    assert "engagement.no_ia_navigation" not in ids
    assert "engagement.no_orientation" not in ids
    assert "engagement.content_hidden_interaction" not in ids


def test_key_info_reachable_not_flagged():
    home = "<nav><a href='/p'>Products</a></nav><h1>Home</h1>"
    prod = "<nav><a href='/p'>Products</a></nav><h1>Gadget</h1>"
    ids = fail_ids([rec("https://x/", home, "home"), rec("https://x/p", prod, "product")])
    assert "engagement.key_info_undiscoverable" not in ids


def test_absence_of_contact_page_is_not_flagged():
    # No contact-class page exists at all -> must NOT be flagged (no penalising absence)
    home = "<nav><a href='/p'>Products</a></nav><h1>Home</h1>"
    prod = "<nav><a href='/p'>Products</a></nav><h1>Gadget</h1>"
    ids = fail_ids([rec("https://x/", home, "home"), rec("https://x/p", prod, "product")])
    assert all("contact" not in (o) for o in ids if o == "engagement.key_info_undiscoverable") or True
    # explicit: the only undiscoverable finding (if any) is not about contact
    for o in E.run_all([rec("https://x/", home, "home"), rec("https://x/p", prod, "product")]):
        if o.check_id == "engagement.key_info_undiscoverable":
            assert o.metric.get("page_class") != "contact"


def test_missing_viewport_flagged():
    html = "<h1>Home</h1><nav><a href='/a'>A</a></nav>"
    assert "engagement.no_viewport_meta" in fail_ids([rec("https://x/", html, "home")])


def test_present_viewport_not_flagged():
    html = ("<meta name='viewport' content='width=device-width, initial-scale=1'>"
            "<h1>Home</h1><nav><a href='/a'>A</a></nav>")
    assert "engagement.no_viewport_meta" not in fail_ids([rec("https://x/", html, "home")])


def test_unexplained_jargon_recommendation():
    a = ("<meta name='viewport' content='width=device-width'>"
         "<nav aria-label='Primary'><a href='/z'>ZQX</a></nav><h1>Home</h1>")
    obs = by_id([rec("https://x/", a, "home")])
    j = obs.get("engagement.unexplained_jargon", [])
    assert j and j[0].confidence == Confidence.LOW


def test_known_acronym_not_flagged():
    a = ("<meta name='viewport' content='width=device-width'>"
         "<nav aria-label='Primary'><a href='/api'>API</a></nav><h1>Home</h1>")
    assert "engagement.unexplained_jargon" not in fail_ids([rec("https://x/", a, "home")])


# --------------------------------------------------------------------------
# Integration: clean vs broken fixtures
# --------------------------------------------------------------------------
def test_clean_site_zero_engagement_findings():
    ids = fail_ids(load("clean-brand"))
    assert ids == set(), "clean site must yield no engagement findings; got %s" % ids


def test_broken_fixture_triggers_expected_checks():
    ids = fail_ids(load("engagement-broken"))
    for expected in (
        "engagement.blocking_interstitial",
        "engagement.unclear_navigation",
        "engagement.inconsistent_naming",
        "engagement.dead_end_page",
        "engagement.key_info_undiscoverable",
        "engagement.no_viewport_meta",
    ):
        assert expected in ids, "missing %s (got %s)" % (expected, ids)


def test_broken_fixture_findings_carry_evidence_and_tier():
    for o in E.run_all(load("engagement-broken")):
        assert o.evidence_sentence
        assert o.metric.get("tier") in {"confirmed_structural", "likely_ux", "recommendation"}
        assert o.metric.get("stage") in {"arrival", "orientation", "understanding",
                                         "evaluation", "next_action"}
        assert o.urls


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------
def _load_entry():
    path = os.path.join(P.ENGAGEMENT_SCRIPTS, "detect.py")
    spec = importlib.util.spec_from_file_location("engagement_detect_entry", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Ctx:
    def __init__(self, records):
        self.cache = Cache()
        for r in records:
            self.cache.put(r)


def test_entrypoint_over_cache():
    mod = _load_entry()
    ctx = _Ctx(load("engagement-broken"))
    obs = mod.detect(ctx)
    assert any(o.check_id == "engagement.blocking_interstitial" for o in obs)


def test_entrypoint_clean_zero():
    mod = _load_entry()
    ctx = _Ctx(load("clean-brand"))
    obs = [o for o in mod.detect(ctx) if o.status == Status.FAIL]
    assert obs == []
