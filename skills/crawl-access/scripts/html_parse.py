"""Minimal, dependency-free HTML parsing for crawl signals.

Extracts only what the crawler needs: anchors, canonical, meta robots, base
href, rel=next/prev, and title. Uses html.parser from the standard library.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import List, Optional


@dataclass
class ParsedHtml:
    hrefs: List[str] = field(default_factory=list)
    canonical: Optional[str] = None
    meta_robots: str = ""
    base_href: Optional[str] = None
    rel_next: Optional[str] = None
    rel_prev: Optional[str] = None
    title: str = ""


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out = ParsedHtml()
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a" and "href" in a:
            self.out.hrefs.append(a["href"].strip())
        elif tag == "base" and "href" in a and self.out.base_href is None:
            self.out.base_href = a["href"].strip()
        elif tag == "link":
            rel = a.get("rel", "").lower()
            href = a.get("href", "").strip()
            if "canonical" in rel and self.out.canonical is None:
                self.out.canonical = href
            if "next" in rel and not self.out.rel_next:
                self.out.rel_next = href
            if "prev" in rel and not self.out.rel_prev:
                self.out.rel_prev = href
        elif tag == "meta":
            name = a.get("name", "").lower()
            if name == "robots":
                self.out.meta_robots = a.get("content", "").lower().strip()
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title and data.strip():
            self.out.title = (self.out.title + " " + data.strip()).strip()


def parse_html(body: str) -> ParsedHtml:
    parser = _Collector()
    try:
        parser.feed(body or "")
    except Exception:
        # Malformed HTML: return whatever we collected so far.
        pass
    return parser.out
