import importlib.util as _ilu
import os

import _render_paths as P  # noqa: F401  (sets sys.path)
import renderdetect
import textextract
from audit_common.acquire import Cache, FixtureAcquirer, PageRecord
from audit_common.schema import Status

# Load the render-extract entrypoint under a unique module name. Several skills
# ship a `detect.py`; a bare `import detect` is order-dependent once other skills'
# scripts dirs are on sys.path (e.g. after the orchestrator runs), so bind it
# explicitly to this skill's file.
_spec = _ilu.spec_from_file_location(
    "render_extract_entry", os.path.join(P.RENDER_SCRIPTS, "detect.py")
)
render_detect = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(render_detect)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def load_suite():
    acq = FixtureAcquirer(os.path.join(P.FIXTURES, "render-suite"))
    return [acq.fetch(e) for e in acq.pages()]


def fail_ids_by_url(observations):
    """Map url -> set of check_ids that are actual FAIL findings (not PASS)."""
    out = {}
    for obs in observations:
        if obs.status != Status.FAIL:
            continue
        for u in obs.urls:
            out.setdefault(u, set()).add(obs.check_id)
    return out


def rec(url, raw, rendered, page_class="other"):
    return PageRecord(url=url, page_class=page_class, raw_html=raw, rendered_html=rendered, fetched=True)


# --------------------------------------------------------------------------
# textextract unit tests
# --------------------------------------------------------------------------
def test_extract_separates_visible_from_hidden():
    html = ("<main><p>Visible here.</p>"
            "<div aria-hidden='true'><p>Hidden secret text.</p></div>"
            "<div style='display:none'><p>Also hidden.</p></div></main>")
    e = textextract.extract(html)
    assert "Visible here." in e.text_visible
    assert "Hidden secret text." not in e.text_visible
    assert "Hidden secret text." in e.text_all
    assert e.hidden_tokens >= 3


def test_extract_headings_jsonld_scripts_images():
    html = ("<h1>Title A</h1><h2>Sub</h2>"
            "<script type='application/ld+json'>{\"@type\":\"Product\",\"name\":\"X\"}</script>"
            "<script src='/a.js'></script>"
            "<img src='/p.png' alt='pic' width='800' height='600'>"
            "<img src='/logo.png' alt='' width='40' height='40'>")
    e = textextract.extract(html)
    assert [h[1] for h in e.headings] == ["Title A", "Sub"]
    assert e.jsonld_count == 1 and "Product" in e.jsonld_types
    assert e.script_count == 2 and e.script_src_count == 1
    assert len(e.meaningful_images()) == 1  # logo excluded as icon


# --------------------------------------------------------------------------
# The five required scenarios (integration over the fixture)
# --------------------------------------------------------------------------
def test_five_site_scenarios():
    records = load_suite()
    obs = renderdetect.run_all(records)
    by_url = fail_ids_by_url(obs)

    static = "https://render.example/static"
    ssr = "https://render.example/react-ssr"
    article = "https://render.example/js-article"
    shell = "https://render.example/empty-shell"
    image = "https://render.example/image-heavy"

    # 1. static HTML site -> no render findings
    assert static not in by_url, by_url.get(static)
    # 2. normal React (SSR) site -> JS is not a defect
    assert ssr not in by_url, by_url.get(ssr)
    # 3. JS-rendered article -> primary content only after JS
    assert "render.primary_content_js_only" in by_url.get(article, set())
    # 4. empty shell -> empty initial shell (and not double-counted as primary gap)
    assert by_url.get(shell) == {"render.empty_initial_shell"}
    # 5. image-heavy site -> meaningful text represented as images
    assert "render.text_as_image" in by_url.get(image, set())


def test_scenario_findings_carry_full_evidence():
    records = load_suite()
    obs = renderdetect.run_all(records)
    primary = [o for o in obs if o.check_id == "render.primary_content_js_only"][0]
    page = primary.metric["pages"][0]
    for key in ("url", "initial_html_tokens", "rendered_visible_tokens", "text_gap_ratio", "tokens_added_by_js"):
        assert key in page
    kinds = {a["type"] for a in primary.artifacts}
    assert {"initial_html", "rendered_text"} <= kinds
    assert "mechanism" in primary.metric


# --------------------------------------------------------------------------
# Per-detector unit tests
# --------------------------------------------------------------------------
def test_headings_js_only():
    body = "<p>" + ("word " * 60) + "</p>"
    raw = f"<main>{body}</main>"
    rendered = f"<main><h1>Injected Heading</h1>{body}</main>"
    obs = renderdetect.run_all([rec("https://s/x", raw, rendered)])
    ids = {o.check_id for o in obs if o.status == Status.FAIL}
    assert ids == {"render.headings_js_only"}


def test_content_behind_interaction():
    hidden = "<div aria-hidden='true'><p>" + ("secret " * 60) + "</p></div>"
    rendered = f"<main><p>Short intro only.</p>{hidden}</main>"
    obs = renderdetect.run_all([rec("https://s/y", rendered, rendered)])
    ids = {o.check_id for o in obs if o.status == Status.FAIL}
    assert "render.content_behind_interaction" in ids


def test_schema_injected_post_hydration():
    body = "<h1>Product</h1><p>" + ("desc " * 50) + "</p>"
    raw = f"<main>{body}</main>"
    rendered = (f"<main>{body}</main>"
                "<script type='application/ld+json'>{\"@type\":\"Product\"}</script>")
    obs = renderdetect.run_all([rec("https://s/z", raw, rendered)])
    ids = {o.check_id for o in obs if o.status == Status.FAIL}
    assert "render.schema_injected_post_hydration" in ids


def test_excessive_scripts_is_informational_not_a_finding():
    scripts = "".join(f"<script src='/s{i}.js'></script>" for i in range(15))
    body = "<h1>Present</h1><p>" + ("content " * 60) + "</p>"
    html = f"<main>{body}</main>{scripts}"
    obs = renderdetect.run_all([rec("https://s/w", html, html)])
    fails = [o for o in obs if o.status == Status.FAIL]
    infos = [o for o in obs if o.check_id == "render.excessive_script_dependence"]
    assert fails == []
    assert infos and infos[0].status == Status.PASS


def test_canvas_content():
    html = "<main><canvas width='800' height='600'></canvas><p>ok</p></main>"
    obs = renderdetect.run_all([rec("https://s/c", html, html)])
    ids = {o.check_id for o in obs if o.status == Status.FAIL}
    assert "render.canvas_content" in ids


def test_no_rendered_dom_is_inconclusive():
    cache = Cache()
    cache.put(PageRecord(url="https://s/only-raw", raw_html="<h1>Hi</h1>", rendered_html="", fetched=True))

    class Ctx:
        pass

    ctx = Ctx()
    ctx.cache = cache
    obs = render_detect.detect(ctx)
    assert len(obs) == 1 and obs[0].status == Status.INCONCLUSIVE
