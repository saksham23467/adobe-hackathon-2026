"""Validate marketplace structure and per-skill SKILL.md format.

Mirrors the checks the Adobe grader will run: exactly one entrypoint, every
listed skill folder exists with a SKILL.md whose frontmatter `name` matches the
folder and follows the agentskills.io naming rules.
"""

import json
import os
import re

try:
    from _bootstrap import ROOT
except ImportError:  # under pytest, conftest handles the path
    ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

_NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _read_frontmatter(path):
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    assert text.startswith("---"), f"{path}: SKILL.md must start with YAML frontmatter"
    parts = text.split("---", 2)
    assert len(parts) >= 3, f"{path}: malformed frontmatter"
    fm = parts[1]
    fields = {}
    key = None
    for line in fm.splitlines():
        m = re.match(r"^([a-zA-Z0-9_-]+):\s*(.*)$", line)
        if m and not line.startswith(" "):
            key = m.group(1)
            fields[key] = m.group(2).strip()
    return fields


def _skill_md(path):
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _frontmatter_and_body(text):
    parts = text.split("---", 2)
    return parts[1], parts[2]


def _description_text(fm):
    """Extract the full description, handling YAML folded/literal scalars
    (``description: >-``) without a YAML dependency."""
    lines = fm.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^description:\s*(.*)$", line)
        if not m:
            continue
        inline = m.group(1).strip()
        if inline and inline not in (">", ">-", "|", "|-"):
            return inline
        # Folded/literal block: gather the following indented lines.
        block = []
        for cont in lines[i + 1:]:
            if re.match(r"^\s+\S", cont):
                block.append(cont.strip())
            elif cont.strip() == "":
                block.append("")
            else:
                break
        return " ".join(x for x in block if x != "").strip()
    return ""


def _linked_paths(body, kind):
    # e.g. references/foo.md or scripts/bar.py mentioned anywhere in the body.
    return set(re.findall(rf"{kind}/[A-Za-z0-9_./-]+", body))


