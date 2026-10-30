"""Checks how the newest problem list is found and read, and how a wrong one is reported."""

from __future__ import annotations

from datetime import datetime

import pytest

from ratchet.git import Repo
from ratchet.project import Project, ProjectError
from ratchet.refactor.problem_list import find_latest_problem_list


def write(project: Project, name: str, text: str = "## P1. Something\n") -> None:
    """Writes a file into the project's .ratchet folder."""
    (project.folder / name).write_text(text, encoding="utf-8")


def test_the_newest_list_is_picked_by_the_time_in_its_name(scratch: tuple[Project, Repo]) -> None:
    """Proves the list with the latest date and time wins, whatever order the files are in."""
    project, _ = scratch
    write(project, "problems-20261027-08h05m.md", "## P7. Newest\n### P8. Also newest\n")
    write(project, "problems-20261026-23h59m.md")

    latest = find_latest_problem_list(project)

    assert latest.name == "problems-20261027-08h05m"
    assert latest.created == datetime(2026, 10, 27, 8, 5)
    assert latest.ids == ["P7", "P8"]
    assert latest.relative_path == ".ratchet/problems-20261027-08h05m.md"


def test_ids_come_from_headings_only_and_once_each(scratch: tuple[Project, Repo]) -> None:
    """Proves an id is read from a heading at any depth, and not from a mention in the text."""
    project, _ = scratch
    write(project, "problems-20261027-08h05m.md",
          "# Problems\n\n#### P3. First\n\nWaits on P9.\n\n## P4 Second\n\n## P3. Again\n")

    assert find_latest_problem_list(project).ids == ["P3", "P4"]


@pytest.mark.parametrize("name", ["problems.md", "problems-2026-10-27.md",
                                  "problems-20261327-08h05m.md", "problems-20261027-8h5m.md"])
def test_a_misnamed_list_is_named_rather_than_skipped(scratch: tuple[Project, Repo], name: str) -> None:
    """Proves a file that looks like a problem list but is misnamed, or holds a date that does
    not exist, stops ratchet with the file's name."""
    project, _ = scratch
    write(project, name)

    with pytest.raises(ProjectError, match=f"misnamed: {name}"):
        find_latest_problem_list(project)


def test_no_list_is_reported(scratch: tuple[Project, Repo]) -> None:
    """Proves a folder without a problem list says how to name one."""
    project, _ = scratch
    for path in project.folder.glob("problems*.md"):
        path.unlink()

    with pytest.raises(ProjectError, match="has no problem list.*problems-20261026-14h32m.md"):
        find_latest_problem_list(project)


def test_a_list_without_problem_headings_is_refused(scratch: tuple[Project, Repo]) -> None:
    """Proves the newest list must give each problem a heading that starts with its id."""
    project, _ = scratch
    write(project, "problems-20261027-08h05m.md", "# Problems\n\n- the controller is too big\n")

    with pytest.raises(ProjectError, match="has no problem headings"):
        find_latest_problem_list(project)


def test_an_empty_list_is_refused(scratch: tuple[Project, Repo]) -> None:
    """Proves an empty newest list is reported as empty."""
    project, _ = scratch
    write(project, "problems-20261027-08h05m.md", "\n")

    with pytest.raises(ProjectError, match="problems-20261027-08h05m.md is empty"):
        find_latest_problem_list(project)
