"""Checks the git helper on scratch repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ratchet.git import GitError, Repo


def make_repo(root: Path) -> Repo:
    """Makes a repository in 'root' with one committed file and a .ratchet folder, and returns it."""
    def git(*arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "ratchet@example.com")
    git("config", "user.name", "ratchet")
    git("config", "core.autocrlf", "false")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (root / ".ratchet").mkdir()
    (root / ".ratchet" / "rules.md").write_text("Keep it small.\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "start")
    return Repo(root)


def test_changed_paths_lists_edits_new_files_and_renames(tmp_path: Path) -> None:
    """Proves an edited file, a new file in a new folder and a renamed file are each listed,
    and a change inside .ratchet is not."""
    repo = make_repo(tmp_path)
    (tmp_path / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "src" / "new dir").mkdir()
    (tmp_path / "src" / "new dir" / "extra.py").write_text("y = 1\n", encoding="utf-8")
    (tmp_path / ".ratchet" / "rules.md").write_text("Changed.\n", encoding="utf-8")

    assert sorted(repo.changed_paths()) == ["src/app.py", "src/new dir/extra.py"]

    repo.commit("edit")
    repo.run("mv", "src/app.py", "src/main.py")
    assert repo.changed_paths() == ["src/main.py"]


def test_diff_includes_new_files_and_leaves_nothing_staged(tmp_path: Path) -> None:
    """Proves the diff shows a new file, and that taking it stages nothing."""
    repo = make_repo(tmp_path)
    (tmp_path / "src" / "extra.py").write_text("y = 1\n", encoding="utf-8")

    assert "+y = 1" in repo.diff()
    assert repo.run("diff", "--cached", "--name-only") == ""


def test_commit_returns_the_new_hash_and_leaves_ratchet_out(tmp_path: Path) -> None:
    """Proves a commit takes every step change but none in .ratchet."""
    repo = make_repo(tmp_path)
    (tmp_path / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / ".ratchet" / "rules.md").write_text("Changed.\n", encoding="utf-8")

    commit = repo.commit("change x")

    assert commit == repo.last_commit()
    assert repo.run("show", "--name-only", "--format=", "HEAD").split() == ["src/app.py"]


def test_commit_with_nothing_to_commit_fails(tmp_path: Path) -> None:
    """Proves committing an empty step is an error rather than a silent no-op."""
    with pytest.raises(GitError, match="git commit"):
        make_repo(tmp_path).commit("nothing")


def test_undo_puts_back_edits_and_removes_new_files_but_not_ratchet(tmp_path: Path) -> None:
    """Proves undo returns the tree to the last commit, leaving .ratchet as it is."""
    repo = make_repo(tmp_path)
    (tmp_path / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")
    (tmp_path / "src" / "extra.py").write_text("y = 1\n", encoding="utf-8")
    (tmp_path / ".ratchet" / "problems.md").write_text("## P1\n", encoding="utf-8")

    repo.undo()

    assert (tmp_path / "src" / "app.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (tmp_path / "src" / "extra.py").exists()
    assert (tmp_path / ".ratchet" / "problems.md").exists()
    assert repo.changed_paths() == []


def test_saved_work_comes_back_exactly(tmp_path: Path) -> None:
    """Proves half-made work, with an edit, a new file and Windows line endings, is put back
    exactly after the tree was changed further."""
    repo = make_repo(tmp_path)
    (tmp_path / "src" / "app.py").write_bytes(b"x = 2\r\ny = 3\r\n")
    (tmp_path / "src" / "extra.py").write_text("z = 1\n", encoding="utf-8")
    patch = repo.save_work()

    (tmp_path / "src" / "app.py").write_text("ruined\n", encoding="utf-8")
    (tmp_path / "src" / "junk.py").write_text("junk\n", encoding="utf-8")
    repo.restore_work(patch)

    assert (tmp_path / "src" / "app.py").read_bytes() == b"x = 2\r\ny = 3\r\n"
    assert (tmp_path / "src" / "extra.py").read_text(encoding="utf-8") == "z = 1\n"
    assert not (tmp_path / "src" / "junk.py").exists()


def test_restoring_no_work_leaves_the_last_commit(tmp_path: Path) -> None:
    """Proves an empty patch restores the tree to the last commit."""
    repo = make_repo(tmp_path)
    patch = repo.save_work()
    (tmp_path / "src" / "app.py").write_text("ruined\n", encoding="utf-8")

    repo.restore_work(patch)

    assert patch == b""
    assert repo.changed_paths() == []


@pytest.mark.parametrize("setting", ["diff.noprefix", "diff.mnemonicPrefix"])
def test_diff_keeps_the_a_and_b_prefixes_when_the_owner_turns_them_off(
        tmp_path: Path, setting: str) -> None:
    """Proves the diff's file headers keep git's a/ and b/ prefixes when the repository's config
    sets 'setting', which would otherwise drop them or replace them with i/ and w/."""
    repo = make_repo(tmp_path)
    repo.run("config", setting, "true")
    (tmp_path / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")

    diff = repo.diff()

    assert "--- a/src/app.py" in diff
    assert "+++ b/src/app.py" in diff


@pytest.mark.parametrize("setting", ["color.ui", "color.diff"])
def test_diff_has_no_color_when_the_owner_forces_it(tmp_path: Path, setting: str) -> None:
    """Proves the diff holds no ANSI color codes when the repository's config sets 'setting' to
    always, which would otherwise color the output even through a pipe."""
    repo = make_repo(tmp_path)
    repo.run("config", setting, "always")
    (tmp_path / "src" / "app.py").write_text("x = 2\n", encoding="utf-8")

    diff = repo.diff()

    assert "\x1b[" not in diff
    assert "+x = 2" in diff


def write(path: Path, text: str = "x\n") -> None:
    """Writes 'text' to 'path', making its folders first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def commit_all(repo: Repo, message: str) -> None:
    """Stages every file that git does not ignore and commits it."""
    repo.run("add", "-A")
    repo.run("commit", "-q", "-m", message)


