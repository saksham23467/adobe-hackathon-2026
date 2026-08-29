---
name: engagement-orientation
description: >-
  Audit the visitor's information journey — arrival → orientation →
  understanding → evaluation → next action — to find where a first-time visitor
  fails to understand what the company does, loses context, or has no next step.
  Deterministically parses each page into a journey model (headings, primary vs
  breadcrumb navigation, links with structural context, CTAs, mobile viewport,
  blocking overlays, hidden orientation content, contact affordances) and runs
  tiered detectors: blocking interstitials, weak above-the-fold orientation,
  missing IA, orientation content hidden behind interactions, ambiguous/empty
  nav links, inconsistent naming for the same destination, dead-end pages,
  key information (product/pricing/contact) unreachable from the home page, and
  mobile-viewport signals. Not a Lighthouse clone and not subjective visual
  critique — every finding is backed by an observable DOM fact.
license: MIT
metadata:
  concern: engagement
  mechanism: "E"
allowed-tools: Bash(python3:*) Read
---

# Engagement & Orientation

Once a visitor arrives, can they orient, understand, evaluate, and act? This
skill models the journey and reports exactly where it breaks.

## The journey model
```
arrival → orientation → understanding → evaluation → next action
```
Each detector is tagged with the stage it breaks (`metric.stage`).

## When to use
After acquisition. Reads the shared cache (prefers rendered DOM). Read-only,
deterministic, standard library only.

## Procedure (deterministic)
1. `journey.py` parses every cached page into a `PageView`: title, visible vs
   hidden `<h1>`, primary-nav vs breadcrumb links, links classified by context
   (nav / header / footer / main), CTAs (action-verb links & buttons), viewport
   meta, blocking overlays (excluding consent banners), and contact affordances.
2. Pages are wired into a `JourneyModel` with a home→page reachability graph.
3. `engagedetect.py` runs the tiered detectors and emits Observations.

## Finding tiers (mapped to confidence)
- **Confirmed structural issue** (`Confidence.HIGH`): an objective DOM defect —
  blocking interstitial, empty/ambiguous link, inconsistent naming.
- **Likely UX issue** (`Confidence.MEDIUM`): strong signal with some judgement —
  weak orientation, dead-end page, key info unreachable, mobile viewport.
- **Recommendation** (`Confidence.LOW`): worth improving, not a defect —
  unexplained navigation jargon.

## Evidence produced
"A full-content-blocking overlay ('Join our newsletter to continue') precedes
the content", "The same destination (/plans) is labelled 'Pricing' and 'Plans'
across the primary nav", "The site has a contact page but the home page does not
link to it."

## False-positive guards
- Cookie/consent banners are **not** blocking interstitials.
- A clear `<h1>` **or** a primary CTA is enough to pass orientation.
- Breadcrumb relabelling (nav "Products" vs breadcrumb "Widget") is legitimate
  and never counts as inconsistent naming.
- Absence is never penalised: key-info discoverability fires only when a
  product/pricing/contact page **exists** but is unreachable from home.
- Dead-end fires only when a page has no nav, no onward content links, **and** no
  CTA — i.e. the visitor is genuinely stuck.
- Mobile signals use only reliably detectable facts (viewport meta / zoom lock),
  never rendering-dependent guesses.

## Outputs
Observations for `engagement.*` checks. See `references/engagement_catalog.md`.
