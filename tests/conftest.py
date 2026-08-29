"""pytest configuration: put the shared lib and repo root on sys.path."""

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for p in (_ROOT, os.path.join(_ROOT, "lib")):
    if p not in sys.path:
        sys.path.insert(0, p)