def test_ignored_paths_list_an_ignored_file_and_a_folder_as_one_entry(tmp_path: Path) -> None:
    """Proves an ignored .env is listed as itself and an ignored .venv folder as the one entry
    '.venv/', and that nothing in .ratchet is listed, though git ignores it too."""
    repo = make_repo(tmp_path)
    write(tmp_path / ".gitignore", ".env\n.venv/\n.ratchet/\n")
    commit_all(repo, "ignore")
    write(tmp_path / ".env", "KEY=secret\n")
    write(tmp_path / ".venv" / "lib" / "x.py")
    write(tmp_path / ".ratchet" / "audits" / "a.json")

    assert repo.ignored_paths() == [".env", ".venv/"]


def test_ignored_paths_are_relative_to_a_project_in_a_subfolder(tmp_path: Path) -> None:
    """Proves the ignored paths of a project in the subfolder proj are relative to proj, and
    that its ignored .ratchet folder is left out."""
    repo = make_repo(tmp_path)
    write(tmp_path / "proj" / "a.py")
    write(tmp_path / "proj" / ".gitignore", ".env\n.ratchet/\n")
    commit_all(repo, "add proj")
    write(tmp_path / "proj" / ".env", "KEY=secret\n")
    write(tmp_path / "proj" / ".ratchet" / "rules.md")

    assert Repo(tmp_path / "proj").ignored_paths() == [".env"]


def test_ignored_paths_never_lists_the_project_itself(tmp_path: Path) -> None:
    """Proves a project folder proj2 that holds only ignored files is not listed as './', '.'
    or '', which would hide the whole project, and that its ignored .env is listed."""
    repo = make_repo(tmp_path)
    write(tmp_path / ".gitignore", ".env\n.ratchet/\n")
    commit_all(repo, "ignore")
    write(tmp_path / "proj2" / ".env", "KEY=secret\n")
    write(tmp_path / "proj2" / ".ratchet" / "rules.md")

    ignored = Repo(tmp_path / "proj2").ignored_paths()

    assert not {"./", ".", ""} & set(ignored)
    assert ignored == [".env"]
