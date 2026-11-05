"""Tests that a removed line falls back to the new file when the committed file cannot be shown."""

from __future__ import annotations

from pathlib import Path

from ratchet.pathways import build_map, functions_of_diff
from test_pathways_deleted_code import _commit, _write


def test_a_path_that_is_not_in_the_commit_falls_back_to_the_new_file(tmp_path: Path) -> None:
    """The package holds an __init__.py at HEAD but the file does not, so git show fails."""
    _commit(tmp_path, {})
    _write(tmp_path, "src/app/mod.py", b"def a():\n    x = 1\n    return x\n")
    diff = "--- a/src/app/mod.py\n+++ b/src/app/mod.py\n@@ -2,3 +2,2 @@\n def a():\n     x = 1\n-    return x\n"
    assert functions_of_diff(build_map(tmp_path), diff) == ["app.mod.a"]
