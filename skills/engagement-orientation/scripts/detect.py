"""engagement-orientation detector entrypoint.

Builds the visitor journey model from the shared cache and runs the tiered
engagement detectors (arrival → orientation → understanding → evaluation →
next action). Read-only; deterministic; standard library only.
"""

from __future__ import annotations

import os
import sys
from typing import List

_HERE = os.path.dirname(__file__)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
_LIB = os.path.join(_ROOT, "lib")
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import engagedetect  # noqa: E402
from audit_common.schema import Observation  # noqa: E402


def detect(ctx) -> List[Observation]:
    records = ctx.cache.all() if getattr(ctx, "cache", None) is not None else []
    records = [r for r in records if (r.rendered_html or r.raw_html)]
    try:
        from audit_common.semantics import classify_record
        records = [r for r in records if classify_record(r).is_human_facing_page]
    except Exception:
        pass
    if not records:
        return []
    return engagedetect.run_all(records)
