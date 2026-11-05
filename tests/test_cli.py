"""Checks the ratchet command line."""

from __future__ import annotations

from functools import partial
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from ratchet import cli
from ratchet.cli import main
from ratchet.project import Project
from lock_files import FAKE_PID, LIST, write_job_file, write_lock
from test_audit_graph import audit_project  # noqa: F401


def test_the_ratchet_command_is_installed() -> None:
    """Proves installing the project gives a ratchet command that calls main()."""
    (command,) = [point for point in entry_points(group="console_scripts") if point.name == "ratchet"]

    assert command.value == "ratchet.cli:main"


def test_refactor_help_lists_its_options(capsys: pytest.CaptureFixture[str]) -> None:
    """Proves `ratchet refactor --help` works and shows the options."""
    with pytest.raises(SystemExit) as stop:
        main(["refactor", "--help"])

    out = capsys.readouterr().out
    assert stop.value.code == 0
    assert "--max-jobs" in out and "--focus" in out and "--project" in out


def test_a_project_without_ratchet_fails_with_a_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Proves an error is printed as one line, with exit code 1, rather than as a traceback."""
    code = main(["refactor", "--project", str(tmp_path)])

    assert code == 1
    assert capsys.readouterr().err.startswith(f"ratchet: {tmp_path} has no .ratchet folder")


def write_project_with_one_list(root: Path) -> None:
    """Writes a .ratchet folder whose only list holds the problem P1."""
    folder = root / ".ratchet"
    folder.mkdir()
    (folder / "config.toml").write_text('test_command = ["pytest"]\neditable = ["src/**"]\n',
                                        encoding="utf-8")
    (folder / "rules.md").write_text("rules\n", encoding="utf-8")
    (folder / "problems-20261102-14h05m.md").write_text("## P1. first thing\n", encoding="utf-8")


def test_problems_shows_the_newest_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Proves `ratchet problems` prints the list's problems and exits with 0."""
    write_project_with_one_list(tmp_path)

    code = main(["problems", "--project", str(tmp_path)])

    assert code == 0
    assert capsys.readouterr().out.splitlines()[0] == "P1  open  -  first thing"


def test_problems_errors_print_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Proves a missing id, --history without an id and --all with an id each exit with 1."""
    write_project_with_one_list(tmp_path)
    project = str(tmp_path)

    assert main(["problems", "P9", "--project", project]) == 1
    assert main(["problems", "--history", "--project", project]) == 1
    assert main(["problems", "P1", "--all", "--project", project]) == 1

    errors = capsys.readouterr().err.splitlines()
    assert len(errors) == 3
    assert all(line.startswith("ratchet: ") for line in errors)


def test_audit_decisions_cannot_be_combined_with_a_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Proves --approve with -p or --fresh exits with 1, and --approve with --discard is refused
    by the parser."""
    write_project_with_one_list(tmp_path)
    project = str(tmp_path)

    assert main(["audit", "--approve", "-p", "x", "--project", project]) == 1
    assert main(["audit", "--discard", "--fresh", "--project", project]) == 1
    with pytest.raises(SystemExit) as stop:
        main(["audit", "--approve", "--discard", "--project", project])

    assert stop.value.code == 2
    errors = capsys.readouterr().err
    assert errors.count("cannot be combined with -p or --fresh") == 2


def test_audit_approve_without_a_waiting_audit_prints_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Proves --approve with no waiting audit exits with 1 and one message."""
    write_project_with_one_list(tmp_path)

    code = main(["audit", "--approve", "--project", str(tmp_path)])

    assert code == 1
    assert capsys.readouterr().err.startswith("ratchet: no audit is waiting")


def test_audit_before_init_prints_one_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Proves an audit in a project that has not run init exits with 1 and names init."""
    write_project_with_one_list(tmp_path)

    code = main(["audit", "--project", str(tmp_path)])

    assert code == 1
    assert "ratchet init" in capsys.readouterr().err


def test_audit_with_a_malformed_rules_block_prints_one_line(
    audit_project: Project, capsys: pytest.CaptureFixture[str]  # noqa: F811
) -> None:
    """Proves an audit whose rules.md has a toml block that does not parse exits with 1 and one
    message."""
    (audit_project.root / ".ratchet" / "rules.md").write_text(
        'Keep it small.\n\n```toml\n[parts\ncore = "app.core"\n```\n', encoding="utf-8",
        newline="\n")

    code = main(["audit", "--project", str(audit_project.root)])

    err = capsys.readouterr().err.strip()
    assert code == 1
    assert len(err.splitlines()) == 1
    assert err.startswith("ratchet: the toml block in rules.md is not valid")


def refuse_agents(*args: object) -> dict:
    """Stands in for the agent maker, so that a run the lock fails to stop calls no real agent."""
    raise AssertionError("the run asked for its agents, so the lock did not stop it")


@pytest.fixture
def no_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    """Has the command line start audits and refactors with refuse_agents as their agent maker."""
    monkeypatch.setattr(cli, "start_audit", partial(cli.start_audit, agent_maker=refuse_agents))
    monkeypatch.setattr(cli, "run_refactor", partial(cli.run_refactor, agent_maker=refuse_agents))


def test_audit_against_a_running_refactor_prints_one_line(
    audit_project: Project, capsys: pytest.CaptureFixture[str], no_agents  # noqa: F811
) -> None:
    """Proves an audit started while a refactor runs in a live process exits with 1 and one
    message that names the refactor's job."""
    write_job_file(audit_project.state_dir / LIST / "refactor" / "job-012.json", "running",
                   "implement")
    write_lock(audit_project)

    code = main(["audit", "--project", str(audit_project.root)])

    assert code == 1
    assert capsys.readouterr().err.splitlines() == [
        "ratchet: A refactor (job-012) is running. Wait for it to finish."]


def test_refactor_against_a_waiting_audit_prints_one_line(
    scratch, capsys: pytest.CaptureFixture[str], no_agents
) -> None:
    """Proves a refactor started while an audit waits for the owner's answers exits with 1 and
    one message that names the audit."""
    project, _ = scratch
    write_job_file(project.state_dir / "audits" / "audit-x.json", "waiting", "check")
    write_lock(project, graph="audit", job="audit-x", job_file="audits/audit-x.json",
               pid=FAKE_PID, state="waiting", gate="questions")

    code = main(["refactor", "--project", str(project.root)])

    assert code == 1
    assert capsys.readouterr().err.splitlines() == [
        "ratchet: Audit audit-x is waiting for your answers. Finish or discard it first."]
