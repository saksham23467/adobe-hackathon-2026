"""fact-accessibility detector entrypoint (content extractability).

Determines whether important brand facts are easy for a machine to identify,
extract, attribute, and quote. Runs the deterministic + heuristic pipeline in
`factextract` / `factdetect` over the acquire-once cache and returns
mechanism-specific, evidence-backed Observations.
"""

from __future__ import annotations

import os
import sys
from typing import List

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_LIB = os.path.join(_ROOT, "lib")
for _p in (_HERE, _LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import factdetect  # noqa: E402
from audit_common.schema import Observation  # noqa: E402


def detect(ctx) -> List[Observation]:
    records = ctx.cache.all() if getattr(ctx, "cache", None) is not None else []
    records = [r for r in records if (r.rendered_html or r.raw_html)]
    if not records:
        return []
    return factdetect.run_all(records)
