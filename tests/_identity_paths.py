"""Shared sys.path bootstrap for structured-identity unit tests."""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
IDENTITY_SCRIPTS = os.path.join(ROOT, "skills", "structured-identity", "scripts")
LIB = os.path.join(ROOT, "lib")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")

for _p in (IDENTITY_SCRIPTS, LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)
