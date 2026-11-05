"""Runs a project's pytest suite, reads each test's outcome, and finds the tests that a change
made worse and the added lines that no test runs.

The suite's command comes from the project's config.toml. It must be a pytest command, since
the outcomes are read from the JUnit report that pytest writes when given --junitxml. When
coverage is asked for, the lines that no test runs are read from the JSON report that pytest-cov
writes. A file that the project's coverage config omits is left out of that check, since no test
could be asked to cover it.
"""

from __future__ import annotations

import configparser
import json
import os
import re
import subprocess
import tomllib
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Sequence

from pydantic import BaseModel

from ratchet.git import find_program
from ratchet.unified_diff import diff_lines

# The lines of a long traceback that a report keeps from its start and from its end.
TRACE_HEAD = 15
TRACE_TAIL = 25
# The lines from the end of the log that a report shows when no outcome could be read.
LOG_TAIL = 60
# The folder, from the project's root, whose code coverage measures.
COVERED_FOLDER = "src"
# The files, from the project's root, that coverage reads its settings from, in the order that it
# takes them.
COVERAGE_CONFIGS = (".coveragerc", ".coveragerc.toml", "setup.cfg", "tox.ini", "pyproject.toml")
# The pieces of a coverage glob pattern, in the order they are tried. Each is a regex and what it
# becomes in the path regex, where None marks a piece that coverage does not allow.
GLOB_TOKENS = (
    (re.compile(r"\*\*\*+"), None),
    (re.compile(r"[^/]+\*\*+"), None),
    (re.compile(r"\*\*+[^/]+"), None),
    (re.compile(r"\*\*/\*\*"), None),
    (re.compile(r"^\*+/"), r"(.*[/\\\\])?"),
    (re.compile(r"/\*+$"), r"[/\\\\].*"),
    (re.compile(r"\*\*/"), r"(.*[/\\\\])?"),
    (re.compile(r"/"), r"[/\\\\]"),
    (re.compile(r"\*"), r"[^/\\\\]*"),
    (re.compile(r"\?"), r"[^/\\\\]"),
    (re.compile(r"\[.*?\]"), r"\g<0>"),
    (re.compile(r"[a-zA-Z0-9_-]+"), r"\g<0>"),
    (re.compile(r"[\[\]]"), None),
    (re.compile(r"."), r"\\\g<0>"),
)


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

    # Each test's outcome by its id, such as "tests.test_app::test_add", with the module read from
    # the project's folder whatever paths were run. It is empty when pytest crashed before writing
    # its report.
    outcomes: dict[str, Outcome]
    # Everything pytest printed, and its exit code, which is -1 when it ran out of time.
    log: str
    code: int
    # The lines that no test ran, in each file that coverage measured, by the file's path from the
    # project's folder with forward slashes. It is empty when coverage was not asked for.
    coverage: dict[str, list[int]] = field(default_factory=dict)
    # The patterns that the project's coverage config leaves out. It is empty when coverage was not
    # asked for.
    omit: list[str] = field(default_factory=list)


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


def outcomes_of_files(outcomes: dict[str, Outcome], paths: Sequence[str]) -> dict[str, Outcome]:
    """Returns the outcomes of the tests in the given files.

    A test's id starts with its file's path with dots in place of slashes and without the '.py',
    which pytest derives from the path relative to the rootdir that run_pytest() pins. A test in
    a class has the class after that module, and a file that failed to import is the id
    '::<module>'.

    Args:
        outcomes: Each test's outcome by its id, as PytestRun.outcomes holds them.
        paths: The files, by their paths from the project's folder with forward slashes.

    Returns:
        The outcomes whose ids belong to one of the files, in their order.
    """
    modules = [path.removesuffix(".py").replace("/", ".") for path in paths]
    return {test_id: outcome for test_id, outcome in outcomes.items()
            if any(test_id.split("::")[0] == module or test_id == f"::{module}"
                   or test_id.startswith(module + ".") for module in modules)}


def read_coverage_report(path: Path) -> dict[str, list[int]]:
    """Returns the lines that no test ran from a JSON report of pytest-cov.

    Args:
        path: The report that pytest-cov wrote.

    Returns:
        Each measured file's missing lines, sorted, by the file's path with forward slashes. It
        is empty when the report does not exist.
    """
    if not path.is_file():
        return {}
    files = json.loads(path.read_text(encoding="utf-8")).get("files", {})
    return {Path(name).as_posix(): sorted(data["missing_lines"]) for name, data in files.items()}


def split_coverage_list(value: str) -> list[str]:
    """Returns the parts of a coverage setting that holds a list as text.

    Args:
        value: The setting, whose parts are separated by newlines or commas.

    Returns:
        The parts with their surrounding space removed, without the empty ones.
    """
    return [part.strip() for part in re.split(r"[\n,]", value) if part.strip()]


