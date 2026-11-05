"""Checks ratchet init on scratch projects."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ratchet.cli import main
from ratchet.git import Repo
from ratchet.init_project import InitError, init_project
from ratchet.project import Project, load_project
from ratchet.records import records_log, records_repo
from ratchet.refactor.settings import COMMAND


@pytest.fixture
def plain(tmp_path: Path) -> Path:
    """Returns a committed git project with one source file and no .ratchet folder."""
    def git(*arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "ratchet@example.com")
    git("config", "user.name", "ratchet")
    git("config", "core.autocrlf", "false")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "start")
    return tmp_path


def project_status(root: Path) -> str:
    """Returns the project's own git status, which ignores the ignored .ratchet folder."""
    return Repo(root).run("status", "--porcelain")


def test_a_new_project_gets_files_a_repository_and_an_ignore_line(plain: Path) -> None:
    """Proves init writes both templates, makes .ratchet a repository with a commit, and commits
    the ignore line alone in the project."""
    actions = init_project(plain, assume_yes=False, confirm=None)

    assert actions
    assert (plain / ".ratchet" / "config.toml").is_file()
    assert (plain / ".ratchet" / "rules.md").read_text(encoding="utf-8").startswith(
        "<!-- ratchet: template.")
    assert (plain / ".gitignore").read_text(encoding="utf-8") == ".ratchet/\n"
    assert Repo(plain).run("show", "--name-only", "--format=%s", "HEAD").split() == [
        "chore:", "ignore", ".ratchet", ".gitignore"]
    assert project_status(plain) == ""
    tracked = set(records_repo(plain).run("ls-files").split())
    assert {"config.toml", "rules.md", ".gitignore"} <= tracked
    assert len(records_log(plain)) == 1


def test_the_written_files_load_as_a_project(plain: Path) -> None:
    """Proves the templates are a valid config.toml and a non-empty rules.md."""
    init_project(plain, assume_yes=False, confirm=None)

    project = load_project(plain, commands=[COMMAND])

    assert project.config.editable
    assert "ratchet init" not in project.rules


def test_a_second_run_changes_nothing(plain: Path) -> None:
    """Proves each step is skipped once it is done."""
    init_project(plain, assume_yes=False, confirm=None)
    head = Repo(plain).last_commit()
    records_head = records_repo(plain).last_commit()

    assert init_project(plain, assume_yes=False, confirm=None) == []
    assert Repo(plain).last_commit() == head
    assert records_repo(plain).last_commit() == records_head


def test_an_existing_rules_file_is_never_overwritten(plain: Path) -> None:
    """Proves init keeps the owner's rules.md and writes only what is missing."""
    (plain / ".ratchet").mkdir()
    (plain / ".ratchet" / "rules.md").write_text("Mine.\n", encoding="utf-8")

    init_project(plain, assume_yes=False, confirm=None)

    assert (plain / ".ratchet" / "rules.md").read_text(encoding="utf-8") == "Mine.\n"
    assert (plain / ".ratchet" / "config.toml").is_file()


