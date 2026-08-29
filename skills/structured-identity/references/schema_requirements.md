# Structured data & entity identity — check catalog

**Core principle:** schema presence is not automatically good, and schema
*absence* is never a finding. Every check below requires the markup/entity to
exist and connects it to a discoverability, extraction, attribution, or
ambiguity problem. Findings are tiered: `confirmed` → FAIL/high, `likely` →
FAIL/medium. Each finding carries `affected_entity`, `detected_mismatch`,
`expected_relationship`, `mechanism`, `fix`, and a `confidence_score`.

## Required properties (used by `identity.schema_invalid`)
| Type | Required for a valid finding |
|---|---|
| Product | `name` |
| Offer | `priceCurrency` when `price` is present |
| Organization / LocalBusiness | `name` |
| WebSite | `name`, `url` |
| Article / NewsArticle / BlogPosting | `datePublished` |
| PostalAddress | `streetAddress` or `addressLocality` |

(Only high-confidence, unambiguous omissions are flagged; optional-but-useful
properties like `logo` or `sameAs` are not treated as "invalid".)

## Detectors
| check_id | Tier | Predicate | FP guard |
|---|---|---|---|
| `identity.jsonld_malformed` | confirmed | an `ld+json` block fails `json.loads` | reports the exact parser error + snippet |
| `identity.type_inappropriate` | likely | `@type` not in the schema.org set and looks like a single CamelCase attempt | ignores full-URL types and multi-token values; suggests nearest valid type |
| `identity.schema_invalid` | confirmed | a required property (table above) is missing | scoped per type; only unambiguous omissions |
| `identity.schema_contradiction` | confirmed | `dateModified` < `datePublished`, or one `@id` reused across `@type`s | date compared on `YYYY-MM-DD` prefix |
| `identity.schema_visible_mismatch` | likely | Product schema name absent from the page, or Offer price not among visible prices | requires ≥30 chars of visible text and a currency-marked visible price |
| `identity.duplicate_entity` | likely | ≥2 primary Organizations with different, non-similar names | similar names go to ambiguity, not duplication |
| `identity.entity_ambiguous` | likely | ≥2 org/brand names that are similar-but-different | exact-equal names are not ambiguous |
| `identity.entity_unlinked` | likely | a Product exists, an Organization exists, but no Product references it | only fires when an Organization actually exists |
| `identity.sameas_broken` | likely | `sameAs` present but contains non-URL/malformed entries | missing `sameAs` is **not** flagged |
| `identity.canonical_entity_mismatch` | likely | entity `url`/`@id` host ≠ page host (www-insensitive) | only when an absolute http url is present |

## Entity graph
`entitygraph.build_graph()` yields `EntityNode`s (JSON-LD `@graph` flattened,
nested typed objects collected) across all sampled pages. `EntityGraph.references()`
resolves identity edges from `@id`, `brand`, `manufacturer`, `publisher`,
`isPartOf`, `about`, `mainEntityOfPage`, `provider`, `sameAs` — used for linkage,
duplication, and ambiguity reasoning.

## Parsing notes
- JSON-LD is the primary syntax; microdata (`itemscope`/`itemprop`) and RDFa
  (`typeof`/`property`) are parsed as a practical subset into the same node model.
- Nodes are read from rendered HTML when available, else raw HTML.
