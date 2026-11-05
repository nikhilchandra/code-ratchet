"""Checks the rules that code can check by itself after the implementer finishes, before the
tests run.

A step breaks a rule when it changes a file that agents may not change, changes one of the
.ratchet files that the agents work from, adds a skip or an expected failure to a test, adds an
import that breaks the target design or an import cycle, or claims to be done without changing
anything, or changes, renames or deletes a test that the test writer wrote.
"""

from __future__ import annotations

import re
from itertools import combinations
from pathlib import Path

from ratchet.architecture import check_architecture
from ratchet.git import Repo
from ratchet.project import Project, is_test_file
from ratchet.refactor.agent_answers import ImplementerAnswer, Status
from ratchet.target_design import TargetDesignError, read_target_design

# An added line in a test file that skips a test or expects it to fail.
SKIP_MARKER = re.compile(r"\b(skip|skipif|xfail)\b")


def snapshot_ratchet_files(project: Project) -> dict[Path, bytes]:
    """Returns the contents of the files directly in .ratchet, such as rules.md and
    the problem lists, so that a change to them can be found and put back.

    Args:
        project: The project, as read from its .ratchet folder.

    Returns:
        Each file's contents by its path. The state folder is left out.
    """
    return {path: path.read_bytes() for path in sorted(project.folder.iterdir()) if path.is_file()}


def find_violations(project: Project) -> list[str]:
    """Finds the violations of the target design and the import cycles in the project's code.

    A violation is named by its kind and the line of source that shows it, so that moving the
    line does not make it a new one. A module outside every part is named by its module, and a
    cycle is named by each pair of modules in it, so that shrinking a cycle adds nothing.

    Args:
        project: The project, as read from its .ratchet folder.

    Returns:
        The sorted names of the violations that break a rule in rules.md, and of the cycles. A
        rules.md without a readable target design leaves only the cycles.
    """
    try:
        design = read_target_design(project.rules)
    except TargetDesignError:
        design = None
    names = set()
    for finding in check_architecture(project, design):
        if finding.kind == "cycle":
            members = sorted({name for item in finding.evidence for name in item.shows.split(" imports ")})
            names.update(f"cycle: {first} and {second}" for first, second in combinations(members, 2))
        elif finding.kind == "no_part":
            names.add(f"no_part: {finding.subject}")
        elif finding.grade == "stated":
            names.update(f"{finding.kind}: {item.file}: {item.quote}" for item in finding.evidence)
    return sorted(names)


def check_rules(
    project: Project,
    repo: Repo,
    answer: ImplementerAnswer,
    inputs: dict[Path, bytes],
    violations_before: list[str] | None = None,
    written: dict[str, bytes] | None = None,
) -> str:
    """Returns why a step broke a rule that code can check, or an empty string.

    A .ratchet file that the step changed, and a written test that the step changed, renamed or
    deleted, is put back before the report is made. A written test that the step left as it
    was is not a change of the step, so the skip scan and the check for a step that changed
    nothing leave it out.

    Args:
        project: The project, as read from its .ratchet folder.
        repo: The project's repository, holding the step's uncommitted changes.
        answer: The implementer's answer for the step.
        inputs: The .ratchet files as snapshot_ratchet_files() returned them before the step.
        violations_before: The violations as find_violations() returned them before the step,
            or None to skip the check for new violations.
        written: The tests that the test writer wrote for the step, with the bytes it left, by
            their paths from the project's folder. None means there are none.

    Returns:
        A markdown report with one line per broken rule, or "" when no rule was broken.
    """
    problems = []
    written = written or {}
    for path, contents in inputs.items():
        if not path.is_file() or path.read_bytes() != contents:
            path.write_bytes(contents)
            name = path.relative_to(project.root).as_posix()
            problems.append(f"{name} was changed, and has been put back")

    for path, contents in written.items():
        file = project.root / path
        if not file.is_file() or file.read_bytes() != contents:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(contents)
            problems.append(f"{path} was written by the test writer, and the step changed, "
                            "renamed or deleted it; it has been put back")

    changed = [path for path in repo.changed_paths() if path not in written]
    for path in changed:
        if not project.may_change(path):
            problems.append(f"{path} is not among the files that a step may change")

    tests = [path for path in changed if is_test_file(path)]
    if tests:
        for line in repo.diff(*tests).splitlines():
            if line.startswith("+") and not line.startswith("+++") and SKIP_MARKER.search(line):
                problems.append(f"a test change adds a skip or an expected failure: {line[1:].strip()}")

    if violations_before is not None:
        old = set(violations_before)
        problems.extend(f"the step adds a violation, {name}" for name in find_violations(project)
                        if name not in old)

    if answer.status == Status.DONE and not changed:
        problems.append("the implementer says that it is done, but nothing changed")

    if not problems:
        return ""
    return "\n".join(["# The step broke a rule that the script checks", "",
                      *[f"- {problem}" for problem in problems]])
