"""Page-class discovery and representative sampling.

Generalization depends on sampling *templates*, not homepage-only and not 20
near-duplicate URLs. We cluster discovered URLs into page-classes by path
shape, then take k per class. This is deterministic clustering; the
crawl-access skill refines discovery (sitemap + nav).
"""

from __future__ import annotations

import re
import urllib.parse
from dataclasses import dataclass
from typing import Dict, List


# Common, stack-agnostic path signals -> canonical page-class labels.
_CLASS_PATTERNS = [
    ("product", re.compile(r"/(products?|shop|item|p)/", re.I)),
    ("article", re.compile(r"/(blog|news|articles?|posts?|stories)/", re.I)),
    ("docs", re.compile(r"/(docs?|documentation|guide|support|help)/", re.I)),
    ("pricing", re.compile(r"/(pricing|plans?)(/|$)", re.I)),
    ("about", re.compile(r"/(about|company|team|who-we-are)(/|$)", re.I)),
    ("faq", re.compile(r"/(faq|questions)(/|$)", re.I)),
    ("category", re.compile(r"/(category|categories|collections?)/", re.I)),
]


def classify(url: str) -> str:
    path = urllib.parse.urlsplit(url).path or "/"
    if path in ("", "/"):
        return "home"
    for label, pat in _CLASS_PATTERNS:
        if pat.search(path):
            return label
    return "other"


@dataclass
class SamplePlan:
    per_class: Dict[str, List[str]]
    k: int

    @property
    def classes(self) -> List[str]:
        return sorted(self.per_class.keys())

    def all_urls(self) -> List[str]:
        urls: List[str] = []
        for cls in self.classes:
            urls.extend(self.per_class[cls][: self.k])
        return urls

    def population(self) -> Dict[str, int]:
        return {cls: len(urls) for cls, urls in self.per_class.items()}


def build_sample_plan(urls: List[str], k: int = 3, max_total: int = 30) -> SamplePlan:
    """Cluster urls by page-class and select up to k per class, capped."""
    buckets: Dict[str, List[str]] = {}
    for u in urls:
        buckets.setdefault(classify(u), []).append(u)

    # Deterministic ordering for reproducibility.
    for cls in buckets:
        buckets[cls] = sorted(dict.fromkeys(buckets[cls]))

    # Enforce a global cap while keeping at least one per class where possible.
    plan = SamplePlan(per_class=buckets, k=k)
    selected = plan.all_urls()
    if len(selected) > max_total and buckets:
        # Reduce k until under cap (never below 1).
        while k > 1 and len(SamplePlan(buckets, k).all_urls()) > max_total:
            k -= 1
        plan = SamplePlan(per_class=buckets, k=k)
    return plan
