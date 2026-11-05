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


def test_a_removed_blank_line_above_a_class_field_names_nothing(tmp_path):
    """Proves that removing a blank line from a class body names nothing, when the committed file
    cannot be read and the removed line is placed through the new file."""
    _tree(tmp_path, "class Box:\n    size: int = 1\n")
    diff = _diff("@@ -1,3 +1,2 @@\n class Box:\n-\n     size: int = 1\n")
    assert functions_of_diff(build_map(tmp_path), diff) == []


def test_an_added_field_in_a_class_names_the_class(tmp_path):
    """Proves that a line added directly in a class body, outside its methods, names the class."""
    _tree(tmp_path, 'class Box:\n    size: int = 1\n    color: str = "red"\n')
    diff = _diff('@@ -1,2 +1,3 @@\n class Box:\n     size: int = 1\n+    color: str = "red"\n')
    assert functions_of_diff(build_map(tmp_path), diff) == ["app.mod.Box"]


def test_removing_the_last_line_of_a_method_before_a_blank_line_names_the_method(tmp_path):
    """Proves that removing the last line of the method a, which sits before a blank line in the
    class body, names a and not the class Box."""
    _tree(tmp_path, "class Box:\n    def a(self):\n        x = 1\n\n    def b(self):\n"
                    "        return 2\n")
    diff = _diff("@@ -1,4 +1,3 @@\n class Box:\n     def a(self):\n         x = 1\n"
                 "-        return x\n \n")
    assert functions_of_diff(build_map(tmp_path), diff) == ["app.mod.Box.a"]


def test_an_added_import_names_nothing(tmp_path):
    """Proves that added module-level imports, one of them written over several lines, and the
    blank lines added after them name nothing."""
    _tree(tmp_path, "import os\nfrom json import (\n    dumps,\n)\n\n\ndef a():\n    return 1\n")
    diff = _diff("@@ -1,2 +1,8 @@\n+import os\n+from json import (\n+    dumps,\n+)\n+\n+\n"
                 " def a():\n     return 1\n")
    assert functions_of_diff(build_map(tmp_path), diff) == []


def test_a_changed_module_constant_names_the_module(tmp_path):
    """Proves that a changed module constant names the module's code, app.mod.<module>."""
    _tree(tmp_path, "LIMIT = 4\n\n\ndef a():\n    return LIMIT\n")
    diff = _diff("@@ -1,1 +1,1 @@\n-LIMIT = 3\n+LIMIT = 4\n")
    assert functions_of_diff(build_map(tmp_path), diff) == ["app.mod.<module>"]
