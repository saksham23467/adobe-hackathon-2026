"""Shared library for the brand-ai-readiness-audit marketplace.

This package is the single source of truth for the cross-skill contract:
the finding schema, the read-only safety gate, the acquire-once cache, the
page-class sampler, and the closed check catalog loader.

Design invariants (see README.md and references/):
  * Acquire once, analyze many  -- one network pass, immutable cache.
  * Detectors decide, LLM narrates -- findings are minted only by predicates.
  * Three-valued detectors -- pass | fail | inconclusive | not_applicable.
  * Closed taxonomy -- every finding.check_id must exist in the catalog.

The library depends only on the Python standard library so the marketplace
stays portable and self-contained.
"""

from .schema import (
    Concern,
    Method,
    Severity,
    Status,
    Confidence,
    Observation,
    Finding,
    SuggestedAction,
    new_report,
    validate_report,
)
from .safety import SafetyConfig, SafetyGate, normalize_url
from .catalog import CheckCatalog, load_catalog

__all__ = [
    "Concern",
    "Method",
    "Severity",
    "Status",
    "Confidence",
    "Observation",
    "Finding",
    "SuggestedAction",
    "new_report",
    "validate_report",
    "SafetyConfig",
    "SafetyGate",
    "normalize_url",
    "CheckCatalog",
    "load_catalog",
]

__version__ = "1.0.0"
