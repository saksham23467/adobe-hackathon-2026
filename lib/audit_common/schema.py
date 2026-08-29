"""The normalized cross-skill contract: Observation -> Finding -> Report.

Every skill in the marketplace speaks this language. Detectors emit
``Observation`` records; the orchestrator compiles them into ``Finding``
records; the recommendation engine attaches ``SuggestedAction``; the
orchestrator emits the final ``Report``.

Only the Python standard library is used. ``validate_report`` is a small,
dependency-free structural validator that mirrors config/report_schema.json
so a submission never emits an out-of-spec report.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------
# Controlled vocabularies
# --------------------------------------------------------------------------
class Concern(str, Enum):
    CRAWL_ACCESS = "crawl_access"
    RENDER = "render"
    FACT_ACCESS = "fact_access"
    IDENTITY = "identity"
    FRESHNESS = "freshness"
    ENGAGEMENT = "engagement"


class Method(str, Enum):
    HTTP = "http"
    RENDER = "render"
    HEADER = "header"
    ROBOTS = "robots"
    SITEMAP = "sitemap"
    SCHEMA = "schema"
    OCR = "ocr"
    EXTERNAL = "external"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"

    @property
    def rank(self) -> int:
        return {"critical": 3, "high": 2, "medium": 1}[self.value]


class Status(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"
    NOT_APPLICABLE = "not_applicable"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"high": 3, "medium": 2, "low": 1}[self.value]


def utcnow_iso() -> str:
    """RFC3339 / ISO-8601 timestamp in UTC (Zulu)."""
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


# --------------------------------------------------------------------------
# Observation -- raw detector output
# --------------------------------------------------------------------------
@dataclass
class Observation:
    check_id: str
    concern: Concern
    method: Method
    status: Status
    metric: Dict[str, Any] = field(default_factory=dict)
    artifacts: List[Dict[str, str]] = field(default_factory=list)
    urls: List[str] = field(default_factory=list)
    sample_n: int = 0
    population_n: Optional[int] = None
    selection: str = ""
    confidence: Confidence = Confidence.MEDIUM
    # Optional evidence sentence supplied by the detector; the compiler will
    # synthesize one from the metric if this is empty.
    evidence_sentence: str = ""
    observed_at: str = field(default_factory=utcnow_iso)
    # Semantic gating (internal). A true observation with low relevance must
    # not become a high-severity finding.
    resource_type: str = ""
    page_type: str = ""
    entity_scope: str = ""
    semantic_relevance: str = "high"   # high | medium | low | none
    evidence_tier: str = ""            # confirmed | likely | review | informational

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check_id": self.check_id,
            "concern": self.concern.value,
            "method": self.method.value,
            "status": self.status.value,
            "metric": self.metric,
            "artifacts": self.artifacts,
            "target": {
                "urls": self.urls,
                "sample_n": self.sample_n,
                "population_n": self.population_n,
                "selection": self.selection,
            },
            "confidence": self.confidence.value,
            "evidence_sentence": self.evidence_sentence,
            "observed_at": self.observed_at,
            "resource_type": self.resource_type,
            "page_type": self.page_type,
            "entity_scope": self.entity_scope,
            "semantic_relevance": self.semantic_relevance,
            "evidence_tier": self.evidence_tier,
        }


# --------------------------------------------------------------------------
# SuggestedAction / Finding -- compiled defect
# --------------------------------------------------------------------------
@dataclass
class SuggestedAction:
    summary: str
    priority: Severity
    steps: List[str] = field(default_factory=list)
    patch: Optional[str] = None
    effort: str = "medium"  # low | medium | high
    expected_effect: str = ""
    # The eight questions a recommendation must answer (1-8). `steps` is "how",
    # `expected_effect` is "expected impact", `effort` is "how difficult", and
    # `priority` / `rank` / `do_first` answer "what should be done first".
    what: str = ""            # 1. what exactly should change
    where: str = ""           # 2. where should it change
    why: str = ""             # 3. why it solves the detected mechanism
    evidence: str = ""        # 5. the observed evidence supporting the fix
    impact: float = 0.0       # normalized expected-impact weight (0..1)
    priority_score: float = 0.0
    rank: int = 0             # 1-based global execution order
    do_first: bool = False    # quick, foundational win to tackle first
    dependencies: List[str] = field(default_factory=list)  # 6. prerequisite finding ids
    duplicate_of: Optional[str] = None  # set on de-duplicated recommendations

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary,
            "priority": self.priority.value,
            "what": self.what,
            "where": self.where,
            "why": self.why,
            "steps": self.steps,
            "patch": self.patch,
            "evidence": self.evidence,
            "expected_effect": self.expected_effect,
            "effort": self.effort,
            "impact": self.impact,
            "priority_score": self.priority_score,
            "rank": self.rank,
            "do_first": self.do_first,
            "dependencies": self.dependencies,
            "duplicate_of": self.duplicate_of,
        }


@dataclass
class Finding:
    id: str
    check_id: str
    concern: Concern
    mechanism: str  # one of A..F (Round-2 appendix node)
    title: str
    severity: Severity
    confidence: Confidence
    evidence: Dict[str, Any]
    suggested_action: Optional[SuggestedAction] = None
    related_findings: List[str] = field(default_factory=list)
    # Reconciliation / non-expert enrichment (set by the orchestrator).
    category: str = ""                 # human-friendly concern label
    affected_urls: List[str] = field(default_factory=list)
    sources: List[Dict[str, Any]] = field(default_factory=list)  # which skills/checks/methods
    evidence_type: str = ""            # e.g. "initial HTML", "rendered DOM"
    contested: bool = False            # skills disagreed -> confidence lowered
    resource_type: str = ""
    page_type: str = ""
    entity_scope: str = ""
    semantic_relevance: str = "high"
    evidence_tier: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "check_id": self.check_id,
            "concern": self.concern.value,
            "category": self.category,
            "mechanism": self.mechanism,
            "title": self.title,
            "severity": self.severity.value,
            "confidence": self.confidence.value,
            "contested": self.contested,
            "affected_urls": self.affected_urls,
            "evidence_type": self.evidence_type,
            "evidence": self.evidence,
            "sources": self.sources,
            "suggested_action": (
                self.suggested_action.to_dict() if self.suggested_action else None
            ),
            "related_findings": self.related_findings,
            "resource_type": self.resource_type,
            "page_type": self.page_type,
            "entity_scope": self.entity_scope,
            "semantic_relevance": self.semantic_relevance,
            "evidence_tier": self.evidence_tier,
        }


# --------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------
def new_report(
    site: str,
    findings: List[Finding],
    beyond_defect: Optional[List[Dict[str, Any]]] = None,
    coverage: Optional[Dict[str, Any]] = None,
    limitations: Optional[List[Dict[str, str]]] = None,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble a report dict that satisfies the required Adobe schema."""
    counts = {"critical": 0, "high": 0, "medium": 0}
    for f in findings:
        counts[f.severity.value] += 1
    report = {
        "site": site,
        "audited_at": utcnow_iso(),
        "summary": {
            "total_findings": len(findings),
            "critical": counts["critical"],
            "high": counts["high"],
            "medium": counts["medium"],
        },
        "findings": [f.to_dict() for f in findings],
        "beyond_defect_recommendations": beyond_defect or [],
        "coverage": coverage or {},
        "limitations": limitations or [],
        "meta": meta or {},
    }
    return report


