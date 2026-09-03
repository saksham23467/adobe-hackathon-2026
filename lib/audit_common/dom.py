"""Shared DOM attribute helpers (hidden-state, name matching)."""

from __future__ import annotations

import re
import unicodedata
from typing import Dict

_NAME_STOP = frozenset({"and", "or", "the", "a", "an", "of", "for", "with", "to"})

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


def _fold_spaces(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"[\u00a0\u202f\u2007\s]+", " ", s).strip()


def name_mentioned(name: str, text: str) -> bool:
    """True if `name` appears in text as a phrase or as all of its tokens.

    Unicode spaces (nbsp) are folded so 'MacBook\\xa0Pro' matches 'MacBook Pro'.
    Multi-word schema names match when every significant token is visible
    (e.g. 'MacBook Air 13-inch and 15-inch' vs copy that splits the sizes).
    """
    if not name or not text or len(name.strip()) < 2:
        return False
    name_n = _fold_spaces(name)
    text_n = _fold_spaces(text)
    if re.search(r"\b" + re.escape(name_n) + r"\b", text_n, re.I):
        return True
    tokens = [
        t for t in re.findall(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)?", name_n)
        if t.lower() not in _NAME_STOP and len(t) > 1
    ]
    if len(tokens) < 2:
        return False
    return all(re.search(r"\b" + re.escape(t) + r"\b", text_n, re.I) for t in tokens)
