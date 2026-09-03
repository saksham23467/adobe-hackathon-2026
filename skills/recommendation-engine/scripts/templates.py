"""Mechanism-specific fix templates, keyed by check_id.

Each template answers the reusable half of the eight recommendation questions:

  what   -> 1. what exactly should change
  where  -> 2. where should it change
  why    -> 3. why it solves the *detected mechanism* (not generic SEO advice)
  how    -> 4. concrete implementation steps
  patch  -> optional copy-pasteable snippet
  impact -> 6. normalized expected-impact weight (0..1)
  effort -> 7. implementation difficulty (low | medium | high)
  effect -> plain-language expected impact

Questions 5 (evidence), 6 (dependencies) and 8 (sequencing) are filled at
runtime from the finding and the prioritization model.

Templates are deliberately mechanism-specific. There is intentionally no generic
"improve SEO" / "add structured data" filler.
"""

from __future__ import annotations

from typing import Any, Dict


def T(what, where, why, how, effort, impact, effect, patch=None) -> Dict[str, Any]:
    return {
        "what": what, "where": where, "why": why, "how": how,
        "effort": effort, "impact": impact, "effect": effect, "patch": patch,
    }


# Default impact weight per concern (benefit to machine-readability / trust).
CONCERN_IMPACT = {
    "crawl_access": 0.95,
    "render": 0.85,
    "fact_access": 0.80,
    "identity": 0.80,
    "freshness": 0.60,
    "engagement": 0.55,
}

