"""Dependency-free extraction of the four representations we compare.

For any HTML document we derive:
  * text_all      -- all text nodes (scripts/styles removed). Approximates the
                     full DOM text (representation B when applied to rendered HTML).
  * text_visible  -- text_all minus content inside elements that are hidden or
                     removed from the accessibility tree (hidden attr,
                     aria-hidden, inline display:none / visibility:hidden).
                     Approximates the visible rendered text (representation C).
  * headings      -- h1..h3 outline (part of the semantic representation D).
  * jsonld        -- schema.org JSON-LD types (also part of D).
  * images/canvas -- non-text carriers of meaning.
  * script/mount  -- signals of client-side rendering (never a defect by itself).

Applied to the *initial HTTP response* this yields representation A (no JS run).
Applied to the *rendered DOM* it yields B / C / D. The detectors compare them.

Standard library only (html.parser). This is intentionally conservative: it is a
static approximation of a browser, tuned to avoid false positives.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Tuple

_SKIP_TAGS = {"script", "style", "noscript", "template"}
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}
_MOUNT_IDS = {"root", "app", "__next", "__nuxt", "application", "app-root", "___gatsby"}
_HEADING_TAGS = {"h1", "h2", "h3"}
_ICON_HINTS = ("icon", "logo", "sprite", "avatar", "pixel", "spacer")


def tokens(text: str) -> int:
    return len([t for t in (text or "").split() if t.strip()])


@dataclass
class PageExtract:
    text_all: str = ""
    text_visible: str = ""
    headings: List[Tuple[str, str]] = field(default_factory=list)
    jsonld_types: List[str] = field(default_factory=list)
    jsonld_count: int = 0
    images: List[Dict[str, str]] = field(default_factory=list)
    canvas_count: int = 0
    script_count: int = 0
    script_src_count: int = 0
    has_mount: bool = False
    title: str = ""

    # -- derived ---------------------------------------------------------
    @property
    def all_tokens(self) -> int:
        return tokens(self.text_all)

    @property
    def visible_tokens(self) -> int:
        return tokens(self.text_visible)

    @property
    def hidden_tokens(self) -> int:
        return max(0, self.all_tokens - self.visible_tokens)

    def meaningful_images(self) -> List[Dict[str, str]]:
        out = []
        for img in self.images:
            src = (img.get("src") or "").lower()
            if any(h in src for h in _ICON_HINTS):
                continue
            w = _to_int(img.get("w"))
            h = _to_int(img.get("h"))
            # Treat as meaningful unless it is clearly a small icon.
            if (w and w < 64) or (h and h < 64):
                continue
            out.append(img)
        return out


def _to_int(v) -> int:
    try:
        return int(str(v).strip().replace("px", ""))
    except Exception:
        return 0


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out = PageExtract()
        self._stack: List[Dict] = []       # non-void, non-skip elements
        self._skip_stack: List[str] = []   # script/style/noscript/template
        self._hidden_depth = 0
        self._cur_heading = None
        self._heading_buf: List[str] = []
        self._in_ld = False
        self._ld_buf: List[str] = []
        self._in_title = False

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _is_hidden(a: Dict[str, str]) -> bool:
        try:
            from audit_common.dom import attrs_hidden
            return attrs_hidden(a)
        except Exception:
            if "hidden" in a:
                return True
            if a.get("aria-hidden", "").lower() == "true":
                return True
            style = a.get("style", "").replace(" ", "").lower()
            return "display:none" in style or "visibility:hidden" in style

    # -- tag events ------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        a = {(k or "").lower(): (v or "") for k, v in attrs}

        if tag == "script":
            self.out.script_count += 1
            if "src" in a:
                self.out.script_src_count += 1
            if a.get("type", "").lower() == "application/ld+json":
                self._in_ld = True
                self._ld_buf = []
            self._skip_stack.append(tag)
            return
        if tag in _SKIP_TAGS:
            self._skip_stack.append(tag)
            return
        if self._skip_stack:
            return

        if tag == "img":
            self.out.images.append({"alt": a.get("alt", ""), "src": a.get("src", ""),
                                    "w": a.get("width", ""), "h": a.get("height", "")})
        elif tag == "canvas":
            self.out.canvas_count += 1

        if tag == "title":
            self._in_title = True

        # SPA mount signals (never a defect by itself; a discriminator for shells).
        if a.get("id", "").lower() in _MOUNT_IDS or tag == "app-root" or "data-reactroot" in a \
                or "data-server-rendered" in a:
            self.out.has_mount = True

        if tag not in _VOID_TAGS:
            hidden = self._is_hidden(a)
            self._stack.append({"tag": tag, "hidden": hidden})
            if hidden:
                self._hidden_depth += 1

        if tag in _HEADING_TAGS:
            self._cur_heading = tag
            self._heading_buf = []

    def handle_startendtag(self, tag, attrs):
        # Self-closing element: fire start-only logic for the interesting ones.
        a = {(k or "").lower(): (v or "") for k, v in attrs}
        if tag == "img" and not self._skip_stack:
            self.out.images.append({"alt": a.get("alt", ""), "src": a.get("src", ""),
                                    "w": a.get("width", ""), "h": a.get("height", "")})

    def handle_endtag(self, tag):
        if self._skip_stack:
            if tag == self._skip_stack[-1]:
                popped = self._skip_stack.pop()
                if popped == "script" and self._in_ld:
                    self._in_ld = False
                    self._record_jsonld("".join(self._ld_buf))
            return

        if tag == "title":
            self._in_title = False

        if tag in _HEADING_TAGS and self._cur_heading:
            text = " ".join(self._heading_buf).strip()
            if text:
                self.out.headings.append((self._cur_heading, text))
            self._cur_heading = None

        # Pop back to the matching open tag (tolerate unclosed elements).
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i]["tag"] == tag:
                for e in self._stack[i:]:
                    if e["hidden"]:
                        self._hidden_depth -= 1
                del self._stack[i:]
                break

    def handle_data(self, data):
        if self._skip_stack:
            if self._in_ld:
                self._ld_buf.append(data)
            return
        if self._in_title:
            if data.strip():
                self.out.title = (self.out.title + " " + data.strip()).strip()
            return
        if self._cur_heading is not None:
            self._heading_buf.append(data)
        if data.strip():
            # Separate adjacent text nodes so tokens from different elements
            # (e.g. consecutive <a> links) are never merged into one word.
            self.out.text_all += " " + data
            if self._hidden_depth == 0:
                self.out.text_visible += " " + data

    # -- jsonld ----------------------------------------------------------
    def _record_jsonld(self, raw: str) -> None:
        raw = raw.strip()
        if not raw:
            return
        try:
            data = json.loads(raw)
        except Exception:
            self.out.jsonld_count += 1  # present but unparseable still counts as "present"
            return
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            self.out.jsonld_count += 1
            t = node.get("@type")
            if isinstance(t, list):
                self.out.jsonld_types.extend(str(x) for x in t)
            elif t:
                self.out.jsonld_types.append(str(t))


def extract(html: str) -> PageExtract:
    parser = _Extractor()
    try:
        parser.feed(html or "")
    except Exception:
        pass
    out = parser.out
    # Normalize whitespace in the collected text.
    out.text_all = " ".join(out.text_all.split())
    out.text_visible = " ".join(out.text_visible.split())
    return out
