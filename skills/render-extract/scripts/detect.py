"""render-extract detector entrypoint.

Compares representations of each page from the acquire-once cache:

  A. curl-fetched HTTP HTML     (record.raw_html) — the live path
  B. optional comparison HTML   (record.rendered_html) — fixtures only
  C. visible text               (hidden/a11y-removed nodes stripped)
  D. semantic representation    (headings, JSON-LD)

Live audits never run a browser (Playwright/Selenium are not used). Empty
shells, canvas, and image-only pages are detected from the curl HTML.
Fixture tests may still supply a second HTML snapshot to compare JS-added
text.
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
    compared = [r for r in records if r.rendered_html and r.raw_html]
    raw_only = [r for r in records if r.raw_html and not r.rendered_html]

    out: List[Observation] = []
    if compared:
        out.extend(renderdetect.run_all(compared))
    if raw_only:
        out.extend(renderdetect.run_raw_http(raw_only))
    if out:
        return out
    if not any(getattr(r, "raw_html", "") for r in records):
        return [
            Observation(
                check_id="render.primary_content_js_only",
                concern=Concern.RENDER,
                method=Method.HTTP,
                status=Status.INCONCLUSIVE,
                confidence=Confidence.MEDIUM,
                evidence_sentence=(
                    "No HTTP HTML was available; machine-readability of the "
                    "page body could not be evaluated."
                ),
            )
        ]
    return []
