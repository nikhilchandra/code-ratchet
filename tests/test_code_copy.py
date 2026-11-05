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
    """Returns a repository with a branch, a tag and a remote."""
    origin = tmp_path / "origin"
    make_repo(origin)
    commit_file(origin, "app.py", "x = 1\n", "start")
    project = tmp_path / "project"
    git(tmp_path, "clone", "-q", str(origin), str(project))
    git(project, "config", "user.email", "a@example.com")
    git(project, "config", "user.name", "a")
    commit_file(project, "app.py", "x = 2\n", "change")
    git(project, "branch", "side")
    git(project, "tag", "v1")
    return project


def test_copy_has_no_branch_tag_or_remote(source: Path) -> None:
    """Proves the copy is at HEAD and holds no branch, tag or remote."""
    folder = make_copy(source)
    try:
        copy = Repo(folder)
        assert copy.run("for-each-ref", "--format=%(refname)").strip() == ""
        assert copy.run("remote").strip() == ""
        assert (folder / "app.py").read_text(encoding="utf-8") == "x = 2\n"
    finally:
        remove_copy(folder)
    assert not folder.exists()


def test_list_on_another_branch_is_not_copied(tmp_path: Path) -> None:
    """Proves a list committed only on another branch is not in the copy, not even as a loose
    object."""
    project = tmp_path / "project"
    make_repo(project)
    commit_file(project, "app.py", "x = 1\n", "start")
    git(project, "switch", "-q", "-c", "side")
    commit_file(project, ".ratchet/problems-1.md", f"# {CANARY}\n", "add records")
    git(project, "switch", "-q", "main")
    commit_file(project, "app.py", "x = 2\n", "change")
    source = Repo(project)
    blob = source.run("rev-parse", "side:.ratchet/problems-1.md").strip()
    folder = make_copy(project)
    try:
        copy = Repo(folder)
        found = subprocess.run(
            ["git", "cat-file", "-e", blob], cwd=folder, capture_output=True
        )
        assert found.returncode != 0
        contents = subprocess.run(
            ["git", "cat-file", "--batch-all-objects", "--batch"],
            cwd=folder, capture_output=True, check=True,
        ).stdout
        assert CANARY.encode() not in contents
        assert ".ratchet" not in copy.run("rev-list", "--all", "--objects")
        assert copy.run("rev-parse", "HEAD") == source.run("rev-parse", "main")
    finally:
        remove_copy(folder)


def test_clean_history_is_copied_as_it_is(tmp_path: Path) -> None:
    """Proves a history with no .ratchet files is not rewritten."""
    project = tmp_path / "project"
    make_repo(project)
    commit_file(project, "app.py", "x = 1\n", "start")
    folder = make_copy(project)
    try:
        assert Repo(folder).run("rev-parse", "HEAD") == Repo(project).run("rev-parse", "HEAD")
    finally:
        remove_copy(folder)


def test_remove_copy_refuses_a_folder_that_is_not_a_ratchet_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves remove_copy deletes nothing given an empty path, a folder without the ratchet-audit-
    prefix, or a ratchet-audit- folder outside the temp folder."""
    here = tmp_path / "cwd"
    here.mkdir()
    (here / "keep.txt").write_text("keep\n", encoding="utf-8")
    monkeypatch.chdir(here)
    project = tmp_path / "project"
    project.mkdir()
    (project / "app.py").write_text("x = 1\n", encoding="utf-8")
    misplaced = tmp_path / "ratchet-audit-x"
    misplaced.mkdir()
    (misplaced / "app.py").write_text("x = 1\n", encoding="utf-8")

    for folder in (project, misplaced):
        with pytest.raises(CopyError, match="not a ratchet copy"):
            remove_copy(folder)
        assert (folder / "app.py").is_file()
    with pytest.raises(CopyError, match="not a ratchet copy"):
        remove_copy(Path(""))
    assert (here / "keep.txt").is_file()
