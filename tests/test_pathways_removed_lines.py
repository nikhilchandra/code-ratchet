"""Tests that a removed line of a diff is mapped to the function it was removed from."""

from pathlib import Path

from ratchet.pathways import build_map, functions_of_diff


def _tree(root: Path, source: str) -> None:
    package = root / "src" / "app"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "mod.py").write_text(source)


def _diff(hunk: str) -> str:
    return "--- a/src/app/mod.py\n+++ b/src/app/mod.py\n" + hunk


def test_removing_the_last_line_of_a_function_maps_to_that_function(tmp_path):
    _tree(tmp_path, "def a():\n    x = 1\n\n\ndef b():\n    return 2\n")
    diff = _diff("@@ -1,4 +1,3 @@\n def a():\n     x = 1\n-    return x\n \n")
    assert functions_of_diff(build_map(tmp_path), diff) == ["app.mod.a"]


def test_removing_the_last_line_before_the_next_def_maps_to_the_first_function(tmp_path):
    _tree(tmp_path, "def a():\n    x = 1\ndef b():\n    return 2\n")
    diff = _diff("@@ -1,3 +1,3 @@\n def a():\n     x = 1\n-    return x\n def b():\n")
    assert functions_of_diff(build_map(tmp_path), diff) == ["app.mod.a"]


def test_removing_a_module_level_line_maps_to_the_module(tmp_path):
    _tree(tmp_path, "def a():\n    return 1\n\nVALUE = 1\n")
    diff = _diff("@@ -3,3 +3,2 @@\n \n-OLD = 0\n VALUE = 1\n")
    assert functions_of_diff(build_map(tmp_path), diff) == ["app.mod.<module>"]
