"""Checks ratchet reopen on scratch projects that have run init."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ratchet.cli import main
from ratchet.init_project import init_project
from ratchet.problems_view import show_history
from ratchet.project import ProjectError, load_project
from ratchet.records import records_log
from ratchet.refactor.job_outcome import JobOutcome
from ratchet.refactor.ledger import (
    JobRecord, Ledger, ProblemRecord, ProblemStatus, load_ledger, save_ledger,
)
from ratchet.refactor.settings import COMMAND
from ratchet.reopen import reopen_problem

NAME = "problems-20261102-14h05m"

LIST = """\
# Problems

## Open

### P7. strip handling written twice

Status: open

The strip code is repeated.

## Questions for the owner

### Q2. Is the tax rounding deliberate?

## Dropped

### D1. rounding copies
"""


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """Returns a project that has run init and holds one list, with P7 marked abandoned."""
    def git(*arguments: str) -> None:
        subprocess.run(["git", *arguments], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "ratchet@example.com")
    git("config", "user.name", "ratchet")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "start")
    init_project(tmp_path, assume_yes=False, confirm=None)
    (tmp_path / ".ratchet" / f"{NAME}.md").write_text(LIST, encoding="utf-8")
    (tmp_path / ".ratchet" / "config.toml").write_text(
        'test_command = ["pytest"]\neditable = ["src/**"]\n', encoding="utf-8")
    save_ledger(Ledger(problem_list=NAME, problems={
        "P7": ProblemRecord(status=ProblemStatus.ABANDONED, sendbacks=3)}),
        tmp_path / ".ratchet" / "state" / NAME / "ledger.json")
    return tmp_path


def test_reopening_sets_open_clears_sendbacks_and_records_the_event(root: Path) -> None:
    """Proves the status, the sendbacks and the reopened event by the owner."""
    project = load_project(root, commands=[COMMAND])
    assert reopen_problem(project, "P7", "still repeated") == NAME
    record = load_ledger(project.state_dir / NAME / "ledger.json", NAME).problems["P7"]
    assert record.status == ProblemStatus.OPEN
    assert record.sendbacks == 0
    assert [(event.kind, event.by, event.note) for event in record.events] == [
        ("reopened", "owner", "still repeated")]


def test_reopening_commits_the_ledger_in_the_records_repository(root: Path) -> None:
    """Proves the commit message and that the ledger is committed."""
    project = load_project(root, commands=[COMMAND])
    reopen_problem(project, "P7", "still repeated")
    assert records_log(root)[0].endswith("reopen P7: still repeated")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root / ".ratchet",
                            check=True, capture_output=True, text=True).stdout
    assert status.strip() == ""


def test_the_history_shows_the_reopen(root: Path) -> None:
    """Proves `ratchet problems P7 --history` ends with the reopen."""
    project = load_project(root, commands=[COMMAND])
    reopen_problem(project, "P7", "still repeated")
    last = show_history(project, "P7").splitlines()[-1]
    assert last.endswith('reopened by the owner: "still repeated"')


def test_the_history_of_an_old_ledger_keeps_its_jobs_after_a_reopen(root: Path) -> None:
    """Proves a ledger from before problem events still shows its job once P7 is reopened."""
    project = load_project(root, commands=[COMMAND])
    save_ledger(Ledger(problem_list=NAME, problems={}, jobs=[JobRecord(
        id="J01", slug="J01", title="t", problems=["P7"], completes=["P7"],
        outcome=JobOutcome.COMMITTED, commit="6918463", attempts=1, cost_usd=0.0,
        ended="2026-11-02 16:20")]), project.state_dir / NAME / "ledger.json")
    reopen_problem(project, "P7", "still repeated")
    lines = show_history(project, "P7").splitlines()
    wording = "J01 committed 6918463, marked done"
    done = [i for i, line in enumerate(lines) if line.endswith(wording)]
    assert len(done) == 1
    assert lines[-1].endswith('reopened by the owner: "still repeated"')
    assert done[0] < len(lines) - 1


@pytest.mark.parametrize(("problem_id", "reason", "message"), [
    ("P99", "why", "does not hold P99"),
    ("Q2", "why", 'Q2 is not a problem, it is under "Questions for the owner"'),
    ("D1", "why", 'D1 is not a problem, it is under "Dropped"'),
    ("P7", "   ", "a reason is needed"),
])
def test_each_refusal_changes_nothing(root: Path, problem_id: str, reason: str,
                                      message: str) -> None:
    """Proves an unknown id, a Q or D id and an empty reason are refused with no commit."""
    project = load_project(root, commands=[COMMAND])
    before = records_log(root)
    with pytest.raises(ProjectError, match=message):
        reopen_problem(project, problem_id, reason)
    assert records_log(root) == before
    ledger = load_ledger(project.state_dir / NAME / "ledger.json", NAME)
    assert ledger.problems["P7"].status == ProblemStatus.ABANDONED


def test_a_project_that_has_not_run_init_is_refused(root: Path) -> None:
    """Proves a project with no records repository is told to run init."""
    project = load_project(root, commands=[COMMAND])
    (root / ".ratchet" / ".git").rename(root / ".ratchet" / "moved")
    with pytest.raises(ProjectError, match="ratchet init"):
        reopen_problem(project, "P7", "why")


def test_the_command_reopens_and_prints_errors_on_one_line(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Proves the command line exits with 0 on success and 1 with one line on a refusal."""
    assert main(["reopen", "P7", "still repeated", "--project", str(root)]) == 0
    assert capsys.readouterr().out == f"ratchet: reopened P7 in {NAME}\n"
    assert main(["reopen", "P99", "why", "--project", str(root)]) == 1
    assert capsys.readouterr().err == f"ratchet: {NAME} does not hold P99\n"
