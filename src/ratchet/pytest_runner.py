"""Runs a project's pytest suite, reads each test's outcome, and finds the tests that a change
made worse.

The suite's command comes from the project's config.toml. It must be a pytest command, since
the outcomes are read from the JUnit report that pytest writes when given --junitxml.
"""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from ratchet.git import find_program

# The lines of a long traceback that a report keeps from its start and from its end.
TRACE_HEAD = 15
TRACE_TAIL = 25
# The lines from the end of the log that a report shows when no outcome could be read.
LOG_TAIL = 60


class OutcomeKind(StrEnum):
    """Names how one test ended, or that it was not found."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"
    MISSING = "missing"


class Outcome(BaseModel):
    """Holds how one test ended, with the failure text of a test that did not pass."""

    kind: OutcomeKind
    text: str = ""


@dataclass
class PytestRun:
    """Holds what one run of the suite produced."""

    # Each test's outcome by its id, such as "tests.test_app::test_add". It is empty when pytest
    # crashed before writing its report.
    outcomes: dict[str, Outcome]
    # Everything pytest printed, and its exit code, which is -1 when it ran out of time.
    log: str
    code: int


def read_junit_report(path: Path) -> dict[str, Outcome]:
    """Returns each test's outcome from a JUnit report.

    Args:
        path: The report that pytest wrote.

    Returns:
        Each test's outcome by its id, or an empty dict when the report does not exist.
    """
    if not path.is_file():
        return {}
    outcomes = {}
    for case in ElementTree.parse(path).getroot().iter("testcase"):
        test_id = f"{case.get('classname')}::{case.get('name')}"
        outcome = Outcome(kind=OutcomeKind.PASSED)
        for tag, kind in (("failure", OutcomeKind.FAILED), ("error", OutcomeKind.ERROR), ("skipped", OutcomeKind.SKIPPED)):
            element = case.find(tag)
            if element is not None:
                text = (element.get("message") or "") + "\n" + (element.text or "")
                outcome = Outcome(kind=kind, text=text.strip())
                break
        outcomes[test_id] = outcome
    return outcomes


def run_pytest(command: list[str], cwd: Path, folder: Path, timeout_s: int) -> PytestRun:
    """Runs the whole suite, and runs it once more when pytest crashed without writing its
    report.

    Args:
        command: The pytest command, to which --junitxml is added.
        cwd: The folder to run the command in, which is the project's own folder.
        folder: The folder for the report, tests.xml, and the log, pytest.log.
        timeout_s: The longest one run may take, in seconds. A run that takes longer is not
            run again.

    Returns:
        The outcomes, the log and the exit code of the last run.
    """
    folder.mkdir(parents=True, exist_ok=True)
    junit = folder / "tests.xml"
    full = [find_program(command[0]), *command[1:], f"--junitxml={junit}"]
    for attempt in range(2):
        junit.unlink(missing_ok=True)
        try:
            finished = subprocess.run(
                full, capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=cwd, timeout=timeout_s,
            )
            log, code = finished.stdout + finished.stderr, finished.returncode
        except subprocess.TimeoutExpired as timeout:
            output = timeout.stdout or b""
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            log, code = f"the suite did not finish within {timeout_s} s\n{output}", -1
            break
        if junit.is_file():
            break
    (folder / "pytest.log").write_text(log, encoding="utf-8")
    return PytestRun(outcomes=read_junit_report(junit), log=log, code=code)


def find_regressions(baseline: dict[str, Outcome], now: dict[str, Outcome]) -> dict[str, Outcome]:
    """Returns each test that did worse than in the baseline, with how it ended now.

    A test that passed and no longer does, a skipped test that now fails, and a test that has
    disappeared all count. A new test counts when it does not pass.

    Args:
        baseline: The outcomes before the work started.
        now: The outcomes after the change.

    Returns:
        Each test that did worse, by its id, in the baseline's order and then the new tests.
    """
    worse = {}
    for test_id, before in baseline.items():
        after = now.get(test_id)
        if after is None:
            worse[test_id] = Outcome(
                kind=OutcomeKind.MISSING, text="the test no longer exists or was not collected"
            )
        elif before.kind == OutcomeKind.PASSED and after.kind != OutcomeKind.PASSED:
            worse[test_id] = after
        elif before.kind == OutcomeKind.SKIPPED and after.kind in (OutcomeKind.FAILED, OutcomeKind.ERROR):
            worse[test_id] = after
    for test_id, after in now.items():
        if test_id not in baseline and after.kind != OutcomeKind.PASSED:
            worse[test_id] = after
    return worse


def format_failure_report(worse: dict[str, Outcome], run: PytestRun, log_path: Path) -> str:
    """Returns the report on the tests that a change broke, for the implementer to work from.

    Args:
        worse: The tests that did worse, from find_regressions().
        run: The run that the tests did worse in.
        log_path: The file that holds the run's whole log.

    Returns:
        The report as markdown. When no outcome could be read, it ends with the log's last lines.
    """
    if worse:
        heading = f"# The tests failed ({len(worse)} newly failing, pytest exit code {run.code})"
    else:
        heading = f"# The test process crashed (pytest exit code {run.code})"
    lines = [heading, "", f"The full log is {log_path}.", ""]
    if not run.outcomes:
        lines += ["No test result could be read. The end of the log follows.", "", "```",
                  *run.log.splitlines()[-LOG_TAIL:], "```"]
    for test_id, outcome in worse.items():
        lines += [f"## {test_id}", "", f"Outcome: {outcome.kind}", ""]
        if outcome.text:
            trace = outcome.text.splitlines()
            if len(trace) > TRACE_HEAD + TRACE_TAIL:
                trace = [*trace[:TRACE_HEAD], "...", *trace[-TRACE_TAIL:]]
            lines += ["```", *trace, "```", ""]
    return "\n".join(lines)
