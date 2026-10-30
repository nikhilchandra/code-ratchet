"""Checks the rules that code can check by itself after the implementer finishes, before the
tests run.

A step breaks a rule when it changes a file that agents may not change, changes one of the
.ratchet files that the agents work from, adds a skip or an expected failure to a test, or
claims to be done without changing anything.
"""

from __future__ import annotations

import re
from pathlib import Path

from ratchet.git import Repo
from ratchet.project import Project
from ratchet.refactor.agent_answers import ImplementerAnswer, Status

# An added line in a test file that skips a test or expects it to fail.
SKIP_MARKER = re.compile(r"\b(skip|skipif|xfail)\b")


def snapshot_ratchet_files(project: Project) -> dict[Path, bytes]:
    """Returns the contents of the files directly in .ratchet, such as rules.md and
    problems.md, so that a change to them can be found and put back.

    Args:
        project: The project, as read from its .ratchet folder.

    Returns:
        Each file's contents by its path. The state folder is left out.
    """
    return {path: path.read_bytes() for path in sorted(project.folder.iterdir()) if path.is_file()}


def _is_test_file(path: str) -> bool:
    """Answers whether a changed file holds tests, by pytest's naming.

    Args:
        path: The file's path relative to the project.

    Returns:
        True for a Python file under a tests folder or named test_*.py or *_test.py.
    """
    parts = path.replace("\\", "/").split("/")
    name = parts[-1]
    return name.endswith(".py") and (
        "tests" in parts[:-1] or name.startswith("test_") or name.endswith("_test.py")
    )


def check_rules(
    project: Project, repo: Repo, answer: ImplementerAnswer, inputs: dict[Path, bytes]
) -> str:
    """Returns why a step broke a rule that code can check, or an empty string.

    A .ratchet file that the step changed is put back before the report is made.

    Args:
        project: The project, as read from its .ratchet folder.
        repo: The project's repository, holding the step's uncommitted changes.
        answer: The implementer's answer for the step.
        inputs: The .ratchet files as snapshot_ratchet_files() returned them before the step.

    Returns:
        A markdown report with one line per broken rule, or "" when no rule was broken.
    """
    problems = []
    for path, contents in inputs.items():
        if not path.is_file() or path.read_bytes() != contents:
            path.write_bytes(contents)
            name = path.relative_to(project.root).as_posix()
            problems.append(f"{name} was changed, and has been put back")

    changed = repo.changed_paths()
    for path in changed:
        if not project.may_change(path):
            problems.append(f"{path} is not among the files that a step may change")

    tests = [path for path in changed if _is_test_file(path)]
    if tests:
        for line in repo.diff(*tests).splitlines():
            if line.startswith("+") and not line.startswith("+++") and SKIP_MARKER.search(line):
                problems.append(f"a test change adds a skip or an expected failure: {line[1:].strip()}")

    if answer.status == Status.DONE and not changed:
        problems.append("the implementer says that it is done, but nothing changed")

    if not problems:
        return ""
    return "\n".join(["# The step broke a rule that the script checks", "",
                      *[f"- {problem}" for problem in problems]])
