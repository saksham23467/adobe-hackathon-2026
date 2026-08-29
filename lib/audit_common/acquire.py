"""Acquire-once cache. One network pass; every skill reads from here.

A ``PageRecord`` holds everything downstream detectors need for one URL:
raw HTTP body + headers, and (when a renderer is available) the rendered DOM
and extracted text. Detectors must never re-fetch; they read the cache.

Two acquisition backends:
  * LiveAcquirer   -- stdlib urllib GET, robots-gated, capped, polite.
  * FixtureAcquirer-- reads a local fixture directory for hermetic tests.

Rendering (headless browser) is optional. If no renderer is installed the
render fields stay empty and render-dependent checks degrade to
``inconclusive`` rather than emitting false findings.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional

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
    rendered_html: str = ""  # populated only if a renderer ran
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
    """Fetch real pages over HTTP(S), read-only and robots-gated."""

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
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"User-Agent": self.gate.config.user_agent, "Accept": "text/html,*/*"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.gate.config.request_timeout_s) as resp:
                rec.status_code = getattr(resp, "status", 200)
                rec.final_url = resp.geturl()
                rec.headers = {k.lower(): v for k, v in resp.headers.items()}
                raw = resp.read(self.gate.config.max_bytes_per_page)
                charset = resp.headers.get_content_charset() or "utf-8"
                rec.raw_html = raw.decode(charset, errors="replace")
                rec.raw_text = _strip_html(rec.raw_html)
                rec.fetched = True
        except urllib.error.HTTPError as e:
            rec.status_code = e.code
            rec.error = f"http_error_{e.code}"
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
