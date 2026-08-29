"""render-extract detector entrypoint.

Compares four representations per page:
  A. initial HTTP response      (record.raw_html)
  B. browser-rendered DOM       (record.rendered_html, full DOM text)
  C. visible rendered text      (rendered DOM minus hidden/inaccessible nodes)
  D. semantic representation    (headings, JSON-LD)

and emits render findings ONLY where JavaScript materially reduces machine
accessibility. If no rendered DOM is available (no headless browser was run),
the render gap is reported as `inconclusive` — never a false finding.
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

import renderdetect  # noqa: E402
from audit_common.schema import (  # noqa: E402
    Concern,
    Confidence,
    Method,
    Observation,
    Status,
)


def detect(ctx) -> List[Observation]:
    records = ctx.cache.all() if getattr(ctx, "cache", None) is not None else []
    rendered_records = [r for r in records if r.rendered_html and r.raw_html]

    if not rendered_records:
        return [
            Observation(
                check_id="render.primary_content_js_only",
                concern=Concern.RENDER,
                method=Method.RENDER,
                status=Status.INCONCLUSIVE,
                confidence=Confidence.MEDIUM,
                evidence_sentence=(
                    "No rendered DOM was available (headless renderer not installed); "
                    "the JavaScript render gap could not be evaluated."
                ),
            )
        ]

    return renderdetect.run_all(rendered_records)
