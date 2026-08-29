"""Freshness & corroboration detectors.

On-site checks run always (staleness, self-contradiction, unusually strong but
unsupported claims). External corroboration runs only when a backend is available
and is strictly conservative: a finding requires a *conflict* or *cross-source
ambiguity*, never the mere absence of coverage. Every finding is classified as
one of: corroborated | partially_corroborated | inconsistent | stale-risk |
insufficient_evidence.
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
from typing import Dict, List, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_LIB = os.path.join(_ROOT, "lib")
for _p in (_HERE, _LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import claims as C  # noqa: E402
from audit_common.schema import (  # noqa: E402
    Concern,
    Confidence,
    Method,
    Observation,
    Status,
)

_STATUS_CONFLICTS = [
    ("open_247", "permanently_closed"),
    ("open_247", "temporarily_closed"),
    ("open_247", "coming_soon"),
    ("permanently_closed", "coming_soon"),
    ("permanently_closed", "temporarily_closed"),
]


def _fail(check_id, concern, method, confidence, urls, metric, evidence, artifacts=None):
    return Observation(
        check_id=check_id, concern=concern, method=method, status=Status.FAIL,
        confidence=confidence, urls=urls[:20], sample_n=len(urls),
        metric=metric, artifacts=(artifacts or [])[:20], evidence_sentence=evidence,
    )


def _note(check_id, concern, method, status, evidence, metric=None):
    return Observation(
        check_id=check_id, concern=concern, method=method, status=status,
        confidence=Confidence.LOW, metric=metric or {}, evidence_sentence=evidence,
    )


# --------------------------------------------------------------------------
# Q1/Q2: staleness (stale-risk)
# --------------------------------------------------------------------------
def detect_stale(sc: C.SiteClaims, now: _dt.date, stale_days: int) -> List[Observation]:
    cds = C.content_dates(sc.dates)
    if not cds:
        return []  # no content date -> no signal (copyright alone is never stale)
    # Evergreen docs/articles are not a site-level staleness defect.
    timed = [d for d in cds if d.page_class not in ("docs",)]
    if not timed:
        return []
    newest = max(timed, key=lambda d: d.date)
    age = (now - newest.date).days
    if age <= stale_days:
        return [_note("freshness.content_stale", Concern.FRESHNESS, Method.HTTP, Status.PASS,
                      f"Most recent content date is {newest.date.isoformat()} ({age} days old).",
                      {"classification": "corroborated", "newest_content_date": newest.date.isoformat()})]
    years = round(age / 365.0, 1)
    return [_fail(
        "freshness.content_stale", Concern.FRESHNESS, Method.HTTP, Confidence.MEDIUM, [""],
        {"classification": "stale-risk", "newest_content_date": newest.date.isoformat(),
         "age_days": age, "mechanism": "The newest dated content is old, so time-sensitive facts "
         "(pricing, availability, leadership) may be out of date and an assistant cannot trust them as current.",
         "fix": "Refresh and re-date the affected pages (update dateModified) once the facts are verified.",
         "confidence_score": 0.6},
        f"The newest dated content on the site is {newest.date.isoformat()} — about {years} years old "
        f"(kind={newest.kind}); time-sensitive claims are stale-risk.",
        artifacts=[{"type": "date", "label": newest.kind, "value": newest.raw}],
    )]


# --------------------------------------------------------------------------
# Q3: self-contradiction (inconsistent, on-site)
# --------------------------------------------------------------------------
def detect_self_contradiction(sc: C.SiteClaims) -> List[Observation]:
    out: List[Observation] = []

    if len(sc.founding_years) >= 2:
        urls = sorted({c.url for c in sc.claims if c.category == "founding_year"})
        out.append(_fail(
            "freshness.self_contradiction", Concern.FRESHNESS, Method.HTTP, Confidence.HIGH, urls,
            {"classification": "inconsistent", "claim_category": "founding_year",
             "values": sorted(sc.founding_years),
             "mechanism": "The site states different founding years, so a machine cannot know which is "
             "correct and may quote a wrong date.",
             "fix": "Use a single, correct founding year everywhere (and in Organization.foundingDate).",
             "confidence_score": 0.85},
            f"The site gives conflicting founding years: {sorted(sc.founding_years)}.",
            artifacts=[{"type": "conflict", "label": "founding_year", "value": ", ".join(sorted(sc.founding_years))}],
        ))

    roles: Dict[str, set] = {}
    for c in sc.claims:
        if c.category == "leadership" and ":" in c.value:
            role, name = c.value.split(":", 1)
            roles.setdefault(role.strip(), set()).add(name.strip())
    for role, names in roles.items():
        if len(names) >= 2:
            urls = sorted({c.url for c in sc.claims if c.category == "leadership"})
            out.append(_fail(
                "freshness.self_contradiction", Concern.FRESHNESS, Method.HTTP, Confidence.HIGH, urls,
                {"classification": "inconsistent", "claim_category": "leadership", "role": role,
                 "values": sorted(names),
                 "mechanism": f"The site names different people as {role}, so the leadership fact is "
                 "self-contradictory and untrustworthy.",
                 "fix": f"State one current {role} consistently across the site.",
                 "confidence_score": 0.8},
                f"The site names conflicting {role}s: {sorted(names)}.",
                artifacts=[{"type": "conflict", "label": role, "value": ", ".join(sorted(names))}],
            ))

    for a, b in _STATUS_CONFLICTS:
        if a in sc.statuses and b in sc.statuses:
            urls = sorted({c.url for c in sc.claims if c.category == "operating_status"})
            out.append(_fail(
                "freshness.self_contradiction", Concern.FRESHNESS, Method.HTTP, Confidence.MEDIUM, urls,
                {"classification": "inconsistent", "claim_category": "operating_status",
                 "values": [a, b],
                 "mechanism": "The site states conflicting operating statuses, so a machine cannot tell "
                 "whether the business is open.",
                 "fix": "Show a single, current operating status site-wide.",
                 "confidence_score": 0.7},
                f"The site states conflicting operating statuses: '{a}' and '{b}'.",
                artifacts=[{"type": "conflict", "label": "operating_status", "value": f"{a} vs {b}"}],
            ))
            break
    return out


# --------------------------------------------------------------------------
# Q7: unusually strong but weakly supported (insufficient_evidence)
# --------------------------------------------------------------------------
def detect_unsupported_claims(sc: C.SiteClaims) -> List[Observation]:
    strong = [
        c for c in sc.claims
        if c.category == "marketing_superlative" and c.strength == "strong" and c.brand_tied
    ]
    if not strong:
        return []
    urls = sorted({c.url for c in strong})
    items = [{"url": c.url, "claim": c.value, "sentence": c.evidence} for c in strong]
    return [_fail(
        "freshness.unsupported_claim", Concern.FRESHNESS, Method.HTTP, Confidence.LOW, urls,
        {"classification": "insufficient_evidence", "claim_category": "marketing_superlative",
         "items": items[:20],
         "mechanism": "A superlative/absolute claim is made with no attribution, citation, ranking source, "
         "or verifiable detail, so an AI system cannot confidently repeat it.",
         "fix": "Back the claim with a cited source/ranking (with a year) or soften it to a verifiable statement.",
         "confidence_score": 0.4},
        f"{len(strong)} strong marketing claim(s) have no supporting evidence; e.g. \"{items[0]['sentence']}\" "
        f"on {items[0]['url']}.",
        artifacts=[{"type": "snippet", "label": c.url, "value": c.evidence} for c in strong],
    )]


# --------------------------------------------------------------------------
# Q4/Q5/Q6: external corroboration (conservative, fail-soft)
# --------------------------------------------------------------------------
def detect_external(sc: C.SiteClaims, corr, site_host: str = "") -> List[Observation]:
    if corr is None:
        return [_note("freshness.claim_conflict", Concern.FRESHNESS, Method.EXTERNAL,
                      Status.INCONCLUSIVE, "external corroboration not performed (no backend/budget)",
                      {"classification": "insufficient_evidence"})]
    if not sc.brand_name:
        return [_note("freshness.claim_conflict", Concern.FRESHNESS, Method.EXTERNAL,
                      Status.INCONCLUSIVE, "no brand entity to corroborate",
                      {"classification": "insufficient_evidence"})]

    try:
        res = corr.lookup(sc.brand_name, site_host=site_host)
    except TypeError:
        res = corr.lookup(sc.brand_name)
    if res is None:
        return [_note("freshness.claim_conflict", Concern.FRESHNESS, Method.EXTERNAL,
                      Status.INCONCLUSIVE, "external lookup failed/timed out",
                      {"classification": "insufficient_evidence"})]
    if not res.matched:
        # Absence of coverage is NOT a problem (protects small/local/new brands).
        return [_note("freshness.claim_conflict", Concern.FRESHNESS, Method.EXTERNAL, Status.PASS,
                      f"No confident external match for '{sc.brand_name}'; not penalized (new/small brand).",
                      {"classification": "insufficient_evidence", "candidate_count": res.candidate_count})]

    out: List[Observation] = []

    if res.prominent_candidates >= 2 and not sc.has_sameas:
        out.append(_fail(
            "identity.entity_ambiguous", Concern.IDENTITY, Method.EXTERNAL, Confidence.MEDIUM, [""],
            {"classification": "inconsistent", "affected_entity": sc.brand_name,
             "candidate_count": res.candidate_count, "prominent_candidates": res.prominent_candidates,
             "source_urls": [res.source_url] if res.source_url else [],
             "mechanism": "The brand name matches several prominent distinct entities and the site declares "
             "no sameAs disambiguators, so assistants may resolve it to the wrong entity.",
             "fix": "Add Organization.sameAs links to canonical profiles (Wikidata/LinkedIn) and a legalName.",
             "confidence_score": 0.6},
            f"'{sc.brand_name}' matches {res.prominent_candidates} prominent entities and the site has no "
            f"sameAs disambiguators — the entity is ambiguous across sources.",
            artifacts=[{"type": "source", "label": "wikidata", "value": res.source_url}],
        ))

    conflicts = []
    if res.claims.get("founding_year") and sc.founding_years and \
            res.claims["founding_year"] not in sc.founding_years:
        conflicts.append(("founding_year", sorted(sc.founding_years), res.claims["founding_year"]))
    ceo = res.claims.get("ceo") or ""
    if ceo and not ceo.startswith("Q") and sc.leadership.get("ceo") and \
            _norm(ceo) != _norm(sc.leadership["ceo"]):
        conflicts.append(("ceo", [sc.leadership["ceo"]], ceo))

    for cat, site_vals, ext_val in conflicts:
        out.append(_fail(
            "freshness.claim_conflict", Concern.FRESHNESS, Method.EXTERNAL, Confidence.MEDIUM, [""],
            {"classification": "inconsistent", "claim_category": cat, "site_value": site_vals,
             "external_value": ext_val, "source_urls": [res.source_url] if res.source_url else [],
             "mechanism": f"The site's {cat} disagrees with an independent source, so an assistant faces "
             "contradictory facts and may distrust or mis-state the claim.",
             "fix": f"Reconcile the {cat}: correct the site or the external record so they agree.",
             "confidence_score": 0.6},
            f"Site {cat} {site_vals} conflicts with {res.label} ({res.source_url}): {ext_val}.",
            artifacts=[{"type": "source", "label": res.label, "value": res.source_url}],
        ))

    if not out:
        corroborated = [k for k in ("founding_year", "ceo") if res.claims.get(k)]
        cls = "corroborated" if corroborated else "partially_corroborated"
        out.append(_note("freshness.claim_conflict", Concern.FRESHNESS, Method.EXTERNAL, Status.PASS,
                         f"'{sc.brand_name}' corroborated by {res.label} ({res.source_url}); no conflicts.",
                         {"classification": cls, "source_urls": [res.source_url] if res.source_url else [],
                          "corroborated_claims": corroborated}))
    return out


def _norm(s: str) -> str:
    import re
    return re.sub(r"[^a-z ]", "", (s or "").lower()).strip()


def _worth_external(sc: C.SiteClaims) -> bool:
    """External search is *useful* only when the lookup could actually yield a
    finding. It cannot when there is no brand entity, OR when the site is already
    fully self-disambiguated (declares ``sameAs``) *and* makes no dated /
    leadership claims to cross-check — then the only possible outcome is a
    positive informational note. Every case that can surface a defect (any
    unresolved brand, or any corroboratable claim) still runs, so no check is
    skipped. This avoids wasted Wikidata round-trips on already-clean sites."""
    if not sc.brand_name:
        return False
    has_claims = bool(
        sc.founding_years or sc.leadership or sc.statuses
        or any(c.category == "marketing_superlative" and c.strength == "strong"
               and getattr(c, "brand_tied", True) for c in sc.claims)
    )
    if sc.has_sameas and not has_claims:
        return False  # fully disambiguated + nothing to cross-check
    return True


def run_all(records, *, corr=None, now: Optional[_dt.date] = None,
            stale_days: int = 1095, site_host: str = "") -> List[Observation]:
    sc = C.extract_site_claims(records)
    now = now or _dt.date.today()

    if corr is not None and not _worth_external(sc):
        external_fn = lambda: [_note(  # noqa: E731
            "freshness.claim_conflict", Concern.FRESHNESS, Method.EXTERNAL,
            Status.INCONCLUSIVE,
            "external corroboration skipped: brand already self-disambiguated "
            "(sameAs present) with no dated/leadership claims to cross-check",
            {"classification": "insufficient_evidence"})]
    else:
        external_fn = lambda: detect_external(sc, corr, site_host)  # noqa: E731

    out: List[Observation] = []
    for fn in (lambda: detect_stale(sc, now, stale_days),
               lambda: detect_self_contradiction(sc),
               lambda: detect_unsupported_claims(sc),
               external_fn):
        try:
            out += fn()
        except Exception:
            continue
    return out
