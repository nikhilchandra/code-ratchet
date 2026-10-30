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
