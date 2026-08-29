"""Shared, computed-once page artifacts reused across skills.

The acquire-once :class:`Cache` already guarantees every URL is fetched exactly
once and shared by all six detection skills. This module extends that principle
to *derived* artifacts: the naive visible-text extraction is computed a single
time (at acquisition) and stored on the ``PageRecord``; skills read it via
``visible_text`` instead of re-stripping the same HTML repeatedly.

``shared_strip`` is a small, bounded, memoized fallback for the rare path where a
record has HTML but no precomputed text. It is intentionally byte-identical to
``audit_common.acquire._strip_html`` so switching a skill onto it never changes a
finding.
"""

from __future__ import annotations

import functools
import re

_SCRIPT_RE = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")
_WS_RE = re.compile(r"\s+")


@functools.lru_cache(maxsize=64)
def shared_strip(html: str) -> str:
    """Naive HTML -> visible text. Memoized so repeated calls on the same page
    (across functions/skills) are free. Bounded cache keeps memory in check."""
    if not html:
        return ""
    h = _SCRIPT_RE.sub(" ", html)
    h = _TAG_RE.sub(" ", h)
    return _WS_RE.sub(" ", h).strip()


def visible_text(record) -> str:
    """Return the page's visible text, preferring the copy extracted once at
    acquisition (``rendered_text``/``raw_text``) over re-stripping HTML."""
    t = getattr(record, "rendered_text", "") or getattr(record, "raw_text", "")
    if t:
        return t
    html = getattr(record, "rendered_html", "") or getattr(record, "raw_html", "")
    return shared_strip(html)
