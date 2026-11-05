"""Checks running a pytest suite, reading its outcomes and its coverage, finding regressions and
finding the added lines that no test runs, on scratch projects and on pretend pytest programs
that crash or hang."""

from __future__ import annotations

import sys
from pathlib import Path

from ratchet.pytest_runner import (
    OutcomeKind,
    Outcome,
    PytestRun,
    find_regressions,
    find_untested_lines,
    format_failure_report,
    format_untested_report,
    has_pytest_cov,
    read_coverage_report,
    run_pytest,
)

PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]

TESTS = """\
import pytest

def test_passes():
    assert 1 + 1 == 2

def test_fails():
    assert 1 + 1 == 3, "arithmetic is broken"

@pytest.mark.skip(reason="not today")
def test_skipped():
    pass
"""

# A pretend pytest that crashes without a report on its first run, and writes a report on the
# second. It counts its runs in a file beside itself.
FLAKY = """\
import sys
from pathlib import Path
count = Path(__file__).with_suffix(".count")
runs = int(count.read_text()) + 1 if count.exists() else 1
count.write_text(str(runs))
if runs == 1:
    print("Windows fatal exception: access violation")
    sys.exit(3221225477 % 256)
junit = next(arg for arg in sys.argv if arg.startswith("--junitxml=")).split("=", 1)[1]
Path(junit).write_text('<testsuites><testsuite><testcase classname="t" name="test_ok"/>'
                       '</testsuite></testsuites>')
"""


def outcome(kind: OutcomeKind, text: str = "") -> Outcome:
    """Returns an outcome, for short test tables."""
    return Outcome(kind=kind, text=text)


def test_a_real_run_reads_every_outcome(tmp_path: Path) -> None:
    """Proves a passing, a failing and a skipped test are each read, with the failure text."""
    (tmp_path / "test_sample.py").write_text(TESTS, encoding="utf-8")

    run = run_pytest(PYTEST, tmp_path, tmp_path / "out", timeout_s=120)

    kinds = {test_id.split("::")[1]: result.kind for test_id, result in run.outcomes.items()}
    assert kinds == {"test_passes": OutcomeKind.PASSED, "test_fails": OutcomeKind.FAILED,
                     "test_skipped": OutcomeKind.SKIPPED}
    assert "arithmetic is broken" in run.outcomes["test_sample::test_fails"].text
    assert run.code == 1
    assert (tmp_path / "out" / "pytest.log").read_text(encoding="utf-8") == run.log


def test_a_crash_is_run_once_more(tmp_path: Path) -> None:
    """Proves a run that crashes before writing its report is run again, and the second run's
    outcomes are kept."""
    (tmp_path / "flaky.py").write_text(FLAKY, encoding="utf-8")

    run = run_pytest([sys.executable, "flaky.py"], tmp_path, tmp_path / "out", timeout_s=60)

    assert (tmp_path / "flaky.count").read_text() == "2"
    assert run.outcomes == {"t::test_ok": outcome(OutcomeKind.PASSED)}


def test_two_crashes_give_no_outcomes_and_a_report_with_the_log(tmp_path: Path) -> None:
    """Proves a suite that crashes twice yields no outcomes, and that the report shows the end
    of the log, since there is nothing else to go on."""
    (tmp_path / "crash.py").write_text("print('segfault here')\nraise SystemExit(5)\n",
                                       encoding="utf-8")

    run = run_pytest([sys.executable, "crash.py"], tmp_path, tmp_path / "out", timeout_s=60)
    report = format_failure_report({}, run, tmp_path / "out" / "pytest.log")

    assert run.outcomes == {} and run.code == 5
    assert report.startswith("# The test process crashed (pytest exit code 5)")
    assert "segfault here" in report


def test_a_hang_is_stopped_and_not_run_again(tmp_path: Path) -> None:
    """Proves a suite that takes too long is stopped, reported, and not run a second time."""
    (tmp_path / "hang.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")

    run = run_pytest([sys.executable, "hang.py"], tmp_path, tmp_path / "out", timeout_s=1)

    assert run.code == -1 and run.outcomes == {}
    assert run.log.startswith("the suite did not finish within 1 s")


def test_regressions_count_only_tests_that_got_worse() -> None:
    """Proves which changes from the baseline count as worse and which do not."""
    baseline = {
        "broke": outcome(OutcomeKind.PASSED),
        "still_passes": outcome(OutcomeKind.PASSED),
        "was_failing": outcome(OutcomeKind.FAILED),
        "skip_now_fails": outcome(OutcomeKind.SKIPPED),
        "skip_now_passes": outcome(OutcomeKind.SKIPPED),
        "vanished": outcome(OutcomeKind.PASSED),
    }
    now = {
        "broke": outcome(OutcomeKind.ERROR, "boom"),
        "still_passes": outcome(OutcomeKind.PASSED),
        "was_failing": outcome(OutcomeKind.FAILED),
        "skip_now_fails": outcome(OutcomeKind.FAILED, "bad"),
        "skip_now_passes": outcome(OutcomeKind.PASSED),
        "new_passes": outcome(OutcomeKind.PASSED),
        "new_fails": outcome(OutcomeKind.FAILED, "new"),
    }

    worse = find_regressions(baseline, now)

    assert list(worse) == ["broke", "skip_now_fails", "vanished", "new_fails"]
    assert worse["vanished"].kind == OutcomeKind.MISSING