TEMPLATES: Dict[str, Dict[str, Any]] = {
    # ---------------------------------------------------------------- access
    "access.ai_ua_blocked": T(
        "Stop disallowing AI assistant crawlers for content paths in robots.txt.",
        "robots.txt at the site root.",
        "The detected mechanism is an explicit Disallow for AI user-agents, which "
        "prevents assistants from fetching pages at answer time — so the brand can "
        "never be retrieved or cited regardless of content quality.",
        ["Remove or narrow the Disallow rules for the blocked AI user-agents.",
         "Add explicit Allow: / rules for the assistants you want to permit.",
         "Keep crawl-delay if needed, but do not block content paths."],
        "low", 0.98,
        "Assistants can fetch and cite the brand's pages.",
        "User-agent: GPTBot\nAllow: /\n\nUser-agent: ClaudeBot\nAllow: /\n\nUser-agent: PerplexityBot\nAllow: /",
    ),
    "access.our_ua_blocked": T(
        "Allow the standard crawling user-agent used to gather brand facts.",
        "robots.txt user-agent rules.",
        "A blanket Disallow blocks the fetcher entirely, so no page content can be "
        "read for extraction.",
        ["Replace the global Disallow with path-specific rules.",
         "Explicitly Allow content directories."],
        "low", 0.95, "Content becomes fetchable for indexing and extraction.",
    ),
    "access.global_noindex": T(
        "Remove the site-wide noindex directive from content pages.",
        "The robots meta tag / X-Robots-Tag header emitted on all pages.",
        "A global noindex tells engines and assistants to discard every page, so "
        "nothing the site publishes can surface.",
        ["Delete the noindex meta/header on public content pages.",
         "Keep noindex only on genuinely private or duplicate URLs."],
        "low", 0.97, "Pages become eligible for indexing and retrieval.",
    ),
    "access.class_noindex": T(
        "Remove noindex from the affected page class.",
        "The template that renders the affected page class (e.g. product/article).",
        "An entire class of otherwise-valuable pages is being suppressed by a "
        "template-level noindex.",
        ["Locate the noindex in the shared template and remove it for public pages."],
        "low", 0.9, "The whole page class becomes retrievable.",
    ),
    "access.important_noindex": T(
        "Remove noindex from the important pages that carry it.",
        "The specific important URLs flagged (home/about/key landing pages).",
        "Key pages a machine needs for brand facts are explicitly excluded from "
        "indexing.",
        ["Remove the noindex directive from the flagged important pages."],
        "low", 0.92, "The brand's most important pages can be retrieved and cited.",
    ),
    "access.global_disallow_all": T(
        "Replace the `Disallow: /` blanket rule with path-specific rules.",
        "robots.txt default (User-agent: *) group.",
        "Disallow: / blocks all crawling, so the whole site is invisible to "
        "machines.",
        ["Remove Disallow: / from the default group.",
         "Add targeted Disallow rules only for private/admin paths."],
        "low", 0.97, "The site becomes crawlable again.",
        "User-agent: *\nDisallow: /admin/\nDisallow: /cart/\nAllow: /",
    ),
    "access.robots_missing": T(
        "Publish a robots.txt that declares crawl policy and the sitemap.",
        "robots.txt at the site root.",
        "No robots.txt means crawl guidance and sitemap discovery rely on guessing; "
        "an explicit file removes ambiguity.",
        ["Add a permissive robots.txt.",
         "Include a Sitemap: line pointing at the XML sitemap."],
        "low", 0.5, "Crawlers get explicit, unambiguous guidance.",
        "User-agent: *\nAllow: /\nSitemap: https://example.com/sitemap.xml",
    ),
    "access.robots_unreachable": T(
        "Fix the robots.txt response so it returns 200 with valid text.",
        "The server route / CDN rule serving /robots.txt.",
        "A robots.txt that errors or times out makes crawl policy indeterminate; "
        "some crawlers then back off entirely.",
        ["Ensure /robots.txt returns 200 text/plain.",
         "Remove any auth/redirect in front of it."],
        "low", 0.7, "Crawlers can reliably read crawl policy.",
    ),
    "access.sitemap_missing": T(
        "Publish an XML sitemap and reference it from robots.txt.",
        "sitemap.xml at the site root + a Sitemap line in robots.txt.",
        "Without a sitemap, discovery depends on link-following alone, so deep or "
        "poorly-linked pages may never be found.",
        ["Generate sitemap.xml listing canonical content URLs with lastmod.",
         "Add `Sitemap:` to robots.txt."],
        "low", 0.6, "All important pages become discoverable.",
    ),
    "access.sitemap_invalid": T(
        "Repair the malformed sitemap so entries parse.",
        "sitemap.xml (or the sitemap index).",
        "A sitemap that fails to parse yields zero usable URLs, defeating its "
        "discovery purpose.",
        ["Validate the XML; fix encoding/namespace/tag errors.",
         "Ensure <loc> values are absolute, canonical URLs."],
        "low", 0.6, "Listed URLs are discovered reliably.",
    ),
    "access.canonical_to_error": T(
        "Point canonicals at a live 200 URL.",
        "The rel=canonical tags on the affected pages.",
        "A canonical pointing at a 4xx/5xx or redirecting URL tells engines the "
        "authoritative version is broken, risking de-indexing of the real page.",
        ["Update canonicals to the final, 200-returning URL.",
         "Never canonicalize to a redirecting or error URL."],
        "medium", 0.75, "Engines keep the correct page indexed.",
    ),
    "access.canonical_inconsistent": T(
        "Make canonical URLs consistent and self-referential.",
        "rel=canonical across duplicate variants.",
        "Conflicting canonicals split ranking/identity signals across duplicates.",
        ["Choose one canonical host+path convention.",
         "Emit a self-referential canonical on each canonical page."],
        "medium", 0.6, "Ranking and entity signals consolidate on one URL.",
    ),
    "access.homepage_unreachable": T(
        "Restore a 200 response for the home page.",
        "The web server / origin for the entry URL.",
        "If the entry point errors or redirects into a loop, machines cannot even "
        "begin to crawl the site.",
        ["Fix the origin error or redirect loop for /.",
         "Verify the home page returns 200 with HTML."],
        "medium", 1.0, "The site becomes crawlable from its entry point.",
    ),
    "access.server_errors": T(
        "Eliminate 5xx responses on content pages.",
        "The origin/application for the flagged URLs.",
        "Server errors drop pages out of retrieval and waste crawl budget.",
        ["Reproduce and fix the 5xx on the flagged URLs.",
         "Add monitoring so regressions are caught."],
        "medium", 0.85, "Affected pages return and stay retrievable.",
    ),
    "access.redirect_loop": T(
        "Break the redirect loop so the URL resolves.",
        "The redirect rules (server config / CMS) for the flagged URL.",
        "A loop never terminates, so the destination content is never reached.",
        ["Trace the chain and remove the cyclic hop.",
         "Redirect straight to the final canonical URL."],
        "medium", 0.8, "The target content becomes reachable.",
    ),
    "access.redirect_chain_long": T(
        "Collapse multi-hop redirects into a single hop.",
        "Redirect rules for the flagged URLs.",
        "Long chains lose signals and crawl budget and can be abandoned mid-chain.",
        ["Rewrite chains A→B→C→D as A→D.",
         "Update internal links to point at the final URL."],
        "low", 0.5, "Faster, lossless resolution to the final page.",
    ),
    "access.broken_internal_links": T(
        "Fix or remove internal links that resolve to errors.",
        "The source pages containing the broken links.",
        "Broken internal links waste crawl budget and strand downstream pages.",
        ["Update the hrefs to valid URLs or remove them.",
         "Add redirects for moved targets."],
        "medium", 0.6, "Crawlers reach all intended pages; no dead ends.",
    ),
    "access.orphan_pages": T(
        "Add internal links so orphaned pages are reachable.",
        "Navigation, hubs, or related-content modules.",
        "Pages with no inbound internal links are effectively undiscoverable by "
        "link-following crawlers.",
        ["Link orphan pages from relevant hubs/nav.",
         "Include them in the sitemap."],
        "medium", 0.6, "Previously orphaned pages become discoverable.",
    ),
    "access.important_page_unreachable": T(
        "Make the flagged important pages reachable within a few clicks.",
        "Primary navigation and internal linking.",
        "Important pages buried beyond crawl reach may never be fetched.",
        ["Link important pages from the home page / primary nav.",
         "Ensure they appear in the sitemap."],
        "medium", 0.75, "Key pages are reliably discovered.",
    ),
    "access.excessive_depth": T(
        "Flatten the click-depth of deep content.",
        "Site information architecture / navigation.",
        "Content many clicks from the home page is crawled late or not at all.",
        ["Add hub pages or nav links to shorten paths to deep content."],
        "medium", 0.5, "Deep content is discovered sooner.",
    ),
    "access.crawl_trap": T(
        "Close the crawl trap generating near-infinite URLs.",
        "The parameterized/calendar/filter routes producing the trap.",
        "Traps exhaust crawl budget on worthless URL permutations, starving real "
        "pages.",
        ["Add Disallow rules or rel=nofollow for trap parameters.",
         "Set canonical to the parameter-free URL."],
        "medium", 0.65, "Crawl budget is spent on real content.",
    ),
    "access.url_normalization": T(
        "Serve one canonical URL form and 301 the rest.",
        "Server rewrite rules + canonical tags.",
        "Multiple URL forms for one page split signals and duplicate crawl effort.",
        ["Pick a single scheme/host/trailing-slash form.",
         "301 all other forms to it and self-canonicalize."],
        "medium", 0.55, "One authoritative URL per page.",
    ),
    "access.duplicate_url_variants": T(
        "De-duplicate URL variants that serve identical content.",
        "Canonical tags + redirect rules.",
        "Duplicate variants dilute ranking/entity signals and waste budget.",
        ["Canonicalize variants to one URL; 301 where possible."],
        "medium", 0.5, "Signals consolidate on the canonical page.",
    ),
    "access.pagination_issue": T(
        "Make paginated sequences crawlable and canonical-correct.",
        "Listing/paginated templates.",
        "Broken pagination hides items on deeper pages from discovery.",
        ["Provide crawlable next/prev links (real <a href>).",
         "Self-canonicalize each page (do not canonical page 2+ to page 1)."],
        "medium", 0.5, "All items across pages become discoverable.",
    ),
    "access.crawl_budget_risk": T(
        "Reduce low-value URLs competing for crawl budget.",
        "Faceted navigation / parameterized routes / sitemap.",
        "A large low-value URL surface delays crawling of important pages.",
        ["Disallow or canonicalize low-value parameter URLs.",
         "Keep the sitemap limited to canonical content."],
        "medium", 0.5, "Important pages are crawled more promptly.",
    ),
    # ---------------------------------------------------------------- render
    "render.primary_content_js_only": T(
        "Emit the page's primary content as text in the initial HTML response.",
        "The server-render / prerender layer for the affected templates.",
        "The detected mechanism is that primary content only appears after client "
        "JavaScript executes, so no-JS crawlers and many AI fetchers store an empty "
        "or partial shell.",
        ["Server-render or pre-render the headline, description and key facts.",
         "Hydrate on top of that HTML rather than replacing it.",
         "Verify with JavaScript disabled that the main content is present."],
        "high", 0.95,
        "No-JS fetchers capture the real content instead of a shell.",
    ),
    "render.class_js_gap": T(
        "Server-render the primary content for the affected page class.",
        "The shared template for the affected class.",
        "A whole class of pages hides its content behind JS, so that class is "
        "systematically under-read by machines.",
        ["Add SSR/SSG for the class template.",
         "Confirm the main text exists in the initial HTML."],
        "high", 0.85, "The entire page class becomes machine-readable.",
    ),
    "render.empty_initial_shell": T(
        "Return meaningful HTML in the first response, not an empty app root.",
        "The document template / SSR entry.",
        "An empty <div id=root> shell means the first response carries no content "
        "for non-executing consumers.",
        ["Render the above-the-fold content server-side.",
         "Ship a static fallback for crawlers."],
        "high", 0.9, "First response contains real, readable content.",
    ),
    "render.headings_js_only": T(
        "Include the document headings in the initial HTML.",
        "The template that injects headings client-side.",
        "Headings define the page's structure and topic; if they appear only after "
        "JS, machines misjudge what the page is about.",
        ["Server-render the <h1>/<h2> outline.",
         "Keep the visible heading identical to the SSR one."],
        "medium", 0.75, "Machines read the correct page structure/topic.",
    ),
    "render.content_behind_interaction": T(
        "Render interaction-gated content in the DOM by default.",
        "Accordion/tab/'read more' components holding primary facts.",
        "Content that only exists after a click is absent from the initial readable "
        "text an extractor sees.",
        ["Keep panels in the DOM (CSS-hide, not JS-inject).",
         "Move primary facts out of collapsed regions where possible."],
        "medium", 0.7, "Hidden facts become part of the readable text.",
    ),
    "render.excessive_script_dependence": T(
        "Reduce hard dependence on JS for core content.",
        "The rendering pipeline for content pages.",
        "When core content requires large JS execution, fragile or throttled "
        "fetchers get little or nothing.",
        ["Server-render core content; treat JS as enhancement.",
         "Provide <noscript> equivalents for critical facts."],
        "high", 0.7, "Core content survives without full JS execution.",
    ),
    "render.schema_injected_post_hydration": T(
        "Emit JSON-LD in the server response, not after hydration.",
        "The component that injects structured data client-side.",
        "Structured data added only after hydration is missed by consumers that "
        "read the initial HTML.",
        ["Render the JSON-LD <script> server-side in <head> or <body>."],
        "medium", 0.7, "Structured data is seen by non-executing consumers.",
    ),
    "render.text_as_image": T(
        "Replace meaningful text-in-images with real HTML text.",
        "The templates/components using image-rendered text.",
        "Text baked into images is invisible to text extractors and assistants.",
        ["Render the text as HTML; use CSS/web fonts for styling.",
         "If the image must stay, add exact alt text."],
        "medium", 0.7, "Previously image-locked text becomes extractable.",
    ),
    "render.canvas_content": T(
        "Provide an accessible HTML/text equivalent for canvas-rendered content.",
        "The <canvas> component and its data source.",
        "Canvas pixels carry no machine-readable text, so any facts there are lost.",
        ["Expose the underlying data as HTML/ARIA or a data table.",
         "Keep canvas as a visual enhancement only."],
        "high", 0.6, "Canvas-conveyed facts become readable.",
    ),
    # ------------------------------------------------------------ fact_access
    "fact.facts_locked_in_non_text": T(
        "Duplicate image/PDF-locked facts as HTML text on the page.",
        "The pages where key facts (pricing/specs) live only in images/PDFs.",
        "Facts trapped in non-text carriers cannot be identified, extracted or "
        "quoted by an assistant.",
        ["Transcribe the fact into on-page HTML text.",
         "Keep the image/PDF as a supplement, not the sole source."],
        "medium", 0.8, "Assistants can read and quote the fact.",
    ),
    "fact.informational_images_no_alt": T(
        "Add exact alt text and HTML equivalents for informational images.",
        "Informational <img> elements carrying facts.",
        "Without alt/HTML text, the fact the image conveys is unreadable to "
        "machines.",
        ["Write concise alt text stating the fact.",
         "Also render spec/price facts as HTML text."],
        "medium", 0.65, "Image-conveyed facts become extractable.",
        '<img src="spec.png" alt="Model X: 12-hour battery, 1.2 kg, USB-C">',
    ),
    "fact.no_quotable_value_prop": T(
        "Add one plain-text sentence stating what the brand does and for whom.",
        "Above the fold on the home page, as readable HTML text.",
        "With no concise, quotable value proposition, assistants have nothing "
        "clean to lift when describing the brand.",
        ["Write a single declarative sentence: '<Brand> is a <category> that "
         "<benefit> for <audience>.'",
         "Place it in an <h1>/<p>, not an image."],
        "low", 0.7, "Assistants get a clean sentence to quote.",
    ),
    "fact.ambiguous_value": T(
        "Replace the vague qualifier with a concrete, stated value.",
        "The page referencing the fact only vaguely (pricing/hours/location).",
        "A phrase like 'affordable pricing' or 'flexible hours' carries no "
        "extractable value, so the fact effectively does not exist for machines.",
        ["State the actual number/value in text (e.g. '$149', 'Mon-Fri 9-5').",
         "If it truly varies, give a range and the conditions."],
        "low", 0.7, "The fact becomes concrete and quotable.",
    ),
    "fact.contradictory_values": T(
        "Reconcile the conflicting values so one authoritative value remains.",
        "The structured data and visible text that disagree.",
        "When schema and visible text disagree (e.g. price), an assistant may quote "
        "the wrong value or distrust the page.",
        ["Update whichever source is stale so both match.",
         "Add a build check that structured price == displayed price."],
        "low", 0.85, "One consistent value machines can trust.",
    ),
    "fact.not_attributed_to_entity": T(
        "Bind the concrete fact to its named entity in structured data.",
        "The template rendering the fact (e.g. product price).",
        "A fact shown without structured association to a named entity cannot be "
        "attributed correctly.",
        ["Wrap the fact in Product/Offer (or appropriate) schema naming the "
         "entity.",
         "Ensure the entity name in schema matches the visible name."],
        "medium", 0.7, "The fact is unambiguously tied to the right entity.",
    ),
    "fact.behind_interaction": T(
        "Surface interaction-gated facts in the initial readable text.",
        "FAQ/specs/policy components hidden behind clicks.",
        "Facts only reachable after interaction are absent from the text an "
        "extractor first reads.",
        ["Render the content in the DOM (CSS-hide if needed).",
         "Prefer FAQPage schema for Q&A content."],
        "medium", 0.65, "Gated facts become part of the readable page.",
    ),
    # -------------------------------------------------------------- identity
    "identity.jsonld_malformed": T(
        "Fix the invalid JSON-LD so it parses.",
        "The script[type=application/ld+json] block flagged.",
        "Malformed JSON-LD is silently discarded, so the entity it describes is "
        "invisible to machines.",
        ["Correct the JSON syntax (quotes/commas/braces).",
         "Validate with a JSON-LD parser in CI."],
        "low", 0.8, "The described entity becomes machine-visible.",
    ),
    "identity.type_inappropriate": T(
        "Use a valid schema.org type for the entity.",
        "The @type value in the flagged structured data.",
        "An invented/typo'd @type is not recognized, so consumers ignore the "
        "entity.",
        ["Replace the invalid @type with the correct schema.org class.",
         "Check the type exists at schema.org."],
        "low", 0.6, "Consumers recognize and use the entity.",
    ),
    "identity.schema_invalid": T(
        "Populate the required properties the entity type expects.",
        "The flagged structured-data block.",
        "An entity missing required properties is incomplete and often rejected.",
        ["Add the missing required properties for the type.",
         "Fill them from the visible content."],
        "low", 0.65, "The entity is complete and usable.",
    ),
    "identity.schema_contradiction": T(
        "Resolve the internal contradiction in the structured data.",
        "The conflicting properties / reused @id.",
        "Internally inconsistent structured data (e.g. dateModified before "
        "datePublished, or one @id across types) is untrustworthy.",
        ["Fix the conflicting values.",
         "Give each distinct entity a unique @id."],
        "low", 0.7, "Structured data is internally consistent.",
    ),
    "identity.schema_visible_mismatch": T(
        "Make the structured value equal the visible value.",
        "The template producing schema and the visible fact.",
        "When a schema name/price differs from the visible page, machines may quote "
        "a stale or wrong fact.",
        ["Bind schema fields to the same source as the visible value.",
         "Add a check that schema == displayed value."],
        "low", 0.75, "Machines quote the same value users see.",
    ),
    "identity.duplicate_entity": T(
        "Consolidate to a single primary Organization entity.",
        "All templates emitting Organization markup.",
        "Multiple distinct primary Organizations make it impossible to decide which "
        "entity owns the site.",
        ["Emit one Organization with a stable @id sitewide.",
         "Reference that @id from other entities instead of redefining it."],
        "medium", 0.6, "One unambiguous entity owns the site.",
    ),
    "identity.entity_unlinked": T(
        "Link product/page entities to the Organization/Brand entity.",
        "Product/Article/WebPage structured data.",
        "Entities that never reference the Organization/Brand can't have their facts "
        "attributed to the company.",
        ["Add brand/publisher/isPartOf referencing the Organization @id."],
        "low", 0.6, "Facts attribute to the correct company.",
    ),
    "identity.canonical_entity_mismatch": T(
        "Align entity url/@id host with the page's own host.",
        "The url/@id fields in structured data.",
        "An entity whose url points at a different host risks attributing the "
        "content to another domain.",
        ["Set entity url/@id to the canonical host of the page."],
        "low", 0.6, "The entity is attributed to the right domain.",
    ),
    "identity.sameas_broken": T(
        "Fix malformed or wrong sameAs identifiers.",
        "The sameAs array on the Organization entity.",
        "Broken sameAs links fail to connect the brand to its authoritative "
        "profiles, weakening disambiguation.",
        ["Use absolute, correct URLs to official profiles "
         "(LinkedIn, Wikidata, Crunchbase)."],
        "low", 0.5, "The brand links to its authoritative profiles.",
    ),
    "identity.entity_ambiguous": T(
        "Disambiguate the brand identity with unique identifiers.",
        "The Organization entity + sameAs.",
        "A generic/ambiguous name without identifiers is easily confused with other "
        "entities of the same name.",
        ["Add sameAs to authoritative profiles.",
         "Use the full legal name and a stable @id."],
        "medium", 0.6, "The brand is distinguishable from same-name entities.",
    ),
    # -------------------------------------------------------------- freshness
    "freshness.content_stale": T(
        "Refresh or re-date the stale content, and expose an accurate date.",
        "The flagged page and its date metadata.",
        "Content presenting old dated facts with no recent update reads as stale and "
        "less trustworthy.",
        ["Review and update the facts.",
         "Emit accurate dateModified (do not fake it)."],
        "low", 0.55, "The page reads as current and trustworthy.",
    ),
    "freshness.self_contradiction": T(
        "Reconcile the conflicting values of the key fact across the site.",
        "The pages stating conflicting values (founding year/leadership/status).",
        "A site that contradicts itself on a key fact undermines machine confidence "
        "in every claim it makes.",
        ["Pick the correct value and update all occurrences.",
         "Centralize the fact so it is stated once."],
        "low", 0.7, "The fact is stated consistently everywhere.",
    ),
    "freshness.unsupported_claim": T(
        "Support the strong claim with attribution or soften it.",
        "The page making the superlative/absolute claim.",
        "An unsupported superlative ('#1', 'the best') is not verifiable and can "
        "read as untrustworthy.",
        ["Add a citation/source/ranking, or",
         "Reword to a verifiable, specific statement."],
        "low", 0.45, "Claims are verifiable and credible.",
    ),
    "freshness.claim_conflict": T(
        "Reconcile the on-site claim with authoritative external sources.",
        "The on-site fact that conflicts with external records.",
        "When the site disagrees with independent sources on a key fact, assistants "
        "may prefer the external value or flag the brand as inconsistent.",
        ["Verify the correct value and update the site, or",
         "Correct the external record if the site is right."],
        "medium", 0.55, "On-site and external facts agree.",
    ),
    # ------------------------------------------------------------- engagement
    "engagement.blocking_interstitial": T(
        "Stop blocking initial content with a full-screen overlay.",
        "The modal/interstitial component shown on first load.",
        "An overlay covering content on arrival forces an obstacle before value, "
        "driving bounces (and hiding content from some fetchers).",
        ["Defer the promo/newsletter modal (delay or exit-intent).",
         "Never gate the first content view behind it."],
        "low", 0.65, "Visitors see value immediately instead of a wall.",
    ),
    "engagement.no_orientation": T(
        "Add an above-the-fold value proposition and a primary action.",
        "The home page hero, as HTML text.",
        "With no heading and no CTA, a first-time visitor cannot tell what this is "
        "or what to do next.",
        ["Add an <h1> stating what the brand is and for whom.",
         "Add one clear primary CTA."],
        "medium", 0.6, "Visitors orient immediately and can act.",
    ),
    "engagement.no_ia_navigation": T(
        "Provide primary navigation (and ideally search).",
        "The site header / global template.",
        "With no nav, breadcrumb or search, visitors have no structured way to move "
        "through the site.",
        ["Add a labelled primary nav to the global header.",
         "Consider adding site search."],
        "medium", 0.6, "Visitors can navigate the site.",
    ),
    "engagement.content_hidden_interaction": T(
        "Render the orientation heading/value prop visibly by default.",
        "The home hero component.",
        "If the main heading exists only in hidden/gated DOM, the orientation "
        "message is absent from what visitors first see.",
        ["Show the <h1>/value prop without requiring interaction."],
        "medium", 0.6, "The orientation message is visible on arrival.",
    ),
    "engagement.unexplained_jargon": T(
        "Expand or replace acronym-only navigation labels.",
        "The primary navigation labels flagged.",
        "Bare acronyms never defined on the site confuse first-time visitors.",
        ["Spell out the term or add a short descriptor next to the acronym."],
        "low", 0.4, "Navigation is understandable to newcomers.",
    ),
    "engagement.unclear_navigation": T(
        "Give every link descriptive, unique text (or an aria-label).",
        "The flagged links (empty or 'click here'/'read more').",
        "Links with empty or non-descriptive text give visitors and assistants no "
        "idea where they lead.",
        ["Replace generic text with the destination's name.",
         "Add aria-label to icon-only links."],
        "low", 0.5, "Every link's destination is clear.",
    ),
    "engagement.inconsistent_naming": T(
        "Use one consistent label for each destination in primary nav.",
        "The navigation templates across pages.",
        "Labelling the same destination differently across pages breaks the "
        "visitor's mental map.",
        ["Standardize the nav label for each destination.",
         "Drive nav from a single source of truth."],
        "low", 0.5, "Navigation labels are stable and predictable.",
    ),
    "engagement.dead_end_page": T(
        "Add navigation and a clear next step to dead-end pages.",
        "The flagged pages that offer no way onward.",
        "A page with no nav, onward links or CTA strands the visitor.",
        ["Include the global nav on every page.",
         "Add contextual links / a CTA to continue the journey."],
        "low", 0.5, "Visitors always have a next step.",
    ),
    "engagement.key_info_undiscoverable": T(
        "Link product/pricing/contact pages from the home page.",
        "The home page and primary navigation.",
        "Key pages that exist but aren't linked from home add friction before the "
        "information that drives decisions.",
        ["Add prominent home/nav links to the flagged page classes."],
        "low", 0.6, "Key information is reachable from the entry point.",
    ),
    "engagement.no_viewport_meta": T(
        "Add a responsive viewport meta and allow pinch-zoom.",
        "The document <head> template.",
        "Missing viewport (or disabled zoom) breaks mobile layout/usability where "
        "it is reliably detectable.",
        ["Add <meta name=viewport content='width=device-width, initial-scale=1'>.",
         "Do not set user-scalable=no / maximum-scale=1."],
        "low", 0.5, "Pages adapt to phones and remain zoomable.",
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
    ),
}
