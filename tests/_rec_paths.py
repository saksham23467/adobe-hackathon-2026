"""Shared sys.path bootstrap for recommendation-engine unit tests."""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REC_SCRIPTS = os.path.join(ROOT, "skills", "recommendation-engine", "scripts")
ORCH_SCRIPTS = os.path.join(ROOT, "skills", "audit-orchestrator", "scripts")
LIB = os.path.join(ROOT, "lib")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")

for _p in (REC_SCRIPTS, LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)
