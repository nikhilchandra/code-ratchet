"""Checks that RESTORE_NOTE in src/ratchet/init_project.py and the docstrings of three audit
functions fit the house line width, and that RESTORE_NOTE's text stays the same.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

import ratchet.init_project
from ratchet.audit.agent_profiles import make_agents
from ratchet.audit.code_copy import make_copy
from ratchet.audit.turn_logs import paths_named
from ratchet.project import FOLDER

# The source file of ratchet.init_project, which these tests read.
SOURCE = Path(ratchet.init_project.__file__)

# The widest line, in characters, that the house style allows.
WIDTH = 100


def source_lines() -> list[str]:
    """Returns the lines of src/ratchet/init_project.py."""
    return SOURCE.read_text(encoding="utf-8").splitlines()


def restore_note_lines() -> list[str]:
    """Returns the source lines of the RESTORE_NOTE statement, from the line that starts
    'RESTORE_NOTE =' up to the first blank line after it.
    """
    lines = source_lines()
    start = next(n for n, line in enumerate(lines) if line.startswith("RESTORE_NOTE ="))
    statement = []
    for line in lines[start:]:
        if not line.strip():
            break
        statement.append(line)
    return statement


def test_restore_note_is_unchanged():
    """RESTORE_NOTE holds the same sentences, with FOLDER filled in, however it is wrapped."""
    assert ratchet.init_project.RESTORE_NOTE == (
        "The files stay on disk in this clone, but checking out an older commit can replace or"
        " delete them, and pulling this commit into another clone deletes them there. In this"
        f" clone, git -C {FOLDER} restore . brings them back from the records repository."
        f" Another clone has no records repository, so copy the {FOLDER} folder over from this"
        " one."
    )


def test_restore_note_fits_100_columns():
    """Every source line of the RESTORE_NOTE statement is at most WIDTH columns."""
    long_lines = [f"{len(line)}: {line}" for line in restore_note_lines() if len(line) > WIDTH]
    assert long_lines == []


def test_restore_note_has_one_blank_line_before_its_comment():
    """One blank line, not two, separates the IGNORE_FORMS line from RESTORE_NOTE's comment."""
    lines = source_lines()
    comment = next(n for n, line in enumerate(lines) if line.startswith("# What the owner is told"))
    assert lines[comment - 1] == ""
    assert lines[comment - 2].startswith("IGNORE_FORMS =")


@pytest.mark.parametrize("function", [make_copy, make_agents, paths_named])
def test_docstring_fits_100_columns(function):
    """Every line of the function's docstring, counting its indentation, is at most WIDTH
    columns.
    """
    docstring = inspect.getsource(function).split('"""')[1]
    long_lines = [f"{len(line)}: {line}" for line in docstring.splitlines() if len(line) > WIDTH]
    assert long_lines == []
