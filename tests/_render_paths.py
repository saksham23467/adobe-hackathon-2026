"""Shared sys.path bootstrap for render-extract unit tests."""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RENDER_SCRIPTS = os.path.join(ROOT, "skills", "render-extract", "scripts")
LIB = os.path.join(ROOT, "lib")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")

for _p in (RENDER_SCRIPTS, LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)
