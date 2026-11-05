"""Checks the pathway map's edge cases: cyclic bases, diff no-newline markers, running as a script, and names that
parameters or assignment targets bind."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

from ratchet.pathways import build_map, file_links, functions_of_diff


def write_tree(root: Path, files: dict[str, str]) -> Path:
    """Writes the files under root/src, with an empty app/__init__.py, and returns root."""
    for name, text in {"app/__init__.py": "", **files}.items():
        path = root / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_a_cycle_of_base_classes_ends_the_method_lookup(tmp_path: Path) -> None:
    """Proves a self call that no class in a base cycle defines is left unresolved, not looped on."""
    root = write_tree(tmp_path, {
        "app/loop.py": (
            "class A(B):\n    def go(self):\n        return self.nope()\n\n\n"
            "class B(A):\n    pass\n"
        ),
    })
    pathway_map = build_map(root)
    assert any(item.function == "app.loop.A.go" for item in pathway_map.unresolved)


def test_a_no_newline_marker_in_a_diff_is_skipped(tmp_path: Path) -> None:
    """Proves a backslash marker line does not count as a changed or context line."""
    root = write_tree(tmp_path, {"app/mod.py": "def one():\n    return 1\n"})
    diff = (
        "--- a/src/app/mod.py\n+++ b/src/app/mod.py\n@@ -1,2 +1,2 @@\n"
        " def one():\n-    return 0\n\\ No newline at end of file\n+    return 1\n"
    )
    assert functions_of_diff(build_map(root), diff) == ["app.mod.one"]


def test_the_module_runs_as_main_in_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves running the module as __main__ exits with the status that main returns."""
    root = write_tree(tmp_path, {"app/mod.py": "def one():\n    return 1\n"})
    monkeypatch.setattr(sys, "argv", ["pathways", "--root", str(root), "--functions", "app.mod.one"])
    sys.modules.pop("ratchet.pathways", None)
    with pytest.raises(SystemExit) as caught:
        runpy.run_module("ratchet.pathways", run_name="__main__")
    assert caught.value.code == 0


def test_a_parameter_that_shadows_an_import_is_unresolved(tmp_path: Path) -> None:
    """Proves a call on a parameter or a starred local named like an imported module, or on a
    parameter named like a builtin, makes no edge to the global and is listed as unresolved."""
    root = write_tree(tmp_path, {
        "app/ledger.py": "def save(x):\n    pass\n",
        "app/use.py": (
            "from app import ledger\n\n\n"
            "def go(ledger):\n    ledger.save(1)\n\n\n"
            "def shadow(type):\n    return type.lower()\n\n\n"
            "def starred(items):\n    first, *ledger = items\n    ledger.save(1)\n"
        ),
    })
    pathway_map = build_map(root)
    assert pathway_map.calls["app.use.go"] == set()
    assert pathway_map.calls["app.use.shadow"] == set()
    assert pathway_map.calls["app.use.starred"] == set()
    unresolved = {(item.function, item.call) for item in pathway_map.unresolved}
    assert {("app.use.go", "ledger.save"), ("app.use.shadow", "type.lower")} <= unresolved


def test_a_subscript_or_attribute_target_binds_no_name(tmp_path: Path) -> None:
    """Proves seen[NOTES] = True keeps the NOTES constant's file link, and repo.name = x keeps the
    type of repo."""
    root = write_tree(tmp_path, {
        "app/git.py": "class Repo:\n    def run(self):\n        pass\n",
        "app/m.py": (
            "from app.git import Repo\n\nNOTES = 'notes.json'\n\n\n"
            "def load(folder, seen):\n    seen[NOTES] = True\n    return (folder / NOTES).read_text()\n\n\n"
            "def use():\n    repo = Repo()\n    repo.name = 'x'\n    return repo.run()\n"
        ),
    })
    pathway_map = build_map(root)
    assert file_links(pathway_map, "app.m.load")["reads"] == ["notes.json"]
    assert "app.git.Repo.run" in pathway_map.calls["app.m.use"]
