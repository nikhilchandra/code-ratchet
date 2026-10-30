"""Checks running a pytest suite, reading its outcomes and finding regressions, on scratch
projects and on pretend pytest programs that crash or hang."""

from __future__ import annotations

import sys
from pathlib import Path

from ratchet.pytest_runner import OutcomeKind, Outcome, PytestRun, format_failure_report, find_regressions, run_pytest

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
