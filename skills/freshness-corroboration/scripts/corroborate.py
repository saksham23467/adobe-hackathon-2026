"""Conservative, fail-soft external corroboration.

This module is the *only* part of the marketplace that leaves the origin, and it
does so under strict rules:

  * At most one entity lookup per audit (the brand), counted against the
    external-call budget on the SafetyGate.
  * Every network error, timeout, or ambiguous result degrades to "no result"
    (``matched=False``) — never an invented corroboration and never a finding.
  * Absence of coverage is never treated as evidence of a problem.

Two backends implement the same tiny interface:
  * ``LiveCorroborator``   — Wikidata ``wbsearchentities`` + entity claims.
  * ``FixtureCorroborator``— reads ``corroboration.json`` from a fixture dir so
    tests are fully hermetic.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class CorroborationResult:
    matched: bool = False
    confidence: str = "low"          # high | low
    label: str = ""
    candidate_count: int = 0
    prominent_candidates: int = 0
    claims: Dict[str, str] = field(default_factory=dict)  # founding_year, ceo, hq
    source_url: str = ""


class FixtureCorroborator:
    """Reads canned corroboration keyed by brand name (hermetic tests)."""

    def __init__(self, path: str) -> None:
        with open(path, "r", encoding="utf-8") as fh:
            self.data = json.load(fh)

    def lookup(self, name: str, site_host: str = "", language: str = "en") -> Optional[CorroborationResult]:
        entry = self.data.get(name) or self.data.get((name or "").lower())
        if entry is None:
            return CorroborationResult(matched=False)
        return CorroborationResult(
            matched=bool(entry.get("matched", False)),
            confidence=entry.get("confidence", "low"),
            label=entry.get("label", name),
            candidate_count=int(entry.get("candidate_count", 0)),
            prominent_candidates=int(entry.get("prominent_candidates", 0)),
            claims={k: str(v) for k, v in (entry.get("claims") or {}).items()},
            source_url=entry.get("source_url", ""),
        )


class LiveCorroborator:
    """Wikidata-backed lookup. Read-only, budgeted, fail-soft."""

    _API = "https://www.wikidata.org/w/api.php"

    def __init__(self, gate) -> None:
        self.gate = gate

    def _get(self, params: Dict[str, str]) -> Optional[dict]:
        if not self.gate.note_external_call():
            return None
        url = self._API + "?" + urllib.parse.urlencode(params)
        import time as _time
        _t0 = _time.monotonic()
        try:
            self.gate.assert_readonly("GET")
            req = urllib.request.Request(
                url, method="GET", headers={"User-Agent": self.gate.config.user_agent})
            with urllib.request.urlopen(req, timeout=self.gate.config.request_timeout_s) as resp:
                return json.loads(resp.read(1_000_000).decode("utf-8", errors="replace"))
        except Exception:
            return None
        finally:
            try:
                self.gate.add_external_time(_time.monotonic() - _t0)
            except Exception:
                pass

    def lookup(self, name: str, site_host: str = "", language: str = "en") -> Optional[CorroborationResult]:
        if not name:
            return CorroborationResult(matched=False)
        search = self._get({
            "action": "wbsearchentities", "search": name, "language": language or "en",
            "format": "json", "type": "item", "limit": "5",
        })
        if search is None:
            return None  # network failure -> inconclusive upstream
        hits = search.get("search", []) or []
        if not hits:
            return CorroborationResult(matched=False, candidate_count=0)
        prominent = sum(1 for h in hits if h.get("description"))
        ids = "|".join(h.get("id", "") for h in hits if h.get("id"))
        ent = self._get({
            "action": "wbgetentities", "ids": ids,
            "format": "json", "props": "claims|labels",
        }) if ids else None
        entities = (ent or {}).get("entities") or {}
        chosen = None
        host = (site_host or "").lower().split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        for h in hits:
            e = entities.get(h.get("id", ""), {})
            official = _official_hosts(e.get("claims", {}))
            if host and any(host == o or host.endswith("." + o) or o.endswith("." + host) for o in official):
                chosen = (h, e)
                break
        if chosen is None:
            # Label match alone is not identity. Refuse to corroborate.
            return CorroborationResult(
                matched=False,
                label=hits[0].get("label", name),
                candidate_count=len(hits),
                prominent_candidates=prominent,
            )
        top, entity = chosen
        claims = _normalize_claims(entity.get("claims", {}))
        exact = top.get("label", "").strip().lower() == name.strip().lower()
        return CorroborationResult(
            matched=True,
            confidence="high" if exact else "low",
            label=top.get("label", name),
            candidate_count=len(hits),
            prominent_candidates=prominent,
            claims=claims,
            source_url=f"https://www.wikidata.org/wiki/{top.get('id', '')}",
        )


def _official_hosts(claims: dict) -> List[str]:
    hosts: List[str] = []
    try:
        for snak in claims.get("P856", []) or []:
            url = (((snak.get("mainsnak") or {}).get("datavalue") or {}).get("value")) or ""
            host = urllib.parse.urlsplit(str(url)).netloc.lower().split(":")[0]
            if host.startswith("www."):
                host = host[4:]
            if host:
                hosts.append(host)
    except Exception:
        pass
    return hosts


def _normalize_claims(claims: dict) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        p571 = claims.get("P571", [])
        if p571:
            t = p571[0]["mainsnak"]["datavalue"]["value"]["time"]
            m = re.search(r"(\d{4})", t)
            if m:
                out["founding_year"] = m.group(1)
    except Exception:
        pass
    try:
        p169 = claims.get("P169", [])
        if p169:
            # CEO is usually a wikibase-entityid; store the id, not a hallucinated name.
            dv = p169[0]["mainsnak"]["datavalue"]["value"]
            if isinstance(dv, dict) and dv.get("id"):
                out["ceo"] = dv["id"]
            elif isinstance(dv, str):
                out["ceo"] = dv
    except Exception:
        pass
    return out


def build_corroborator(ctx):
    """Pick a backend. Fixture (hermetic) wins; else live if budget allows."""
    fixture_dir = getattr(ctx, "fixture_dir", None)
    if fixture_dir:
        path = os.path.join(fixture_dir, "corroboration.json")
        if os.path.exists(path):
            try:
                return FixtureCorroborator(path)
            except Exception:
                return None
        return None  # a fixture run without canned data stays hermetic (no network)
    budget = int(getattr(ctx, "config", {}).get("max_external_calls", 0) or 0)
    gate = getattr(ctx, "gate", None)
    if budget > 0 and gate is not None and not gate.budget_exhausted():
        return LiveCorroborator(gate)
    return None
