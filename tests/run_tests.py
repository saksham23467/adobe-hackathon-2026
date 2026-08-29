#!/usr/bin/env python3
"""Zero-dependency test runner.

Runs every test_* function in this directory without requiring pytest. Use
pytest if available for nicer output; this harness guarantees the suite runs
anywhere Python 3 is installed.

    python3 tests/run_tests.py
"""

import importlib.util
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
for _p in (HERE, ROOT, os.path.join(ROOT, "lib")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_module(path):
    spec = importlib.util.spec_from_file_location(os.path.basename(path)[:-3], path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    test_files = sorted(
        f for f in os.listdir(HERE) if f.startswith("test_") and f.endswith(".py")
    )
    passed = failed = 0
    failures = []
    for fname in test_files:
        mod = _load_module(os.path.join(HERE, fname))
        for attr in sorted(dir(mod)):
            if not attr.startswith("test_"):
                continue
            fn = getattr(mod, attr)
            if not callable(fn):
                continue
            try:
                fn()
                passed += 1
                print(f"PASS {fname}::{attr}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                failures.append((fname, attr, e, traceback.format_exc()))
                print(f"FAIL {fname}::{attr} -- {type(e).__name__}: {e}")

    print(f"\n{passed} passed, {failed} failed")
    if failures:
        print("\n--- failure details ---")
        for fname, attr, _e, tb in failures:
            print(f"\n{fname}::{attr}\n{tb}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
