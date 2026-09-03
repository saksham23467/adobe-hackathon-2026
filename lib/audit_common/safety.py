"""Read-only safety gate. This is the first line of guardrail compliance.

Guarantees enforced here (per the Adobe scope & guardrails):
  * GET-only. No POST/PUT/PATCH/DELETE ever leaves this module.
  * robots.txt is honored for our own fetches.
  * Hard caps on pages, bytes, concurrency, and total wall-clock time.
  * No Authorization/Cookie credentials are attached to requests.
  * No authenticated areas, no destructive or site-altering actions.

Live GETs go through curl (see ``curlfetch``). robots.txt parsing uses stdlib.
"""

from __future__ import annotations

import threading
import time
import urllib.parse
import urllib.robotparser
from dataclasses import dataclass, field
from typing import Dict, Optional, Set


# Our own crawler identity. Kept honest and contactable.
DEFAULT_USER_AGENT = "brand-ai-readiness-audit/1.0 (+read-only audit bot)"

_ALLOWED_METHODS: Set[str] = {"GET", "HEAD"}
_ALLOWED_SCHEMES: Set[str] = {"http", "https"}


def normalize_url(raw: str) -> str:
    """Normalize a user-supplied site into a canonical absolute URL.

    * Adds https:// when no scheme is present.
    * Lowercases the host, strips fragments, collapses a bare host to '/'.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("empty url")
    if "://" not in raw:
        raw = "https://" + raw
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise ValueError(f"unsupported scheme: {parts.scheme!r}")
    host = parts.netloc.lower()
    path = parts.path or "/"
    return urllib.parse.urlunsplit((parts.scheme, host, path, parts.query, ""))


@dataclass
class SafetyConfig:
    user_agent: str = DEFAULT_USER_AGENT
    max_pages: int = 40
    max_render: int = 8
    max_external_calls: int = 8
    max_bytes_per_page: int = 3_000_000  # 3 MB
    max_concurrency: int = 4
    request_timeout_s: float = 10.0
    min_delay_s: float = 0.5  # politeness between requests to one host
    total_budget_s: float = 270.0  # < 5 minutes, with margin
    respect_robots: bool = True

    @classmethod
    def from_dict(cls, data: Dict) -> "SafetyConfig":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in (data or {}).items() if k in allowed})


class RateLimiter:
    """Per-host politeness delay."""

    def __init__(self, min_delay_s: float) -> None:
        self._min_delay = min_delay_s
        self._last: Dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, host: str) -> None:
        """Space out request *dispatch* to one host by at least min_delay.

        Thread-safe: under bounded concurrency each host still sees requests no
        closer than min_delay apart (politeness preserved), while the network
        latency of separate requests overlaps."""
        with self._lock:
            now = time.monotonic()
            last = self._last.get(host)
            wait_for = 0.0
            if last is not None:
                elapsed = now - last
                if elapsed < self._min_delay:
                    wait_for = self._min_delay - elapsed
            # Reserve this host's next slot before sleeping so concurrent callers
            # queue behind us instead of all reading the same stale timestamp.
            self._last[host] = now + wait_for
        if wait_for > 0:
            time.sleep(wait_for)


class SafetyGate:
    """Central authority for what the marketplace is allowed to fetch."""

    def __init__(self, config: Optional[SafetyConfig] = None) -> None:
        self.config = config or SafetyConfig()
        self.rate = RateLimiter(self.config.min_delay_s)
        self._robots: Dict[str, Optional[urllib.robotparser.RobotFileParser]] = {}
        self._robots_raw: Dict[str, str] = {}
        self._robots_status: Dict[str, str] = {}  # ok | missing | unreachable
        self._pages_fetched = 0
        self._external_calls = 0
        self._start = time.monotonic()
        self.robots_respected = bool(self.config.respect_robots)
        # -- perf telemetry (cheap, always on) --------------------------
        self._request_keys: Set[str] = set()
        self._total_requests = 0
        self._duplicate_requests = 0
        self._fetch_time_s = 0.0
        self._external_time_s = 0.0
        self._counter_lock = threading.Lock()

    # -- budget ---------------------------------------------------------
    def elapsed_s(self) -> float:
        return time.monotonic() - self._start

    def time_left_s(self) -> float:
        return max(0.0, self.config.total_budget_s - self.elapsed_s())

    def budget_exhausted(self) -> bool:
        return self.time_left_s() <= 0.0

    # -- method / scheme guard -----------------------------------------
    def assert_readonly(self, method: str) -> None:
        if method.upper() not in _ALLOWED_METHODS:
            raise PermissionError(
                f"method {method!r} is not read-only; only GET/HEAD are permitted"
            )

    # -- robots ---------------------------------------------------------
    def cache_robots(self, url: str, raw: str, status: str = "ok") -> None:
        """Install a single robots.txt snapshot shared by crawler and detectors."""
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        self._robots_raw[origin] = raw or ""
        self._robots_status[origin] = status
        if raw and status == "ok":
            rp = urllib.robotparser.RobotFileParser()
            rp.parse(raw.splitlines())
            self._robots[origin] = rp
        else:
            self._robots[origin] = None

    def robots_raw(self, url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        return self._robots_raw.get(origin, "")

    def robots_status(self, url: str) -> str:
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        return self._robots_status.get(origin, "")

    def _robots_for(self, url: str) -> Optional[urllib.robotparser.RobotFileParser]:
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin in self._robots:
            return self._robots[origin]
        # Lazy fetch only if nothing has cached robots yet. Fail closed on 5xx.
        robots_url = urllib.parse.urljoin(origin, "/robots.txt")
        try:
            from .curlfetch import http_get
            got = http_get(
                robots_url,
                user_agent=self.config.user_agent,
                timeout_s=self.config.request_timeout_s,
                max_bytes=200_000,
                max_redirects=3,
                accept="text/plain,*/*",
            )
            if got.error and not got.body:
                self.cache_robots(robots_url, "", "unreachable")
            elif got.status and 500 <= got.status:
                self.cache_robots(robots_url, "", "unreachable")
            elif got.status and 400 <= got.status < 500:
                self.cache_robots(robots_url, "", "missing")
            elif (got.body or "").strip():
                self.cache_robots(robots_url, got.body, "ok")
            else:
                self.cache_robots(robots_url, "", "missing")
        except Exception:
            self.cache_robots(robots_url, "", "unreachable")
        return self._robots.get(origin)

    def can_fetch(self, url: str) -> bool:
        """True if our user-agent may fetch url under robots.txt.

        Fail closed when robots.txt is unreachable (5xx / network error):
        unknown policy is not permission. A missing (404) robots.txt allows.
        """
        if not self.config.respect_robots:
            return True
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        status = self._robots_status.get(origin)
        if status == "unreachable":
            return False
        rp = self._robots_for(url)
        status = self._robots_status.get(origin, "")
        if status == "unreachable":
            return False
        if rp is None:
            return True  # missing / empty robots → no rules
        try:
            return rp.can_fetch(self.config.user_agent, url)
        except Exception:
            return False

    def robots_parser(self, url: str) -> Optional[urllib.robotparser.RobotFileParser]:
        """Expose the parsed robots for detectors (e.g. AI-UA analysis)."""
        return self._robots_for(url)

    # -- counters -------------------------------------------------------
    def note_page_fetch(self) -> bool:
        with self._counter_lock:
            if self._pages_fetched >= self.config.max_pages:
                return False
            self._pages_fetched += 1
            return True

    def note_external_call(self) -> bool:
        with self._counter_lock:
            if self._external_calls >= self.config.max_external_calls:
                return False
            self._external_calls += 1
            return True

    @property
    def pages_fetched(self) -> int:
        return self._pages_fetched

    @property
    def external_calls(self) -> int:
        return self._external_calls

    # -- perf telemetry -------------------------------------------------
    def note_request(self, url: str) -> None:
        """Record every network request so the audit can report request volume
        and prove URL deduplication (a duplicate = the same URL fetched twice)."""
        with self._counter_lock:
            self._total_requests += 1
            key = url.split("#", 1)[0]
            if key in self._request_keys:
                self._duplicate_requests += 1
            else:
                self._request_keys.add(key)

    def add_fetch_time(self, seconds: float) -> None:
        with self._counter_lock:
            self._fetch_time_s += max(0.0, seconds)

    def add_external_time(self, seconds: float) -> None:
        with self._counter_lock:
            self._external_time_s += max(0.0, seconds)

    @property
    def total_requests(self) -> int:
        return self._total_requests

    @property
    def duplicate_requests(self) -> int:
        return self._duplicate_requests

    @property
    def fetch_time_s(self) -> float:
        return round(self._fetch_time_s, 3)

    @property
    def external_time_s(self) -> float:
        return round(self._external_time_s, 3)