# --------------------------------------------------------------------------
# Dependency-free structural validation (mirrors report_schema.json)
# --------------------------------------------------------------------------
_REQUIRED_SEVERITIES = {"critical", "high", "medium"}


def validate_report(report: Dict[str, Any]) -> List[str]:
    """Return a list of human-readable errors. Empty list == valid.

    Enforces the required floor from the Adobe handout plus the internal
    contract. Intentionally strict on required fields, lenient on extensions.
    """
    errors: List[str] = []

    def require(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    require(isinstance(report, dict), "report must be an object")
    if not isinstance(report, dict):
        return errors

    require(isinstance(report.get("site"), str) and report["site"] != "", "site: required non-empty string")
    require(isinstance(report.get("audited_at"), str) and report["audited_at"] != "", "audited_at: required non-empty string")

    summary = report.get("summary")
    require(isinstance(summary, dict), "summary: required object")
    if isinstance(summary, dict):
        for key in ("total_findings", "critical", "high", "medium"):
            require(isinstance(summary.get(key), int), f"summary.{key}: required integer")

    findings = report.get("findings")
    require(isinstance(findings, list), "findings: required array")
    if isinstance(findings, list):
        seen_ids = set()
        for i, f in enumerate(findings):
            loc = f"findings[{i}]"
            if not isinstance(f, dict):
                errors.append(f"{loc}: must be object")
                continue
            for key in ("id", "title", "severity", "evidence", "suggested_action"):
                require(key in f and f[key] is not None, f"{loc}.{key}: required")
            if f.get("severity") not in _REQUIRED_SEVERITIES:
                errors.append(f"{loc}.severity: must be one of {sorted(_REQUIRED_SEVERITIES)}")
            fid = f.get("id")
            if fid in seen_ids:
                errors.append(f"{loc}.id: duplicate id '{fid}'")
            seen_ids.add(fid)
            ev = f.get("evidence")
            if isinstance(ev, str):
                require(ev.strip() != "", f"{loc}.evidence: required non-empty string")
            elif isinstance(ev, dict):
                require(
                    bool((ev.get("sentence") or ev.get("observed_fact") or "").strip()),
                    f"{loc}.evidence: required sentence",
                )
            else:
                errors.append(f"{loc}.evidence: must be a string (handout floor) or object")
            sa = f.get("suggested_action")
            if isinstance(sa, dict):
                require(isinstance(sa.get("summary"), str) and sa["summary"] != "", f"{loc}.suggested_action.summary: required")
                require(sa.get("priority") in _REQUIRED_SEVERITIES, f"{loc}.suggested_action.priority: must be a severity")
            elif sa is not None:
                errors.append(f"{loc}.suggested_action: must be object")

    # Cross-check summary counts against findings (internal consistency).
    if isinstance(findings, list) and isinstance(summary, dict):
        if summary.get("total_findings") != len(findings):
            errors.append("summary.total_findings does not match len(findings)")

    return errors


def as_json_dict(obj: Any) -> Any:
    """Recursively convert dataclasses / enums to plain JSON-safe values."""
    if isinstance(obj, Enum):
        return obj.value
    if dataclasses.is_dataclass(obj):
        return as_json_dict(dataclasses.asdict(obj))
    if isinstance(obj, dict):
        return {k: as_json_dict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [as_json_dict(v) for v in obj]
    return obj
