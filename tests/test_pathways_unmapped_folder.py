"""Tests that a removed line in a folder that _find_modules skips is not mapped through the committed file."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ratchet.git import Repo
from ratchet.pathways import build_map, functions_of_diff


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


def test_a_changed_file_in_a_folder_without_init_names_nothing(tmp_path: Path) -> None:
    """Proves that editing a function in src/scripts, which has no __init__.py, names no function,
    since the map does not hold that file."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "ratchet@example.com")
    _git(tmp_path, "config", "user.name", "ratchet")
    _git(tmp_path, "config", "core.autocrlf", "false")
    (tmp_path / "src/app").mkdir(parents=True)
    (tmp_path / "src/scripts").mkdir()
    (tmp_path / "src/app/__init__.py").write_bytes(b"")
    run = tmp_path / "src/scripts/run.py"
    run.write_bytes(b"def main():\n    x = 1\n    return x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "start")
    run.write_bytes(b"def main():\n    x = 1\n    return 2\n")

    assert functions_of_diff(build_map(tmp_path), Repo(tmp_path).diff()) == []
