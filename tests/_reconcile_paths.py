"""Shared sys.path bootstrap for audit-orchestrator reconciliation tests."""

import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ORCH_SCRIPTS = os.path.join(ROOT, "skills", "audit-orchestrator", "scripts")
LIB = os.path.join(ROOT, "lib")
FIXTURES = os.path.join(ROOT, "tests", "fixtures")

for _p in (ORCH_SCRIPTS, LIB):
    if _p not in sys.path:
        sys.path.insert(0, _p)
