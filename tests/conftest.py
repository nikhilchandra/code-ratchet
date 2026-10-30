"""Makes scratch projects for the tests that need a git repository with a .ratchet folder."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from ratchet.git import Repo
from ratchet.project import Project, load_project
from ratchet.refactor.settings import COMMAND

# The scratch project's tests run on this Python's pytest, without a cache folder.
PYTHON = sys.executable.replace("\\", "/")
CONFIG = f"""\
test_command = ["{PYTHON}", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
editable = ["src/**", "tests/**"]
frozen = ["**/*.feature"]
"""

APP = '''\
"""Adds numbers."""


def add(a, b):
    """Returns the sum."""
    # add them
    return a + b
'''


@pytest.fixture
def scratch(tmp_path: Path) -> tuple[Project, Repo]:
    """Returns a committed project with src/app.py, a test and a .ratchet folder, and its repo."""
    def git(*arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "ratchet@example.com")
    git("config", "user.name", "ratchet")
    git("config", "core.autocrlf", "false")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text(APP, encoding="utf-8", newline="\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text(
        "from app import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\npythonpath = src\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text("__pycache__/\n.ratchet/state/\n", encoding="utf-8")
    (tmp_path / ".ratchet").mkdir()
    (tmp_path / ".ratchet" / "config.toml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / ".ratchet" / "rules.md").write_text("Keep it small.\n", encoding="utf-8")
    (tmp_path / ".ratchet" / "problems.md").write_text("## P1: too big\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "start")
    return load_project(tmp_path, commands=[COMMAND]), Repo(tmp_path)