def read_ini_omit(path: Path, prefixes: tuple[str, ...], required: bool) -> list[str] | None:
    """Returns the omit patterns of an ini style coverage config.

    Args:
        path: The config file.
        prefixes: The text that may come before 'run' and 'report' in a section's name.
        required: Whether the file is passed over unless it holds a section whose name starts
            with 'coverage:'.

    Returns:
        The patterns of the run section and then of the report section, or None when the file
        is passed over.
    """
    parser = configparser.RawConfigParser()
    parser.read(path, encoding="utf-8")
    if required and not any(name.startswith("coverage:") for name in parser.sections()):
        return None
    return [pattern for part in ("run", "report") for prefix in prefixes
            for pattern in split_coverage_list(parser.get(prefix + part, "omit", fallback=""))]


def read_toml_omit(path: Path, nested: bool) -> list[str] | None:
    """Returns the omit patterns of a TOML coverage config.

    Args:
        path: The config file.
        nested: Whether the settings can only be under [tool.coverage], as in pyproject.toml,
            and the file is passed over when that table is missing.

    Returns:
        The patterns of the run table and then of the report table, or None when the file is
        passed over. A pattern list given as one string is split like an ini value.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    tool = data.get("tool", {}).get("coverage")
    if nested and tool is None:
        return None
    # a .coveragerc.toml may hold the tables at its top level or under [tool.coverage]
    tables = [tool or {}] if nested else [data, tool or {}]
    omit = []
    for part in ("run", "report"):
        for table in tables:
            value = table.get(part, {}).get("omit", [])
            omit += split_coverage_list(value) if isinstance(value, str) else list(value)
    return omit


def read_coverage_omit(root: Path) -> list[str]:
    """Returns the patterns that the project's coverage config leaves out of the measuring.

    The first of the files in COVERAGE_CONFIGS that coverage would take is read, and no other.
    A .coveragerc and a .coveragerc.toml are taken whenever they exist. A setup.cfg or tox.ini is
    taken when it has a coverage section, and a pyproject.toml when it has a [tool.coverage]
    table.

    Args:
        root: The project's folder.

    Returns:
        The omit patterns of the run settings and then of the report settings. It is empty when
        no file is taken or the file taken cannot be parsed.
    """
    for name in COVERAGE_CONFIGS:
        path = root / name
        if not path.is_file():
            continue
        try:
            if name.endswith(".toml"):
                found = read_toml_omit(path, nested=name == "pyproject.toml")
            else:
                prefixes = ("", "coverage:") if name == ".coveragerc" else ("coverage:",)
                found = read_ini_omit(path, prefixes, required=name != ".coveragerc")
        except (configparser.Error, tomllib.TOMLDecodeError):
            return []
        if found is not None:
            return found
    return []


def glob_regex(pattern: str) -> str | None:
    """Returns the regex that matches the paths of a coverage glob pattern.

    The pattern is read the way coverage reads it. A '*' matches within one folder, a pattern
    that starts with '*/' or has no '/' matches in any folder, and a trailing '/*' matches
    everything below.

    Args:
        pattern: The glob pattern.

    Returns:
        The regex, or None when the pattern has a piece that coverage does not allow, such as
        '***'.
    """
    pattern = pattern.replace("\\", "/")
    if "/" not in pattern:
        pattern = "**/" + pattern
    pieces = []
    position = 0
    while position < len(pattern):
        for token, replacement in GLOB_TOKENS:
            match = token.match(pattern, position)
            if match:
                if replacement is None:
                    return None
                pieces.append(match.expand(replacement))
                position = match.end()
                break
    return "".join(pieces)


def is_omitted(path: str, root: Path, omit: Sequence[str]) -> bool:
    """Tells whether a coverage config leaves a file out.

    Args:
        path: The file's path from the project's folder.
        root: The project's folder.
        omit: The omit patterns of the coverage config. A pattern that does not start with '*' or
            '?' is also tried as a path from the project's folder, as coverage does.

    Returns:
        True when the whole of the file's absolute path matches one pattern. A pattern that
        coverage does not allow matches nothing.
    """
    full = (root / path).resolve().as_posix()
    flags = re.IGNORECASE if os.name == "nt" else 0
    for pattern in omit:
        forms = [pattern]
        if not pattern.startswith(("*", "?")):
            forms.append(os.path.abspath(root / pattern))
        for form in forms:
            regex = glob_regex(form)
            if regex is not None and re.fullmatch(regex, full, flags):
                return True
    return False


def has_pytest_cov(command: list[str], cwd: Path, timeout_s: int) -> bool:
    """Tells whether pytest-cov loads when the project's test command runs.

    Args:
        command: The pytest command.
        cwd: The folder to run the command in, which is the project's own folder.
        timeout_s: The longest the check may take, in seconds.

    Returns:
        True when pytest's help lists the --cov option, and False when it does not or pytest
        could not be run.
    """
    try:
        finished = subprocess.run(
            [find_program(command[0]), *command[1:], "--help"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=cwd, timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return "--cov" in finished.stdout


def run_pytest(
    command: list[str], cwd: Path, folder: Path, timeout_s: int, coverage: bool = False
) -> PytestRun:
    """Runs the whole suite, and runs it once more when pytest crashed without writing its
    report.

    Args:
        command: The pytest command, to which --rootdir and --junitxml are added. The rootdir is
            'cwd', so the test ids are read from the project's folder whatever paths the command
            names.
        cwd: The folder to run the command in, which is the project's own folder.
        folder: The folder for the report, tests.xml, and the log, pytest.log.
        timeout_s: The longest one run may take, in seconds. A run that takes longer is not
            run again.
        coverage: Whether to measure which lines of the src folder the tests run. The report,
            coverage.json, goes in 'folder', and so does the data file, so the project's folder
            gets no .coverage file.

    Returns:
        The outcomes, the log and the exit code of the last run, and, when coverage was asked for,
        the missing lines of each measured file and the project's coverage omit patterns.
    """
    folder.mkdir(parents=True, exist_ok=True)
    junit = folder / "tests.xml"
    report = folder / "coverage.json"
    full = [find_program(command[0]), *command[1:], f"--rootdir={cwd}", f"--junitxml={junit}"]
    env = None
    if coverage:
        full += [f"--cov={COVERED_FOLDER}", f"--cov-report=json:{report}"]
        env = {**os.environ, "COVERAGE_FILE": str(folder / ".coverage")}
    for attempt in range(2):
        junit.unlink(missing_ok=True)
        report.unlink(missing_ok=True)
        try:
            finished = subprocess.run(
                full, capture_output=True, text=True, encoding="utf-8", errors="replace",
                cwd=cwd, timeout=timeout_s, env=env,
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
    return PytestRun(outcomes=read_junit_report(junit), log=log, code=code,
                     coverage=read_coverage_report(report) if coverage else {},
                     omit=read_coverage_omit(cwd) if coverage else [])


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


def find_untested_lines(
    diff: str, missing: dict[str, list[int]], root: Path = Path(), omit: Sequence[str] = ()
) -> dict[str, list[int]]:
    """Returns the added lines of src code that no test runs.

    Only the .py files in the src folder count. An added line counts when the coverage report
    lists it as missing. A file that the report does not hold at all was either left out by the
    coverage config or never measured. One that an omit pattern matches is skipped, since coverage
    leaves it out on purpose and no test could cover it. Any other, such as a file in a folder
    with no __init__.py, was never measured, so every line it adds counts except blank lines and
    comments. An empty report is not checked here: the caller skips it, since nothing was
    measured.

    Args:
        diff: The unified diff of the change.
        missing: The missing lines of each measured file, as PytestRun.coverage holds them.
        root: The project's folder, which the omit patterns are matched against.
        omit: The omit patterns of the coverage config, as PytestRun.omit holds them.

    Returns:
        The untested added lines of each file, in order, by the file's path. A file with none is
        left out.
    """
    untested: dict[str, list[int]] = {}
    skipped: dict[str, bool] = {}
    for line in diff_lines(diff):
        path = line.new_path
        if line.marker != "+" or not path or not (path.startswith(f"{COVERED_FOLDER}/") and path.endswith(".py")):
            continue
        code = line.text[1:].strip()
        if path in missing:
            untested_line = line.new_line in missing[path]
        else:
            if path not in skipped:
                skipped[path] = is_omitted(path, root, omit)
            untested_line = bool(code) and not code.startswith("#") and not skipped[path]
        if untested_line:
            untested.setdefault(path, []).append(line.new_line)
    return untested


def format_untested_report(untested: dict[str, list[int]], log_path: Path) -> str:
    """Returns the report on the added lines that no test runs, for the implementer to work from.

    Args:
        untested: The untested lines of each file, from find_untested_lines().
        log_path: The file that holds the run's whole log.

    Returns:
        The report as markdown.
    """
    total = sum(len(numbers) for numbers in untested.values())
    lines = [f"# Lines that no test runs ({total} {'line' if total == 1 else 'lines'})", "",
             "Every line that the change adds or changes in src must be run by some test. "
             "Write a test that runs each line below, or remove the line.", "",
             f"The full log is {log_path}.", ""]
    for name, numbers in untested.items():
        lines.append(f"- `{name}`: lines {', '.join(str(number) for number in numbers)}")
    return "\n".join(lines) + "\n"