def test_a_project_that_tracks_ratchet_is_moved_over(scratch: tuple[Project, Repo]) -> None:
    """Proves init untracks .ratchet in one project commit, keeps the files, and lists the old
    commits in the first records commit."""
    project, repo = scratch

    actions = init_project(project.root, assume_yes=True, confirm=None)

    assert repo.run("ls-files", "--", ".ratchet").strip() == ""
    assert (project.root / ".ratchet" / "rules.md").read_text(encoding="utf-8") == "Keep it small.\n"
    assert ".ratchet/" in (project.root / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert repo.run("log", "-1", "--format=%s").strip() == "chore: stop tracking .ratchet in this repository"
    assert repo.run("status", "--porcelain").strip() == ""
    assert any("stopped tracking" in action for action in actions)
    message = records_repo(project.root).run("log", "-1", "--format=%B")
    assert "Project commits that touched .ratchet:" in message
    assert " start" in message
    assert "problems-20261026-09h00m.md" in records_repo(project.root).run("ls-files")


def test_tracking_needs_consent_when_there_is_no_terminal(scratch: tuple[Project, Repo]) -> None:
    """Proves a tracked .ratchet without --yes and without a terminal stops before any change."""
    project, repo = scratch
    head = repo.last_commit()

    with pytest.raises(InitError, match="--yes") as caught:
        init_project(project.root, assume_yes=False, confirm=None)

    assert "git -C .ratchet restore ." in str(caught.value)
    assert repo.last_commit() == head
    assert not (project.root / ".ratchet" / ".git").exists()


def test_the_owner_can_agree_or_refuse_at_the_terminal(scratch: tuple[Project, Repo]) -> None:
    """Proves a no answer changes nothing and a yes answer moves the project over."""
    project, repo = scratch
    questions: list[str] = []

    with pytest.raises(InitError, match="nothing was changed"):
        init_project(project.root, assume_yes=False, confirm=lambda q: questions.append(q) or False)
    assert repo.run("ls-files", "--", ".ratchet").strip()

    init_project(project.root, assume_yes=False, confirm=lambda q: True)

    assert len(questions) == 1 and ".ratchet" in questions[0]
    assert repo.run("ls-files", "--", ".ratchet").strip() == ""


def test_the_warning_says_how_to_restore_the_records(scratch: tuple[Project, Repo]) -> None:
    """Proves the consent question says another checkout or clone can lose the files, and how to
    bring them back."""
    project, _ = scratch
    questions: list[str] = []

    init_project(project.root, assume_yes=False, confirm=lambda q: questions.append(q) or True)

    assert len(questions) == 1
    assert "checking out an older commit" in questions[0]
    assert "another clone" in questions[0]
    assert "git -C .ratchet restore ." in questions[0]
    assert "copy the .ratchet folder over" in questions[0]


def test_a_yes_run_reports_how_to_restore_the_records(scratch: tuple[Project, Repo]) -> None:
    """Proves a run with --yes, which asks nothing, still says how to restore the files."""
    project, _ = scratch

    actions = init_project(project.root, assume_yes=True, confirm=None)

    [line] = [action for action in actions if action.startswith("stopped tracking .ratchet")]
    assert "git -C .ratchet restore ." in line
    assert "another clone" in line


def test_a_tracked_project_that_already_ignores_ratchet_gets_one_ignore_line(
    scratch: tuple[Project, Repo]
) -> None:
    """Proves a project that tracks .ratchet and already ignores it ends with one ignore line."""
    project, repo = scratch
    ignore = project.root / ".gitignore"
    ignore.write_text(ignore.read_text(encoding="utf-8") + ".ratchet/\n", encoding="utf-8",
                      newline="\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=project.root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "ignore .ratchet"], cwd=project.root, check=True,
                   capture_output=True)

    init_project(project.root, assume_yes=True, confirm=None)

    assert ignore.read_text(encoding="utf-8").splitlines().count(".ratchet/") == 1
    assert repo.run("ls-files", "--", ".ratchet").strip() == ""
    assert repo.run("log", "-1", "--format=%s").strip() == "chore: stop tracking .ratchet in this repository"
    assert repo.run("status", "--porcelain").strip() == ""


def test_the_init_command_moves_a_tracked_project_and_prints_the_restore_note(
    scratch: tuple[Project, Repo], capsys: pytest.CaptureFixture[str]
) -> None:
    """Proves `ratchet init --yes` untracks .ratchet and prints how to restore the files. Stdin is
    not a terminal under pytest, so --yes is what lets it go ahead."""
    project, repo = scratch

    assert main(["init", "--project", str(project.root), "--yes"]) == 0

    lines = capsys.readouterr().out.splitlines()
    [line] = [line for line in lines if line.startswith("ratchet: stopped tracking .ratchet")]
    assert "git -C .ratchet restore ." in line
    assert repo.run("ls-files", "--", ".ratchet").strip() == ""


def test_a_project_with_changes_is_refused(plain: Path) -> None:
    """Proves init stops when the project has uncommitted changes."""
    (plain / "app.py").write_text("x = 2\n", encoding="utf-8")

    with pytest.raises(InitError, match="not committed"):
        init_project(plain, assume_yes=True, confirm=None)

    assert not (plain / ".ratchet").exists()


def test_a_folder_that_is_not_a_repository_is_refused(tmp_path: Path) -> None:
    """Proves init stops with a message when there is no git repository."""
    with pytest.raises(InitError, match="not a git repository"):
        init_project(tmp_path, assume_yes=True, confirm=None)


def test_the_init_command_prints_what_it_did(plain: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Proves `ratchet init` reports each change, and says so when there is nothing to do."""
    assert main(["init", "--project", str(plain)]) == 0
    assert "wrote .ratchet/rules.md" in capsys.readouterr().out

    assert main(["init", "--project", str(plain)]) == 0
    assert "nothing to do" in capsys.readouterr().out


def test_the_init_command_reports_an_error_on_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Proves a refused init prints one line and exits with 1."""
    assert main(["init", "--project", str(tmp_path)]) == 1
    assert capsys.readouterr().err.startswith("ratchet: ")