def test_the_report_shortens_a_long_traceback() -> None:
    """Proves a long failure keeps its first and last lines, so the report stays readable."""
    trace = "\n".join(f"line {number}" for number in range(100))
    run = PytestRun(outcomes={"t::test_a": outcome(OutcomeKind.FAILED, trace)}, log="", code=1)

    report = format_failure_report({"t::test_a": outcome(OutcomeKind.FAILED, trace)}, run, Path("pytest.log"))

    assert report.startswith("# The tests failed (1 newly failing, pytest exit code 1)")
    assert "line 14\n...\nline 75" in report
    assert "line 50" not in report


# A module with a branch that the test below it never takes.
CALC = """\
def sign(x):
    if x < 0:
        return -1
    return 1
"""
CALC_TEST = "from calc import sign\n\n\ndef test_sign():\n    assert sign(5) == 1\n"

# A diff that changes src/a.py, so that its new lines 3 and 4 are added, and adds a test file
# and a text file under src.
DIFF = '''\
diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1,3 +1,4 @@
 def f():
     """Doc."""
-    return 1
+    x = 1
+    return x
diff --git a/tests/test_a.py b/tests/test_a.py
new file mode 100644
--- /dev/null
+++ b/tests/test_a.py
@@ -0,0 +1,2 @@
+def test_a():
+    pass
diff --git a/src/notes.txt b/src/notes.txt
new file mode 100644
--- /dev/null
+++ b/src/notes.txt
@@ -0,0 +1 @@
+not code
'''

# A diff that adds a file under a src folder with no __init__.py, holding a comment, a blank
# line and two lines of code.
LOOSE = """\
diff --git a/src/loose/new.py b/src/loose/new.py
new file mode 100644
--- /dev/null
+++ b/src/loose/new.py
@@ -0,0 +1,4 @@
+# a note
+
+X = 1
+Y = 2
"""


def test_has_pytest_cov_sees_the_plugin_and_its_absence(tmp_path: Path) -> None:
    """Proves has_pytest_cov finds the plugin, and finds it missing when the command turns it
    off."""
    assert has_pytest_cov(PYTEST, tmp_path, 60) is True
    assert has_pytest_cov([*PYTEST, "-p", "no:pytest_cov"], tmp_path, 60) is False


def test_a_coverage_run_reads_the_missing_lines_and_leaves_no_data_file(tmp_path: Path) -> None:
    """Proves a coverage run reads the line that no test runs, writes its report beside the log,
    and leaves no coverage data file in the project's folder."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "calc.py").write_text(CALC, encoding="utf-8", newline="\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_calc.py").write_text(CALC_TEST, encoding="utf-8")
    (tmp_path / "pytest.ini").write_text("[pytest]\npythonpath = src\n", encoding="utf-8")
    untested = CALC.splitlines().index("        return -1") + 1

    run = run_pytest(PYTEST, tmp_path, tmp_path / "out", 60, coverage=True)

    assert run.coverage["src/calc.py"] == [untested]
    assert not (tmp_path / ".coverage").exists()
    assert (tmp_path / "out" / "coverage.json").exists()


def test_untested_lines_are_the_added_lines_the_report_misses() -> None:
    """Proves only the added lines of src .py files that the report lists as missing count, and
    that removed lines, files outside src and files that are not .py are left out."""
    assert find_untested_lines(DIFF, {"src/a.py": [4, 9]}) == {"src/a.py": [4]}


def test_an_added_file_missing_from_the_report_counts_its_code_lines() -> None:
    """Proves an added src file that the report does not hold counts every line that is not
    blank and not a comment."""
    assert find_untested_lines(LOOSE, {}) == {"src/loose/new.py": [3, 4]}


def test_the_untested_report_lists_each_file_and_its_lines() -> None:
    """Proves the report's heading counts the lines, and each file is listed with its lines."""
    report = format_untested_report({"src/a.py": [4, 7]}, Path("x/pytest.log"))

    assert report.startswith("# Lines that no test runs (2 lines)")
    assert "- `src/a.py`: lines 4, 7" in report


def test_a_missing_coverage_report_reads_as_empty(tmp_path: Path) -> None:
    """Proves a report that pytest-cov never wrote reads as no measured files."""
    assert read_coverage_report(tmp_path / "coverage.json") == {}


def test_has_pytest_cov_is_false_when_pytest_cannot_run(tmp_path: Path) -> None:
    """Proves a missing program, and a run that outlasts the timeout, both read as no plugin."""
    assert has_pytest_cov(["no-such-program-anywhere"], tmp_path, 60) is False
    assert has_pytest_cov([sys.executable, "-c", "import time; time.sleep(30)", "--"], tmp_path, 1) is False
