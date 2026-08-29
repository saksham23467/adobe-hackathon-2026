"""Loader for the closed check catalog (config/check_catalog.json).

The catalog is the single source of truth for the finding taxonomy. A finding
may only be minted for a check_id that exists here. Each entry carries its
concern, the Round-2 mechanism (A..F) it maps to, a default severity, and a
default confidence -- so severity is assigned by rule, not by an LLM's mood.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, Optional

from .schema import Concern, Confidence, Severity


@dataclass
class CheckSpec:
    check_id: str
    concern: Concern
    mechanism: str
    default_severity: Severity
    default_confidence: Confidence
    title: str
    description: str

    @classmethod
    def from_dict(cls, data: Dict) -> "CheckSpec":
        return cls(
            check_id=data["check_id"],
            concern=Concern(data["concern"]),
            mechanism=data["mechanism"],
            default_severity=Severity(data["default_severity"]),
            default_confidence=Confidence(data["default_confidence"]),
            title=data.get("title", data["check_id"]),
            description=data.get("description", ""),
        )


class CheckCatalog:
    def __init__(self, version: str, checks: Dict[str, CheckSpec]) -> None:
        self.version = version
        self._checks = checks

    def has(self, check_id: str) -> bool:
        return check_id in self._checks

    def get(self, check_id: str) -> Optional[CheckSpec]:
        return self._checks.get(check_id)

    def __len__(self) -> int:
        return len(self._checks)

    def ids(self):
        return list(self._checks.keys())


def _default_catalog_path() -> str:
    here = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(here, "config", "check_catalog.json")


def load_catalog(path: Optional[str] = None) -> CheckCatalog:
    path = path or _default_catalog_path()
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    checks = {}
    for entry in data.get("checks", []):
        spec = CheckSpec.from_dict(entry)
        checks[spec.check_id] = spec
    return CheckCatalog(version=data.get("version", "0"), checks=checks)
