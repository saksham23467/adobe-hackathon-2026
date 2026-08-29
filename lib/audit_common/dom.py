"""Shared DOM attribute helpers (hidden-state, name matching)."""

from __future__ import annotations

import re
from typing import Dict

_HIDDEN_CLASS = re.compile(
    r"\b(hidden|sr-only|visually-hidden|visuallyhidden|d-none|invisible|"
    r"collapse|opacity-0|screen-reader-only|w-0|h-0|clip-hidden)\b",
    re.I,
)


def attrs_hidden(a: Dict[str, str]) -> bool:
    if "hidden" in a:
        return True
    if (a.get("aria-hidden") or "").lower() == "true":
        return True
    style = (a.get("style") or "").replace(" ", "").lower()
    if "display:none" in style or "visibility:hidden" in style:
        return True
    if _HIDDEN_CLASS.search(a.get("class") or ""):
        return True
    return False


def name_mentioned(name: str, text: str) -> bool:
    """True if `name` appears as a whole token in text (not a substring)."""
    if not name or not text or len(name.strip()) < 2:
        return False
    return bool(re.search(r"\b" + re.escape(name.strip()) + r"\b", text, re.I))
