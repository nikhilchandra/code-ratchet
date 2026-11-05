"""Checks and keeps the tests that the test writer writes before a step.

The test writer may only add new test files, and a test must not hold a skip or an expected
failure. The files it writes are saved under the job's folder with the bytes it left, so that
the checks after each attempt can put back any that the implementer changed.
"""

from __future__ import annotations

from pathlib import Path

from ratchet.git import Repo
from ratchet.project import Project, is_test_file
from ratchet.refactor.rule_checks import SKIP_MARKER

# The folder, inside a job's folder, that holds the saved copies of the written tests.
WRITTEN_FOLDER = "written"


def find_write_faults(project: Project, repo: Repo, asked: list[str]) -> tuple[list[str], list[str]]:
    """Finds what is wrong with the tests that the test writer left in the working tree.

    Args:
        project: The project, as read from its .ratchet folder.
        repo: The project's repository, holding the test writer's uncommitted changes.
        asked: The files that the super-planner asked for.

    Returns:
        The faults, one sentence each, and the new test files that the test writer wrote. The
        faults are empty when the writing may go on to the test run.
    """
    faults = []
    written = []
    for path in repo.changed_paths():
        if not (is_test_file(path) and project.may_change(path)):
            faults.append(f"{path} is not a new test file")
        elif repo.run("ls-tree", "--name-only", "HEAD", "--", path).strip():
            faults.append(f"{path} already exists at the last commit")
        else:
            written.append(path)
    faults += [f"{path} was asked for and does not exist" for path in asked
               if not (project.root / path).is_file()]
    for path in written:
        text = (project.root / path).read_text(encoding="utf-8", errors="replace")
        faults += [f"{path} adds a skip or an expected failure: {line.strip()}"
                   for line in text.splitlines() if SKIP_MARKER.search(line)]
    return faults, written


def save_written(project: Project, folder: Path, paths: list[str]) -> None:
    """Saves the written tests as they stand, each under the same path in the job's folder.

    Args:
        project: The project, as read from its .ratchet folder.
        folder: The job's folder.
        paths: The written tests' paths from the project's folder.
    """
    for path in paths:
        saved = folder / WRITTEN_FOLDER / path
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes((project.root / path).read_bytes())


def read_written(folder: Path, paths: list[str]) -> dict[str, bytes]:
    """Returns the saved bytes of the written tests, as save_written() kept them.

    Args:
        folder: The job's folder.
        paths: The written tests' paths from the project's folder.

    Returns:
        Each test's bytes by its path.
    """
    return {path: (folder / WRITTEN_FOLDER / path).read_bytes() for path in paths}
