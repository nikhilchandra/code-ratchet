"""Checks the audit's copy of the code on scratch repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ratchet.audit.code_copy import CopyError, make_copy, remove_copy
from ratchet.git import Repo

CANARY = "canary-7731"


def git(path: Path, *arguments: str) -> None:
    """Runs one git command in a folder."""
    subprocess.run(["git", *arguments], cwd=path, check=True, capture_output=True)


def commit_file(path: Path, name: str, text: str, message: str) -> None:
    """Writes a file and commits it."""
    target = path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message)


def make_repo(path: Path) -> None:
    """Makes an empty repository with a git identity."""
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "a@example.com")
    git(path, "config", "user.name", "a")
    git(path, "config", "core.autocrlf", "false")


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """Returns a repository with a branch, a tag, a remote and a history that holds .ratchet."""
    origin = tmp_path / "origin"
    make_repo(origin)
    commit_file(origin, "app.py", "x = 1\n", "start")
    project = tmp_path / "project"
    git(tmp_path, "clone", "-q", str(origin), str(project))
    git(project, "config", "user.email", "a@example.com")
    git(project, "config", "user.name", "a")
    commit_file(project, ".ratchet/problems-1.md", f"# {CANARY}\n", "add records")
    git(project, "rm", "-r", "-q", "--cached", ".ratchet")
    git(project, "commit", "-q", "-m", "untrack records")
    commit_file(project, "app.py", "x = 2\n", "change")
    git(project, "branch", "side")
    git(project, "tag", "v1")
    return project


def test_copy_has_no_branch_tag_or_remote(source: Path) -> None:
    """Proves the copy is at HEAD and holds no branch, tag or remote."""
    folder = make_copy(source, [], "strip")
    try:
        copy = Repo(folder)
        assert copy.run("for-each-ref", "--format=%(refname)").strip() == ""
        assert copy.run("remote").strip() == ""
        assert (folder / "app.py").read_text(encoding="utf-8") == "x = 2\n"
    finally:
        remove_copy(folder)
    assert not folder.exists()


def test_history_with_ratchet_is_stripped(source: Path) -> None:
    """Proves a history that held .ratchet is rewritten, and the canary is in no commit."""
    folder = make_copy(source, [], "strip")
    try:
        copy = Repo(folder)
        assert copy.run("log", "--all", "--format=%h", "--", ".ratchet").strip() == ""
        assert copy.run("log", "--all", "-S", CANARY, "--format=%h").strip() == ""
        assert ".ratchet" not in copy.run("rev-list", "--all", "--objects")
        assert copy.run("rev-list", "--all", "--count").strip() == "4"
    finally:
        remove_copy(folder)


def test_hidden_paths_are_stripped_too(source: Path) -> None:
    """Proves paths listed in 'hide' are removed from the copy's history."""
    commit_file(source, "vendor/lib.py", "y = 1\n", "add vendor")
    folder = make_copy(source, ["vendor"], "strip")
    try:
        assert not (folder / "vendor").exists()
        assert Repo(folder).run("log", "--all", "--format=%h", "--", "vendor").strip() == ""
    finally:
        remove_copy(folder)


def test_refuse_setting_raises_and_leaves_no_copy(
    source: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Proves the refuse setting stops the copy when the history holds .ratchet."""
    made = tmp_path / "temp"
    made.mkdir()
    monkeypatch.setattr("tempfile.tempdir", str(made))
    with pytest.raises(CopyError, match="refuse"):
        make_copy(source, [], "refuse")
    assert list(made.iterdir()) == []


def test_clean_history_is_copied_as_it_is(tmp_path: Path) -> None:
    """Proves a history with no .ratchet files is not rewritten, even under the refuse setting."""
    project = tmp_path / "project"
    make_repo(project)
    commit_file(project, "app.py", "x = 1\n", "start")
    folder = make_copy(project, [], "refuse")
    try:
        assert Repo(folder).run("rev-parse", "HEAD") == Repo(project).run("rev-parse", "HEAD")
    finally:
        remove_copy(folder)