def _load_manifest():
    with open(os.path.join(ROOT, "marketplace.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def test_manifest_exists_and_parses():
    m = _load_manifest()
    assert m["name"] == "brand-ai-readiness-audit"
    assert isinstance(m.get("skills"), list) and m["skills"]


def test_exactly_one_entrypoint():
    m = _load_manifest()
    entrypoints = [s for s in m["skills"] if s.get("entrypoint")]
    assert len(entrypoints) == 1, f"expected exactly one entrypoint, got {len(entrypoints)}"
    assert m.get("entrypoint") == entrypoints[0]["id"]


def test_every_skill_folder_valid():
    m = _load_manifest()
    for skill in m["skills"]:
        folder = os.path.join(ROOT, skill["path"])
        assert os.path.isdir(folder), f"missing skill folder: {skill['path']}"
        skill_md = os.path.join(folder, "SKILL.md")
        assert os.path.isfile(skill_md), f"missing SKILL.md in {skill['path']}"
        fields = _read_frontmatter(skill_md)
        assert "name" in fields, f"{skill_md}: missing name"
        assert "description" in fields and fields["description"] != "", f"{skill_md}: missing description"
        name = fields["name"]
        assert _NAME_RE.match(name), f"{skill_md}: invalid name '{name}'"
        folder_name = os.path.basename(folder.rstrip("/"))
        assert name == folder_name, f"{skill_md}: name '{name}' != folder '{folder_name}'"
        assert name == skill["id"], f"manifest id '{skill['id']}' != SKILL name '{name}'"
        assert fields.get("allowed-tools"), f"{skill_md}: missing allowed-tools (agentskills.io)"


def test_skill_paths_are_unique():
    m = _load_manifest()
    ids = [s["id"] for s in m["skills"]]
    assert len(ids) == len(set(ids)), "duplicate skill ids in manifest"


def test_manifest_paths_follow_convention():
    m = _load_manifest()
    for skill in m["skills"]:
        assert skill["path"] == f"skills/{skill['id']}", (
            f"path '{skill['path']}' should be 'skills/{skill['id']}'"
        )


def test_no_orphaned_skill_directories():
    """Every skills/<x>/SKILL.md directory must be listed in the manifest."""
    m = _load_manifest()
    listed = {s["id"] for s in m["skills"]}
    on_disk = set()
    skills_root = os.path.join(ROOT, "skills")
    for name in os.listdir(skills_root):
        if os.path.isfile(os.path.join(skills_root, name, "SKILL.md")):
            on_disk.add(name)
    assert on_disk - listed == set(), f"orphaned skill dirs not in manifest: {on_disk - listed}"
    assert listed - on_disk == set(), f"manifest lists missing skills: {listed - on_disk}"


def test_frontmatter_field_limits():
    """name <= 64 chars & valid, description non-empty & <= 1024 chars."""
    m = _load_manifest()
    for skill in m["skills"]:
        skill_md = os.path.join(ROOT, skill["path"], "SKILL.md")
        text = _skill_md(skill_md)
        fm, _body = _frontmatter_and_body(text)
        fields = _read_frontmatter(skill_md)
        name = fields["name"]
        assert len(name) <= 64, f"{skill_md}: name exceeds 64 chars"
        desc = _description_text(fm)
        assert desc, f"{skill_md}: description resolves empty"
        assert len(desc) <= 1024, f"{skill_md}: description {len(desc)} > 1024 chars"


def test_skill_body_under_500_lines():
    m = _load_manifest()
    for skill in m["skills"]:
        skill_md = os.path.join(ROOT, skill["path"], "SKILL.md")
        _fm, body = _frontmatter_and_body(_skill_md(skill_md))
        n = body.count("\n") + 1
        assert n <= 500, f"{skill_md}: body {n} lines exceeds 500"


def test_linked_files_exist():
    """Every references/* and scripts/* path mentioned in a SKILL.md exists."""
    m = _load_manifest()
    for skill in m["skills"]:
        folder = os.path.join(ROOT, skill["path"])
        _fm, body = _frontmatter_and_body(_skill_md(os.path.join(folder, "SKILL.md")))
        for kind in ("references", "scripts"):
            for rel in _linked_paths(body, kind):
                target = os.path.join(folder, rel)
                assert os.path.isfile(target), (
                    f"{skill['id']}: SKILL.md links '{rel}' but it does not exist"
                )


def test_no_orphaned_reference_files():
    """Progressive disclosure: every references/*.md is linked from SKILL.md."""
    m = _load_manifest()
    for skill in m["skills"]:
        folder = os.path.join(ROOT, skill["path"])
        ref_dir = os.path.join(folder, "references")
        if not os.path.isdir(ref_dir):
            continue
        _fm, body = _frontmatter_and_body(_skill_md(os.path.join(folder, "SKILL.md")))
        linked = _linked_paths(body, "references")
        for name in os.listdir(ref_dir):
            if not name.endswith(".md"):
                continue
            assert f"references/{name}" in linked, (
                f"{skill['id']}: reference '{name}' is orphaned (not linked from SKILL.md)"
            )


def test_references_one_level_deep():
    """References must be a single level under the skill (no nested dirs)."""
    m = _load_manifest()
    for skill in m["skills"]:
        ref_dir = os.path.join(ROOT, skill["path"], "references")
        if not os.path.isdir(ref_dir):
            continue
        for entry in os.listdir(ref_dir):
            assert os.path.isfile(os.path.join(ref_dir, entry)), (
                f"{skill['id']}: references/{entry} must be a file (references are one level deep)"
            )


def test_entrypoint_declared_consistently():
    m = _load_manifest()
    assert m.get("entrypoint"), "manifest missing top-level entrypoint"
    ep = [s for s in m["skills"] if s.get("entrypoint")]
    assert len(ep) == 1 and ep[0]["id"] == m["entrypoint"]
    # Non-entrypoint skills must not carry the flag.
    for s in m["skills"]:
        if s["id"] != m["entrypoint"]:
            assert not s.get("entrypoint"), f"{s['id']} must not be flagged entrypoint"
