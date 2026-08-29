"""structured-identity detector entrypoint.

Builds an entity graph from JSON-LD / microdata / RDFa across the acquire-once
cache and runs the structured-data + entity-identity detectors. Schema presence
is never assumed good and schema absence alone is never flagged; every finding
ties structured data to a concrete discoverability, extraction, attribution, or
ambiguity problem.
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

import identitydetect  # noqa: E402
from audit_common.schema import Observation  # noqa: E402


def detect(ctx) -> List[Observation]:
    records = ctx.cache.all() if getattr(ctx, "cache", None) is not None else []
    records = [r for r in records if (r.rendered_html or r.raw_html)]
    if not records:
        return []
    return identitydetect.run_all(records)
