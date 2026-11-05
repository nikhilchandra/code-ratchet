"""Checks the ratchet command line."""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest

from ratchet.cli import main


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
