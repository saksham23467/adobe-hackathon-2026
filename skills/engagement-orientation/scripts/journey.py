"""Model the visitor's information journey from raw HTML (deterministic).

We parse each page into a `PageView` that captures the observable signals a
first-time visitor relies on — orientation (heading/CTA), navigation, links
(with their structural context: nav / header / footer / main), mobile viewport,
blocking overlays, contact affordances, and whether orientation content is hidden
behind interactions. A site-level `JourneyModel` wires pages into a reachability
graph so we can reason about the stages:

    arrival → orientation → understanding → evaluation → next action

Standard library only (html.parser + urllib).
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional, Set, Tuple

_SKIP = {"script", "style", "noscript", "template"}
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "param", "source", "track", "wbr"}
_HEADINGS = {"h1", "h2", "h3"}

_ACTION_VERBS = (
    "get started", "sign up", "signup", "sign in", "log in", "buy", "shop",
    "order", "add to cart", "add to bag", "book", "request", "contact",
    "subscribe", "start free", "start now", "try", "download", "get a quote",
    "explore", "discover", "browse", "view plans", "see plans", "get demo",
    "book a demo", "join", "apply", "learn how",
)
_NON_DESCRIPTIVE = {"click here", "read more", "learn more", "more", "here",
                    "this", "link", "click", "go", "details", "read"}
_KNOWN_ACRONYMS = {"API", "FAQ", "FAQS", "SDK", "CRM", "SEO", "B2B", "B2C",
                   "SAAS", "HR", "IT", "AI", "ML", "ROI", "KPI", "PDF", "URL",
                   "USB", "UI", "UX", "OS", "VR", "AR", "CEO", "CTO", "IOT"}
_CONSENT_WORDS = ("cookie", "consent", "gdpr", "privacy", "we use cookies")


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


@dataclass
class Link:
    href: str
    text: str = ""
    aria_label: str = ""
    path: str = ""          # normalized same-site path, "" if external/non-nav
    external: bool = False
    in_nav: bool = False
    in_breadcrumb: bool = False
    in_header: bool = False
    in_footer: bool = False
    in_main: bool = False
    hidden: bool = False

    @property
    def label(self) -> str:
        return self.text or self.aria_label


@dataclass
class PageView:
    url: str
    page_class: str = "other"
    resource_type: str = "html_document"
    page_type: str = ""
    title: str = ""
    h1s: List[str] = field(default_factory=list)
    visible_h1s: List[str] = field(default_factory=list)
    headings: List[str] = field(default_factory=list)
    has_nav: bool = False
    has_breadcrumb: bool = False
    has_search: bool = False
    viewport: Optional[str] = None
    overlay: Dict[str, str] = field(default_factory=dict)   # {text, selector}
    links: List[Link] = field(default_factory=list)
    ctas: List[str] = field(default_factory=list)

    # -- derived helpers -----------------------------------------------
    @property
    def self_path(self) -> str:
        return urllib.parse.urlsplit(self.url).path or "/"

    @property
    def viewport_blocks_zoom(self) -> bool:
        v = (self.viewport or "").replace(" ", "").lower()
        return "user-scalable=no" in v or "maximum-scale=1" in v or "maximum-scale=1.0" in v

    def internal_links(self) -> List[Link]:
        return [l for l in self.links if l.path and not l.external]

    def content_links(self) -> List[Link]:
        return [l for l in self.internal_links() if not l.in_nav and not l.in_footer]

    def nav_dest_labels(self) -> List[Tuple[str, str]]:
        # Primary navigation only — breadcrumb trails legitimately relabel a
        # destination (e.g. "Widget" vs the nav's "Products").
        return [(l.path, _norm_ws(l.label)) for l in self.internal_links()
                if l.in_nav and not l.in_breadcrumb and l.label]

    def link_paths(self) -> Set[str]:
        return {l.path for l in self.internal_links()}

    def contact_affordance(self) -> bool:
        for l in self.links:
            h = (l.href or "").lower()
            if h.startswith("mailto:") or h.startswith("tel:"):
                return True
            if "contact" in h or "contact" in (l.label or "").lower():
                return True
        return False

    def has_primary_cta(self) -> bool:
        return bool(self.ctas)

    def has_visible_h1(self) -> bool:
        return any(_norm_ws(h) for h in self.visible_h1s)

    def hidden_h1_only(self) -> bool:
        return (not self.has_visible_h1()) and any(_norm_ws(h) for h in self.h1s)


class _PageParser(HTMLParser):
    _CTX_KEYS = ("nav", "breadcrumb", "header", "footer", "main", "hidden")

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base_url
        self.base_host = urllib.parse.urlsplit(base_url).netloc.lower()
        self.pv = PageView(url=base_url)
        self._ctx = {k: 0 for k in self._CTX_KEYS}
        self._stack: List[dict] = []
        self._cur_link: Optional[Link] = None
        self._link_text: List[str] = []
        self._in_title = False
        self._heading: Optional[str] = None
        self._heading_buf: List[str] = []

    @staticmethod
    def _hidden(a: Dict[str, str]) -> bool:
        try:
            from audit_common.dom import attrs_hidden
            return attrs_hidden(a)
        except Exception:
            if "hidden" in a:
                return True
            if a.get("aria-hidden", "").lower() == "true":
                return True
            st = a.get("style", "").replace(" ", "").lower()
            return "display:none" in st or "visibility:hidden" in st

    def _is_overlay(self, tag: str, a: Dict[str, str]) -> bool:
        st = a.get("style", "").replace(" ", "").lower()
        modal = a.get("aria-modal", "").lower() == "true"
        role_dialog = a.get("role", "").lower() == "dialog"
        if modal and role_dialog and "position:fixed" in st:
            return True
        if tag == "dialog" and "open" in a and modal:
            return True
        cls = a.get("class", "").lower()
        if re.search(r"\b(modal|overlay|interstitial|popup|lightbox)\b", cls) and "position:fixed" in st:
            return True
        return False

    def _incs_for(self, tag: str, a: Dict[str, str]) -> List[str]:
        incs: List[str] = []
        role = a.get("role", "").lower()
        if tag == "nav" or role == "navigation":
            incs.append("nav")
            self.pv.has_nav = True
            if "breadcrumb" in (a.get("aria-label", "") + " " + a.get("class", "")).lower():
                incs.append("breadcrumb")
                self.pv.has_breadcrumb = True
        if tag in ("header", "footer", "main"):
            incs.append(tag)
        if self._hidden(a):
            incs.append("hidden")
        return incs

    def handle_starttag(self, tag, attrs):
        a = {(k or "").lower(): (v or "") for k, v in attrs}

        if tag in _SKIP:
            self._stack.append({"tag": tag, "skip": True, "incs": []})
            return

        if tag == "meta" and a.get("name", "").lower() == "viewport":
            self.pv.viewport = a.get("content", "")
        elif tag == "title":
            self._in_title = True
        elif tag == "input" and a.get("type", "").lower() == "search":
            self.pv.has_search = True
        if a.get("role", "").lower() == "search":
            self.pv.has_search = True

        if self._is_overlay(tag, a):
            self.pv.overlay = {"selector": (tag + "." + a.get("class", "")).strip("."),
                               "text": ""}

        if tag in _HEADINGS:
            self._heading = tag
            self._heading_buf = []

        if tag == "a" and "href" in a:
            link = Link(
                href=a.get("href", "").strip(), aria_label=a.get("aria-label", ""),
                in_nav=self._ctx["nav"] > 0, in_breadcrumb=self._ctx["breadcrumb"] > 0,
                in_header=self._ctx["header"] > 0, in_footer=self._ctx["footer"] > 0,
                in_main=self._ctx["main"] > 0, hidden=self._ctx["hidden"] > 0,
            )
            self._classify_href(link)
            self._cur_link = link
            self._link_text = []
        elif tag == "button" or (tag == "input" and a.get("type", "").lower() in ("submit", "button")):
            label = a.get("value", "") or a.get("aria-label", "")
            if label and self._is_cta(label):
                self.pv.ctas.append(_norm_ws(label))

        if tag not in _VOID:
            incs = self._incs_for(tag, a)
            for k in incs:
                self._ctx[k] += 1
            self._stack.append({"tag": tag, "incs": incs})

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if self._stack and self._stack[-1].get("tag") == tag and not self._stack[-1].get("skip"):
            fr = self._stack.pop()
            for k in fr.get("incs", []):
                self._ctx[k] -= 1

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        if tag == "a" and self._cur_link is not None:
            self._cur_link.text = _norm_ws(" ".join(self._link_text))
            if self._cur_link.href and self._is_cta(self._cur_link.label):
                self.pv.ctas.append(self._cur_link.label)
            self.pv.links.append(self._cur_link)
            self._cur_link = None
            self._link_text = []
        if tag in _HEADINGS and self._heading == tag:
            text = _norm_ws(" ".join(self._heading_buf))
            if text:
                self.pv.headings.append(text)
                if tag == "h1":
                    self.pv.h1s.append(text)
                    if self._ctx["hidden"] == 0:
                        self.pv.visible_h1s.append(text)
            self._heading = None
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i].get("tag") == tag:
                for fr in self._stack[i:]:
                    for k in fr.get("incs", []):
                        self._ctx[k] -= 1
                del self._stack[i:]
                break

    def handle_data(self, data):
        if self._stack and self._stack[-1].get("skip"):
            return
        if self._in_title and data.strip():
            self.pv.title = _norm_ws(self.pv.title + " " + data)
        if self._cur_link is not None and data.strip():
            self._link_text.append(data.strip())
        if self._heading is not None and data.strip():
            self._heading_buf.append(data.strip())
        if self.pv.overlay and not self.pv.overlay.get("text") and data.strip():
            self.pv.overlay["text"] = _norm_ws(data)[:160]

    # -- helpers --------------------------------------------------------
    def _classify_href(self, link: Link) -> None:
        h = link.href
        low = h.lower()
        if (not h or low.startswith("#") or low.startswith("mailto:")
                or low.startswith("tel:") or low.startswith("javascript:")):
            link.external = low.startswith(("mailto:", "tel:", "javascript:"))
            return
        try:
            joined = urllib.parse.urljoin(self.base, h)
            parts = urllib.parse.urlsplit(joined)
        except Exception:
            return
        if parts.netloc and parts.netloc.lower() != self.base_host:
            link.external = True
            return
        link.path = parts.path or "/"

    @staticmethod
    def _is_cta(text: str) -> bool:
        t = (text or "").strip().lower()
        if not t:
            return False
        return any(v in t for v in _ACTION_VERBS)


def parse_page(url: str, html: str, page_class: str = "other") -> PageView:
    p = _PageParser(url)
    try:
        p.feed(html or "")
    except Exception:
        pass
    p.pv.page_class = page_class
    if p.pv.overlay:
        txt = (p.pv.overlay.get("text") or "").lower()
        if any(w in txt for w in _CONSENT_WORDS):
            p.pv.overlay = {}
    if not p.pv.has_nav:
        header_links = [l for l in p.pv.links if l.in_header and l.path]
        aside_links = [l for l in p.pv.links if l.in_nav]
        if len(header_links) >= 3 or len(aside_links) >= 3:
            p.pv.has_nav = True
    return p.pv


@dataclass
class JourneyModel:
    pages: Dict[str, PageView] = field(default_factory=dict)
    home_url: str = ""

    @property
    def home(self) -> Optional[PageView]:
        return self.pages.get(self.home_url)

    def has_real_homepage(self) -> bool:
        """True only when the sample includes the site's actual home page.

        Home-relative checks (key info reachable from home, site-wide IA)
        must not fire when the crawl sample is product-only and the first
        fetched URL was used as a fallback entry.
        """
        home = self.home
        if home is None:
            return False
        if getattr(home, "page_class", "") == "home":
            return True
        path = urllib.parse.urlsplit(home.url).path or "/"
        return path in ("", "/")

    def by_class(self, cls: str) -> List[PageView]:
        return [p for p in self.pages.values() if p.page_class == cls]

    def home_reaches(self, path: str, max_depth: int = 3) -> bool:
        home = self.home
        if home is None or not path:
            return False
        by_path = {p.self_path: p for p in self.pages.values() if p.self_path}
        seen: Set[str] = set()
        q: List[Tuple[PageView, int]] = [(home, 0)]
        while q:
            page, depth = q.pop(0)
            if page.url in seen:
                continue
            seen.add(page.url)
            for lp in page.link_paths():
                if lp == path or (lp != "/" and path.startswith(lp.rstrip("/") + "/")) \
                        or (path != "/" and lp.startswith(path.rstrip("/") + "/")):
                    return True
                nxt = by_path.get(lp)
                if nxt is not None and depth + 1 <= max_depth:
                    q.append((nxt, depth + 1))
        for p in self.pages.values():
            if any((l.in_nav or l.in_header) and l.path == path for l in p.links):
                return True
        return False


def build_model(records) -> JourneyModel:
    model = JourneyModel()
    home_candidate = ""
    try:
        from audit_common.semantics import classify_record
    except Exception:
        classify_record = None  # type: ignore
    for r in records:
        html = getattr(r, "rendered_html", "") or getattr(r, "raw_html", "")
        if not html:
            continue
        cls = classify_record(r) if classify_record is not None else None
        # Engagement is about visitor-facing HTML documents, not assets.
        if cls is not None and not cls.is_human_facing_page:
            continue
        pv = parse_page(getattr(r, "url", ""), html, getattr(r, "page_class", "other"))
        if cls is not None:
            pv.resource_type = cls.resource_type
            pv.page_type = cls.page_type
        model.pages[pv.url] = pv
        if pv.page_class == "home" and not model.home_url:
            model.home_url = pv.url
        if not home_candidate:
            home_candidate = pv.url
    if not model.home_url:
        model.home_url = home_candidate
    return model
