"""Shared sys.path bootstrap for crawl-access unit tests."""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CRAWL_SCRIPTS = os.path.join(ROOT, "skills", "crawl-access", "scripts")
LIB = os.path.join(ROOT, "lib")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")

for _p in (CRAWL_SCRIPTS, LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)
