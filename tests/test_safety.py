"""Validate read-only safety guarantees."""

import os

try:
    from _bootstrap import ROOT  # noqa: F401
except ImportError:
    ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    import sys

    for _p in (ROOT, os.path.join(ROOT, "lib")):
        if _p not in sys.path:
            sys.path.insert(0, _p)

from audit_common.safety import SafetyConfig, SafetyGate, normalize_url


def test_normalize_url_adds_scheme():
    assert normalize_url("example.com") == "https://example.com/"
    assert normalize_url("http://Example.com/Path") == "http://example.com/Path"


def test_reject_unsupported_scheme():
    try:
        normalize_url("ftp://example.com")
    except ValueError:
        return
    raise AssertionError("expected ValueError for ftp scheme")


def test_only_get_head_allowed():
    gate = SafetyGate(SafetyConfig())
    gate.assert_readonly("GET")
    gate.assert_readonly("HEAD")
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        try:
            gate.assert_readonly(method)
        except PermissionError:
            continue
        raise AssertionError(f"{method} should be rejected")


def test_page_cap_enforced():
    gate = SafetyGate(SafetyConfig(max_pages=2))
    assert gate.note_page_fetch() is True
    assert gate.note_page_fetch() is True
    assert gate.note_page_fetch() is False


def test_external_cap_enforced():
    gate = SafetyGate(SafetyConfig(max_external_calls=1))
    assert gate.note_external_call() is True
    assert gate.note_external_call() is False


def test_budget_tracks_time():
    gate = SafetyGate(SafetyConfig(total_budget_s=1000))
    assert gate.budget_exhausted() is False
    assert gate.time_left_s() > 0
