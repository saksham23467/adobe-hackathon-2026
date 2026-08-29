"""Shared sys.path bootstrap for engagement-orientation unit tests."""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ENGAGEMENT_SCRIPTS = os.path.join(ROOT, "skills", "engagement-orientation", "scripts")
LIB = os.path.join(ROOT, "lib")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")

for _p in (ENGAGEMENT_SCRIPTS, LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)
