#!/usr/bin/env python3
"""Build the Adobe submission ZIP from this marketplace root.

Writes an archive whose root is the marketplace (marketplace.json at zip root),
under 50 MB, with no live runs, caches, git metadata, or machine-local configs.

    python3 scripts/package_submission.py
    python3 scripts/package_submission.py --out /tmp/brand-ai-readiness-audit.zip
"""

from __future__ import annotations

import argparse
import os
import sys
import zipfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_DEFAULT_OUT = os.path.abspath(
    os.path.join(_ROOT, "..", "brand-ai-readiness-audit.zip")
)
_MAX_BYTES = 50 * 1024 * 1024

_ROOT_FILES = (
    "marketplace.json",
    "README.md",
    "requirements.txt",
    ".gitignore",
)
_ROOT_DIRS = ("skills", "lib", "config", "tests", "scripts")

_SKIP_DIR_NAMES = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "runs",
}
_SKIP_FILE_SUFFIXES = (".pyc", ".pyo", ".log", ".zip")
_SKIP_FILE_NAMES = {".ds_store", ".env", "report.json"}


def _skip_dir(name: str) -> bool:
    return name in _SKIP_DIR_NAMES or name.startswith(".")


def _skip_file(name: str) -> bool:
    lower = name.lower()
    if lower in _SKIP_FILE_NAMES:
        return True
    if name.startswith("_tmp"):
        return True
    return any(lower.endswith(suf) for suf in _SKIP_FILE_SUFFIXES)


def iter_members(root: str):
    for name in _ROOT_FILES:
        path = os.path.join(root, name)
        if os.path.isfile(path):
            yield name, path
    for dirname in _ROOT_DIRS:
        base = os.path.join(root, dirname)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not _skip_dir(d)]
            for fname in filenames:
                if _skip_file(fname):
                    continue
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                yield rel, full


def build_zip(root: str, out_path: str) -> int:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)
    members = list(iter_members(root))
    names = {rel for rel, _ in members}
    if "marketplace.json" not in names:
        raise SystemExit("marketplace.json missing from package")
    if "README.md" not in names:
        raise SystemExit("README.md missing from package")
    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel, full in sorted(members):
            zf.write(full, rel)
    size = os.path.getsize(out_path)
    if size >= _MAX_BYTES:
        os.remove(out_path)
        raise SystemExit(f"archive {size} bytes exceeds 50 MB; not written")
    return size


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Package the Adobe submission ZIP.")
    parser.add_argument(
        "--out",
        default=_DEFAULT_OUT,
        help="destination zip path (default: sibling of the marketplace root)",
    )
    args = parser.parse_args(argv)
    out = os.path.abspath(args.out)
    size = build_zip(_ROOT, out)
    print(f"wrote {out} ({size} bytes, {size / (1024 * 1024):.2f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
