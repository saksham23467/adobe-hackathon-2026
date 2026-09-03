"""Acquire-once cache. One network pass; every skill reads from here.

A ``PageRecord`` holds everything downstream detectors need for one URL:
raw HTTP body + headers, and (when a renderer is available) the rendered DOM
and extracted text. Detectors must never re-fetch; they read the cache.

Two acquisition backends:
  * LiveAcquirer   -- curl GET (urllib fallback), robots-gated, capped, polite.
  * FixtureAcquirer-- reads a local fixture directory for hermetic tests.

No headless browser (Playwright/Selenium are not used). Downstream skills
read the curl-fetched HTML. Render-gap checks that need a JS-executed DOM
are not run; empty shells and non-text payloads are detected from that HTML.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .curlfetch import http_get
from .safety import SafetyGate


@dataclass
class PageRecord:
    url: str
    page_class: str = "other"
    status_code: int = 0
    final_url: str = ""
    redirect_chain: List[str] = field(default_factory=list)
    headers: Dict[str, str] = field(default_factory=dict)
    raw_html: str = ""
    raw_text: str = ""  # naive text extracted from raw_html (no JS)
    rendered_html: str = ""  # fixture-only comparison HTML; never from a browser
    rendered_text: str = ""
    fetched: bool = False
    error: str = ""
    resource_type: str = ""
    page_type: str = ""

    def to_dict(self) -> Dict:
        d = dict(self.__dict__)
        # Trim large blobs when serializing the cache index for debugging.
        return d


class Cache:
    """In-memory (optionally disk-backed) store of PageRecords by URL."""

    def __init__(self, workdir: Optional[str] = None) -> None:
        self._records: Dict[str, PageRecord] = {}
        self._workdir = workdir
        if workdir:
            os.makedirs(workdir, exist_ok=True)

    def put(self, record: PageRecord) -> None:
        self._records[record.url] = record

    def get(self, url: str) -> Optional[PageRecord]:
        return self._records.get(url)

    def all(self) -> List[PageRecord]:
        return list(self._records.values())

    def by_class(self, page_class: str) -> List[PageRecord]:
        return [r for r in self._records.values() if r.page_class == page_class]

    def __len__(self) -> int:
        return len(self._records)


_NEXT_RE = re.compile(
    r'(?is)<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>'
)
_NUXT_RE = re.compile(
    r'(?is)<script[^>]+id=["\']__NUXT_DATA__["\'][^>]*>(.*?)</script>'
)
_JSON_SCRIPT_RE = re.compile(
    r'(?is)<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>'
)


def _json_strings(blob: str, limit: int = 80) -> str:
    try:
        data = json.loads(blob)
    except Exception:
        return ""
    out: List[str] = []

    def walk(node) -> None:
        if len(out) >= limit:
            return
        if isinstance(node, str):
            s = node.strip()
            if len(s) >= 12 and not s.startswith(("http://", "https://", "/")):
                out.append(s)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node[:40]:
                walk(v)

    walk(data)
    return " ".join(out)


def extract_embedded_state(html: str) -> str:
    """Pull visible-ish strings from Next/Nuxt/JSON script payloads.

    Used as a no-browser fallback so JS apps are not scored as empty shells
    when the initial HTML already contains the serialized app state.
    """
    if not html:
        return ""
    parts = []
    for rx in (_NEXT_RE, _NUXT_RE, _JSON_SCRIPT_RE):
        for m in rx.finditer(html):
            text = _json_strings(m.group(1))
            if text:
                parts.append(text)
    return " ".join(parts)


def _strip_html(html: str) -> str:
    """Extremely small, dependency-free text extraction from HTML.

    This is deliberately naive. Real extraction lives in the
    render-extract skill. It removes script/style blocks and tags.
    """
    import re

    if not html:
        return ""
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


class LiveAcquirer:
    """Fetch real pages over HTTP(S) with curl, read-only and robots-gated."""

    def __init__(self, gate: SafetyGate) -> None:
        self.gate = gate

    def fetch(self, url: str, page_class: str = "other") -> PageRecord:
        rec = PageRecord(url=url, page_class=page_class)
        self.gate.assert_readonly("GET")
        if self.gate.config.respect_robots and not self.gate.can_fetch(url):
            rec.error = "robots_disallow_our_ua"
            return rec
        if not self.gate.note_page_fetch():
            rec.error = "page_cap_reached"
            return rec
        host = urllib.parse.urlsplit(url).netloc
        self.gate.rate.wait(host)
        try:
            got = http_get(
                url,
                user_agent=self.gate.config.user_agent,
                timeout_s=self.gate.config.request_timeout_s,
                max_bytes=self.gate.config.max_bytes_per_page,
                accept="text/html,*/*",
            )
            rec.status_code = got.status
            rec.final_url = got.final_url or url
            rec.headers = got.headers
            rec.raw_html = got.body
            rec.raw_text = _strip_html(rec.raw_html)
            rec.fetched = bool(got.body) or (200 <= got.status < 400)
            if got.error:
                rec.error = got.error
            elif got.status >= 400:
                rec.error = f"http_error_{got.status}"
        except Exception as e:  # noqa: BLE001 -- record, never crash the audit
            rec.error = f"fetch_error:{type(e).__name__}"
        return rec


class FixtureAcquirer:
    """Load a local fixture directory as if it were a live site.

    Fixture layout:
        <fixture>/pages.json   # [{"url","file","page_class","rendered_file"?,
                               #    "status"?, "headers"?}, ...]
        <fixture>/*.html
    This keeps the end-to-end smoke test hermetic (no network).
    """

    def __init__(self, fixture_dir: str) -> None:
        self.dir = fixture_dir
        index_path = os.path.join(fixture_dir, "pages.json")
        with open(index_path, "r", encoding="utf-8") as fh:
            self.index = json.load(fh)

    def _read(self, name: str) -> str:
        if not name:
            return ""
        with open(os.path.join(self.dir, name), "r", encoding="utf-8") as fh:
            return fh.read()

    def pages(self) -> List[Dict]:
        return list(self.index.get("pages", []))

    def robots_txt(self) -> str:
        return self._read(self.index.get("robots_file", "")) if self.index.get("robots_file") else ""

    def fetch(self, entry: Dict) -> PageRecord:
        raw = self._read(entry.get("file", ""))
        rendered = self._read(entry.get("rendered_file", "")) if entry.get("rendered_file") else ""
        rec = PageRecord(
            url=entry["url"],
            page_class=entry.get("page_class", "other"),
            status_code=int(entry.get("status", 200)),
            final_url=entry["url"],
            headers={k.lower(): v for k, v in (entry.get("headers") or {}).items()},
            raw_html=raw,
            raw_text=_strip_html(raw),
            rendered_html=rendered,
            rendered_text=_strip_html(rendered) if rendered else "",
            fetched=True,
        )
        try:
            from .semantics import classify_record
            cls = classify_record(rec)
            rec.resource_type = cls.resource_type
            rec.page_type = cls.page_type
        except Exception:
            pass
        return rec
