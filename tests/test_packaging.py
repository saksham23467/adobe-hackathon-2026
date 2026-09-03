"""Regression: the submission packager emits a judge-safe archive."""

import os
import tempfile
import zipfile

try:
    from _bootstrap import ROOT
except ImportError:
    ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def _packager():
    import importlib.util

    path = os.path.join(ROOT, "scripts", "package_submission.py")
    spec = importlib.util.spec_from_file_location("package_submission", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_packaging_script_exists():
    assert os.path.isfile(os.path.join(ROOT, "scripts", "package_submission.py"))


def test_package_has_marketplace_at_root_and_excludes_junk():
    pack = _packager()
    with tempfile.TemporaryDirectory() as dest:
        out = os.path.join(dest, "submission.zip")
        pack.build_zip(ROOT, out)
        assert os.path.getsize(out) < 50 * 1024 * 1024
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
        assert "marketplace.json" in names
        assert "README.md" in names
        assert "skills/audit-orchestrator/SKILL.md" in names
        assert "config/settings.json" in names
        assert "scripts/package_submission.py" in names
        blob = "\n".join(names)
        assert "runs/" not in blob
        assert "__pycache__" not in blob
        assert ".git/" not in blob
        assert "_tmp_adobe" not in blob
        assert ".DS_Store" not in blob
