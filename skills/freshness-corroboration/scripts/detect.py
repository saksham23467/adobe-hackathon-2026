"""freshness-corroboration detector entrypoint.

Identifies high-value on-site claims, checks freshness and self-consistency
without leaving the origin, then (only if a corroboration backend and budget are
available) corroborates the brand entity against independent sources. Strictly
fail-soft: any missing capability degrades to inconclusive, never a false finding.
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
from typing import List

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_LIB = os.path.join(_ROOT, "lib")
for _p in (_HERE, _LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import corroborate  # noqa: E402
import freshdetect  # noqa: E402
from audit_common.schema import Observation  # noqa: E402


def detect(ctx) -> List[Observation]:
    records = ctx.cache.all() if getattr(ctx, "cache", None) is not None else []
    records = [r for r in records if (r.rendered_html or r.raw_html)]
    if not records:
        return []

    cfg = getattr(ctx, "config", {}) or {}
    stale_days = int(cfg.get("freshness_stale_days", 1095))

    corr = None
    try:
        corr = corroborate.build_corroborator(ctx)
    except Exception:
        corr = None

    import urllib.parse
    host = urllib.parse.urlsplit(getattr(ctx, "entry_url", "") or "").netloc
    return freshdetect.run_all(
        records, corr=corr, now=_dt.date.today(), stale_days=stale_days, site_host=host,
    )
