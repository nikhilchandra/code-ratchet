"""Checks the records repository on scratch projects."""

from __future__ import annotations

from pathlib import Path

import pytest

from ratchet.records import commit_records, has_records, init_records, records_log, records_repo


def write(path: Path, text: str = "x\n") -> None:
    """Writes a file, making its folder first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def tracked(root: Path) -> set[str]:
    """Returns the files that the records repository tracks."""
    return set(records_repo(root).run("ls-files").split())


@pytest.fixture
def no_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hides every global git setting, so that git has no name or email to commit with."""
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    for name in ("NAME", "EMAIL"):
        for kind in ("AUTHOR", "COMMITTER"):
            monkeypatch.delenv(f"GIT_{kind}_{name}", raising=False)
    monkeypatch.setenv("EMAIL", "")


def test_init_makes_a_repository_and_a_second_run_changes_nothing(tmp_path: Path) -> None:
    """Proves init makes .ratchet a repository with an ignore file, and a second call does
    nothing."""
    assert not has_records(tmp_path)
    assert init_records(tmp_path) is True
    assert has_records(tmp_path)
    assert (tmp_path / ".ratchet" / ".gitignore").is_file()
    assert init_records(tmp_path) is False


def test_ledgers_are_tracked_and_other_state_is_not(tmp_path: Path) -> None:
    """Proves the lists, the config, the rules and each ledger are tracked, and job and audit
    files under state are not."""
    init_records(tmp_path)
    folder = tmp_path / ".ratchet"
    list_state = folder / "state" / "problems-20261102-14h05m"
    write(folder / "config.toml")
    write(folder / "rules.md")
    write(folder / "comments.md")
    write(folder / "problems-20261102-14h05m.md")
    write(list_state / "ledger.json", "{}")
    write(list_state / "jobs" / "J01" / "job.json")
    write(list_state / "other.json")
    write(folder / "state" / "audits" / "audit-1.json")
    write(folder / "state" / "audits" / "audit-1" / "audit.log")

    assert commit_records(tmp_path, "first") is not None

    assert tracked(tmp_path) == {
        ".gitignore", "config.toml", "rules.md", "comments.md",
        "problems-20261102-14h05m.md", "state/problems-20261102-14h05m/ledger.json",
    }


def test_commit_works_with_no_git_identity(tmp_path: Path, no_identity: None) -> None:
    """Proves a commit succeeds on a machine where git has no name or email."""
    init_records(tmp_path)
    write(tmp_path / ".ratchet" / "rules.md")

    assert commit_records(tmp_path, "add rules") is not None

    author = records_repo(tmp_path).run("log", "-1", "--format=%an <%ae>").strip()
    assert author == "ratchet <ratchet@localhost>"


def test_commit_with_no_change_returns_none(tmp_path: Path) -> None:
    """Proves a commit with nothing new makes no commit."""
    init_records(tmp_path)
    write(tmp_path / ".ratchet" / "rules.md")
    assert commit_records(tmp_path, "one") is not None
    assert commit_records(tmp_path, "two") is None
    assert len(records_log(tmp_path)) == 1


def test_log_reads_back_newest_first_and_honors_the_limit(tmp_path: Path) -> None:
    """Proves the log lists messages newest first and stops at the limit."""
    init_records(tmp_path)
    for number in range(3):
        write(tmp_path / ".ratchet" / "rules.md", f"{number}\n")
        commit_records(tmp_path, f"change {number}")

    log = records_log(tmp_path)
    assert [line.split(" ", 3)[3] for line in log] == ["change 2", "change 1", "change 0"]
    assert len(records_log(tmp_path, limit=2)) == 2


def test_log_is_empty_without_a_repository_or_a_commit(tmp_path: Path) -> None:
    """Proves the log is empty for a project with no records repository and for one with no
    commit."""
    assert records_log(tmp_path) == []
    init_records(tmp_path)
    assert records_log(tmp_path) == []
