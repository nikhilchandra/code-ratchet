"""Tests that a removed line falls back to the new file when git cannot be run."""

from __future__ import annotations

from pathlib import Path

import pytest

from ratchet import pathways
from ratchet.pathways import build_map, functions_of_diff


def test_a_removed_line_falls_back_to_the_new_file_when_git_cannot_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no git program to read the committed file, a removed line is mapped through the new file."""
    package = tmp_path / "src" / "app"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", newline="\n")
    (package / "mod.py").write_text("def a():\n    x = 1\n", newline="\n")

    def missing(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(pathways.subprocess, "run", missing)
    diff = (
        "--- a/src/app/mod.py\n+++ b/src/app/mod.py\n"
        "@@ -1,3 +1,2 @@\n def a():\n     x = 1\n-    return x\n"
    )
    assert functions_of_diff(build_map(tmp_path), diff) == ["app.mod.a"]
