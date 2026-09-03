"""Which pages look like script-driven shells in the curl HTML.

Used to rank empty-shell candidates. No browser is started. Pages that already
ship substantial text in the HTTP body are not treated as shells.

Pure, deterministic, dependency-free: safe to unit-test and reuse across skills.
"""

from __future__ import annotations

from typing import List

_PRIORITY_CLASSES = ("home", "product", "pricing", "about", "service")


def _tokens(text: str) -> int:
    return len((text or "").split())


def needs_render(record, *, min_text_tokens: int = 50) -> bool:
    """True if rendering this page could plausibly reveal content the initial
    HTTP response hides (a script-driven shell with little static text)."""
    raw_html = getattr(record, "raw_html", "") or ""
    if not raw_html:
        return False
    # Already rendered elsewhere -> no need to render again.
    if getattr(record, "rendered_html", ""):
        return False
    tokens = _tokens(getattr(record, "raw_text", ""))
    if tokens >= min_text_tokens:
        return False  # substantial static text: rendering cannot change findings
    has_scripts = "<script" in raw_html.lower()
    # Shell: little/no static text AND script-driven, or an almost-empty body.
    return (has_scripts and tokens < min_text_tokens) or tokens < 10


def select_for_render(records, max_render: int, *, min_text_tokens: int = 50) -> List:
    """Return the bounded, prioritized list of pages a renderer should process.

    Deterministic: important page-classes first, then by URL. Capped at
    ``max_render`` so rendering can never blow the time budget."""
    if max_render <= 0:
        return []
    candidates = [r for r in records if needs_render(r, min_text_tokens=min_text_tokens)]

    def _rank(r):
        cls = getattr(r, "page_class", "other")
        pri = _PRIORITY_CLASSES.index(cls) if cls in _PRIORITY_CLASSES else len(_PRIORITY_CLASSES)
        return (pri, getattr(r, "url", ""))

    candidates.sort(key=_rank)
    return candidates[:max_render]
