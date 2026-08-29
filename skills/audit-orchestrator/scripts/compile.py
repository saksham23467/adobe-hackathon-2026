"""Compile normalized Observations into Findings.

This is the last line of false-positive defense:
  * Closed taxonomy    -- drop any observation whose check_id is unknown.
  * Three-valued gate  -- only status == fail becomes a finding; inconclusive
                          becomes a limitation; pass / not_applicable are dropped.
  * Severity by rule   -- taken from the catalog, then capped by confidence.
  * Stable IDs         -- deterministic ordering, zero-padded F-### ids.
"""

from __future__ import annotations

import os
import sys
from typing import Dict, List, Tuple

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_LIB = os.path.join(_ROOT, "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

from audit_common.catalog import CheckCatalog  # noqa: E402
from audit_common.schema import (  # noqa: E402
    Confidence,
    Finding,
    Observation,
    Severity,
    Status,
)
from audit_common.semantics import apply_severity_gate, should_emit_finding  # noqa: E402


# Confidence caps the maximum severity a finding may carry.
_CONFIDENCE_CAP = {
    Confidence.HIGH: Severity.CRITICAL,
    Confidence.MEDIUM: Severity.HIGH,
    Confidence.LOW: Severity.MEDIUM,
}


def _cap_severity(sev: Severity, conf: Confidence, relevance: str = "high") -> Severity:
    sev, _conf = apply_severity_gate(sev, conf, relevance)
    return sev


def _evidence(obs: Observation, sentence: str) -> Dict:
    return {
        "sentence": sentence,
        "method": obs.method.value,
        "scope": obs.selection or (", ".join(obs.urls[:3]) if obs.urls else ""),
        "metric": obs.metric,
        "artifacts": obs.artifacts,
    }


def _synth_sentence(obs: Observation, title: str) -> str:
    if obs.evidence_sentence:
        return obs.evidence_sentence
    metric = ", ".join(f"{k}={v}" for k, v in obs.metric.items())
    scope = f" ({obs.selection})" if obs.selection else ""
    return f"{title}{scope}. {metric}".strip()


def compile_findings(
    observations: List[Observation], catalog: CheckCatalog
) -> Tuple[List[Finding], List[Dict[str, str]]]:
    limitations: List[Dict[str, str]] = []
    fails: List[Observation] = []

    for obs in observations:
        if not catalog.has(obs.check_id):
            # Ungrounded finding type -> never emit (closed taxonomy).
            continue
        if obs.status == Status.INCONCLUSIVE:
            limitations.append(
                {
                    "check_id": obs.check_id,
                    "reason": obs.evidence_sentence or "check could not be completed",
                }
            )
        elif obs.status == Status.FAIL:
            if should_emit_finding(getattr(obs, "semantic_relevance", "high")):
                fails.append(obs)
        # pass / not_applicable / out-of-scope are intentionally dropped

    # Deterministic ordering: severity desc, then check_id, then first url.
    def sort_key(obs: Observation):
        spec = catalog.get(obs.check_id)
        sev = _cap_severity(
            spec.default_severity, obs.confidence,
            getattr(obs, "semantic_relevance", "high"),
        )
        return (-sev.rank, obs.check_id, obs.urls[0] if obs.urls else "")

    fails.sort(key=sort_key)

    findings: List[Finding] = []
    for i, obs in enumerate(fails, start=1):
        spec = catalog.get(obs.check_id)
        sev, conf = apply_severity_gate(
            spec.default_severity, obs.confidence,
            getattr(obs, "semantic_relevance", "high"),
        )
        title = spec.title
        findings.append(
            Finding(
                id=f"F-{i:03d}",
                check_id=obs.check_id,
                concern=spec.concern,
                mechanism=spec.mechanism,
                title=title,
                severity=sev,
                confidence=conf,
                evidence=_evidence(obs, _synth_sentence(obs, title)),
                resource_type=getattr(obs, "resource_type", "") or "",
                page_type=getattr(obs, "page_type", "") or "",
                entity_scope=getattr(obs, "entity_scope", "") or "",
                semantic_relevance=getattr(obs, "semantic_relevance", "high") or "high",
                evidence_tier=getattr(obs, "evidence_tier", "") or "",
            )
        )
    return findings, limitations
