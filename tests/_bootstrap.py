"""Path bootstrap shared by tests when run without pytest (via run_tests.py)."""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (ROOT, os.path.join(ROOT, "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

FIXTURE_CLEAN = os.path.join(ROOT, "tests", "fixtures", "clean-brand")
