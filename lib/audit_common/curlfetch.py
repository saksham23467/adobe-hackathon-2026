"""Read-only HTTP GET via curl (standard runtime tool).

Playwright and Selenium are not used. The live audit fetches with ``curl``
(GET only, http/https, robots-gated by the caller). If ``curl`` is missing,
stdlib ``urllib`` is the fallback so hermetic tests still run.

Never interpolates the URL into a shell string.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .net import is_public_http_url

_STATUS_LINE = re.compile(r"^HTTP/\S+\s+(\d{3})\b", re.I)
_CHARSET = re.compile(r"charset=([\w.-]+)", re.I)


@dataclass
class HttpResult:
    requested_url: str
    final_url: str = ""
    status: int = 0
    headers: Dict[str, str] = field(default_factory=dict)
    body: str = ""
    content_type: str = ""
    redirect_chain: List[Tuple[str, str, int]] = field(default_factory=list)
    error: str = ""
    transport: str = ""


def curl_available() -> bool:
    return shutil.which("curl") is not None


def http_get(
    url: str,
    *,
    user_agent: str,
    timeout_s: float = 10.0,
    max_bytes: int = 3_000_000,
    max_redirects: int = 5,
    accept: str = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
) -> HttpResult:
    """GET ``url``. Prefer curl; fall back to urllib."""
    if curl_available():
        return _curl_get(
            url,
            user_agent=user_agent,
            timeout_s=timeout_s,
            max_bytes=max_bytes,
            max_redirects=max_redirects,
            accept=accept,
        )
    return _urllib_get(
        url,
        user_agent=user_agent,
        timeout_s=timeout_s,
        max_bytes=max_bytes,
        max_redirects=max_redirects,
        accept=accept,
    )


def parse_header_dump(raw: str) -> Tuple[List[Tuple[str, str, int]], Dict[str, str]]:
    """Parse curl ``-D`` output (one block per hop) into hops + final headers."""
    hops: List[Tuple[str, str, int]] = []
    blocks = re.split(r"(?:\r?\n){2,}", raw or "")
    last_headers: Dict[str, str] = {}
    prev_url = ""
    for block in blocks:
        lines = [ln.rstrip("\r") for ln in block.splitlines() if ln.strip()]
        if not lines:
            continue
        status = 0
        m = _STATUS_LINE.match(lines[0])
        if m:
            status = int(m.group(1))
        headers: Dict[str, str] = {}
        for ln in lines[1:]:
            if ":" not in ln:
                continue
            key, val = ln.split(":", 1)
            headers[key.strip().lower()] = val.strip()
        loc = headers.get("location", "")
        if loc and status in (301, 302, 303, 307, 308):
            hops.append((prev_url, loc, status))
            prev_url = loc
        last_headers = headers
    return hops, last_headers


def _charset(headers: Dict[str, str], body_bytes: bytes) -> str:
    ctype = headers.get("content-type", "")
    m = _CHARSET.search(ctype)
    if m:
        return m.group(1)
    head = body_bytes[:2048].decode("ascii", errors="ignore")
    meta = re.search(r'charset=["\']?([\w.-]+)', head, re.I)
    if meta:
        return meta.group(1)
    return "utf-8"


def _curl_get(
    url: str,
    *,
    user_agent: str,
    timeout_s: float,
    max_bytes: int,
    max_redirects: int,
    accept: str,
) -> HttpResult:
    result = HttpResult(requested_url=url, final_url=url, transport="curl")
    with tempfile.TemporaryDirectory(prefix="audit-curl-") as tmp:
        body_path = os.path.join(tmp, "body")
        hdr_path = os.path.join(tmp, "headers")
        cmd = [
            "curl",
            "-sS",
            "-g",
            "-L",
            "--compressed",
            "--http1.1",
            "--proto", "=http,https",
            "--proto-redir", "=http,https",
            "--max-redirs", str(max_redirects),
            "--max-time", str(max(1, int(timeout_s))),
            "--max-filesize", str(max_bytes),
            "-A", user_agent,
            "-H", f"Accept: {accept}",
            "-D", hdr_path,
            "-o", body_path,
            "-w", "%{http_code}\n%{url_effective}\n%{content_type}\n",
            "--",
            url,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_s + 2,
                check=False,
            )
        except subprocess.TimeoutExpired:
            result.error = "fetch_error:Timeout"
            return result
        except OSError as e:
            result.error = f"fetch_error:{type(e).__name__}"
            return result

        lines = (proc.stdout or "").splitlines()
        try:
            result.status = int(lines[0]) if lines else 0
        except ValueError:
            result.status = 0
        if len(lines) >= 2 and lines[1].strip():
            result.final_url = lines[1].strip()
        if len(lines) >= 3:
            result.content_type = (lines[2] or "").split(";")[0].strip()

        hdr_raw = ""
        if os.path.exists(hdr_path):
            with open(hdr_path, "r", encoding="utf-8", errors="replace") as fh:
                hdr_raw = fh.read()
        hops, headers = parse_header_dump(hdr_raw)
        result.headers = headers
        result.redirect_chain = hops
        if not result.content_type:
            result.content_type = (headers.get("content-type") or "").split(";")[0].strip()

        raw = b""
        if os.path.exists(body_path):
            with open(body_path, "rb") as fh:
                raw = fh.read(max_bytes)
        result.body = raw.decode(_charset(headers, raw), errors="replace")

        if proc.returncode != 0 and result.status == 0:
            err = (proc.stderr or "").strip().splitlines()
            result.error = f"fetch_error:curl_{proc.returncode}"
            if err:
                result.error += f":{err[-1][:160]}"
            return result

        if result.final_url and not is_public_http_url(result.final_url, resolve=True):
            result.error = "blocked_host"
            result.body = ""
            return result
        return result


def _urllib_get(
    url: str,
    *,
    user_agent: str,
    timeout_s: float,
    max_bytes: int,
    max_redirects: int,
    accept: str,
) -> HttpResult:
    """Stdlib fallback when curl is not on PATH."""
    result = HttpResult(requested_url=url, final_url=url, transport="urllib")

    class _Recorder(urllib.request.HTTPRedirectHandler):
        def __init__(self) -> None:
            super().__init__()
            self.hops: List[Tuple[str, str, int]] = []
            self.max_redirections = max_redirects

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            self.hops.append((req.full_url, newurl, code))
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    recorder = _Recorder()
    opener = urllib.request.build_opener(recorder)
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"User-Agent": user_agent, "Accept": accept},
    )
    try:
        with opener.open(req, timeout=timeout_s) as resp:
            result.status = getattr(resp, "status", 200) or 200
            result.final_url = resp.geturl()
            result.headers = {k.lower(): v for k, v in resp.headers.items()}
            result.content_type = resp.headers.get_content_type()
            raw = resp.read(max_bytes)
            charset = resp.headers.get_content_charset() or "utf-8"
            result.body = raw.decode(charset, errors="replace")
            result.redirect_chain = recorder.hops
    except urllib.error.HTTPError as e:
        result.status = e.code
        result.final_url = getattr(e, "url", url) or url
        try:
            result.headers = {k.lower(): v for k, v in e.headers.items()}
            result.content_type = e.headers.get_content_type()
            raw = e.read(max_bytes)
            charset = "utf-8"
            try:
                charset = e.headers.get_content_charset() or "utf-8"
            except Exception:
                pass
            result.body = raw.decode(charset, errors="replace")
        except Exception:
            pass
        result.redirect_chain = recorder.hops
        if recorder.hops and len(recorder.hops) >= max_redirects:
            result.error = "too_many_redirects"
    except Exception as e:  # noqa: BLE001
        result.error = f"fetch_error:{type(e).__name__}"
        return result

    if result.final_url and not is_public_http_url(result.final_url, resolve=True):
        result.error = "blocked_host"
        result.body = ""
    return result
