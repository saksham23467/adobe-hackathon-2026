---
name: structured-identity
description: >-
  Audit structured data and entity identity without treating schema presence as
  automatically good — and without ever flagging a site merely for lacking
  schema. Parses JSON-LD (primary), microdata, and RDFa into one normalized
  entity graph, then checks: malformed JSON-LD, unrecognized/typo @type,
  missing required properties, internally contradictory data, structured-vs-
  visible mismatch (stale/wrong values), duplicate or conflicting primary
  entities, disconnected entity graphs (Product not linked to Organization),
  confusingly similar brand names, malformed sameAs links, and entity-url/host
  inconsistency. Every finding connects structured data to an actual
  discoverability, extraction, attribution, or ambiguity problem and carries
  exact evidence, the affected entity, the detected mismatch, the expected
  relationship, severity, confidence, mechanism, and a concrete fix.
license: MIT
metadata:
  concern: identity
  mechanism: "A,D"
allowed-tools: Bash(python3:*) Read
---

# Structured Data & Entity Identity

Are the brand's facts machine-quotable via *correct* structured data, and is the
entity unambiguous? Presence of schema is necessary but not sufficient — bad
schema can be worse than none.

## When to use
After acquisition. Reads the shared cache; never re-fetches.

## Engine
- `scripts/entitygraph.py` — parses JSON-LD / microdata / RDFa into `EntityNode`s
  and builds an `EntityGraph` whose edges express identity relationships (brand,
  publisher, isPartOf, about, @id references, sameAs). Also provides
  name-normalization + similarity for ambiguity detection.
- `scripts/identitydetect.py` — the ten detectors, each tied to a concrete
  problem, never to mere absence.
- `scripts/detect.py` — entrypoint over `ctx.cache`.

## Checks (all require schema to be present / an entity to exist)
| # | check_id | Problem it proves |
|---|---|---|
| 6 | `identity.jsonld_malformed` | invalid JSON → entity invisible to machines |
| 4 | `identity.type_inappropriate` | unrecognized/typo @type → markup ignored |
| 5 | `identity.schema_invalid` | required property missing → fact not usable |
| 7 | `identity.schema_contradiction` | internally inconsistent data → untrustworthy |
| 8 | `identity.schema_visible_mismatch` | schema disagrees with the page → stale/wrong quote |
| 9 | `identity.duplicate_entity` | conflicting primary orgs → cannot decide site owner |
| 16/17 | `identity.entity_ambiguous` | confusingly similar brand names → conflation |
| 13 | `identity.entity_unlinked` | Product not linked to Org → cannot attribute |
| 14 | `identity.sameas_broken` | malformed sameAs → grounding breaks |
| 15 | `identity.canonical_entity_mismatch` | entity url on a different host → misattribution |

## The entity graph
Nodes are every typed object across all sampled pages (JSON-LD `@graph` flattened;
nested Offer/PostalAddress/Brand collected too). Edges are resolved from `@id`,
`brand`, `manufacturer`, `publisher`, `isPartOf`, `about`, `mainEntityOfPage`,
`provider`, and `sameAs`. Linkage, duplication, and ambiguity are computed on this
graph, so a finding can name the affected entity and the expected relationship.

## Evidence produced
Each finding stores `affected_entity`, `detected_mismatch`, `expected_relationship`,
`mechanism`, `fix`, a `confidence_score`, and artifacts. Example: "schema price
149.0 not shown in visible text [199.0] on `/products/widget` — an assistant may
quote a stale value."

## False-positive guards
- **Absence is never a finding.** A site with no structured data but clear content
  produces zero findings here.
- Malformed vs missing vs invalid are distinct checks; they never double-count.
- `type_inappropriate` fires only for tokens that look like a schema attempt
  (single CamelCase word) outside the known type set — catching typos.
- Ambiguity requires two *similar-but-different* brand names; duplication requires
  two *different, non-similar* primary orgs.
- `sameas_broken` and `canonical_entity_mismatch` fire only when the property is
  present and demonstrably wrong.

## Outputs
Observations for `identity.*` checks. See `references/schema_requirements.md`.
