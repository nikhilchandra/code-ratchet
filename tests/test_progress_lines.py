"""Checks how the console's progress lines are laid out."""

from __future__ import annotations

from ratchet.refactor.progress_lines import count_of, format_step_lines, name_tests


def test_a_line_has_aligned_columns_and_the_cost_at_the_end() -> None:
    """Proves the job, problems and step sit in fixed columns, with the cost after the text."""
    (line,) = format_step_lines("job-007", "plan", "line totals come from pricing", problems="P3",
                                cost_usd=0.99, width=100)

    assert line == "job-007  P3     plan          line totals come from pricing  $0.99"


def test_a_line_too_wide_is_cut_and_keeps_its_cost() -> None:
    """Proves a long text is cut with an ellipsis so the line fits, without losing the cost."""
    (line,) = format_step_lines("job-1", "plan", "x" * 200, cost_usd=1.5, width=80, indent=7)

    assert len(line) == 80 - 7
    assert line.endswith("x...  $1.50")


def test_a_detail_is_quoted_wrapped_under_the_text_and_capped() -> None:
    """Proves a detail starts in the text's column, in quotes, on at most two lines."""
    lines = format_step_lines("job-008", "review", "reject: scope -> attempt 2",
                              detail="The diff renames a method. " * 20, width=80, indent=7)

    first, *details = lines
    column = first.index("reject")
    assert len(details) == 2
    assert details[0] == " " * column + details[0].strip()
    assert details[0].strip().startswith('"The diff renames')
    assert details[1].endswith(' ..."')
    assert all(len(line) <= 80 - 7 for line in details)


def test_failing_tests_are_named_shortly_and_the_rest_counted() -> None:
    """Proves a test id loses its package path, and only the first three are named."""
    ids = ["tests.unit.test_app::test_add", "tests.test_tax::TestRates::test_zero",
           "tests.test_x::test_a", "tests.test_x::test_b", "tests.test_x::test_c"]

    assert name_tests(ids[:1]) == "test_app::test_add"
    assert name_tests(ids) == "test_app::test_add, test_tax::TestRates::test_zero, test_x::test_a +2 more"


def test_counts_use_the_right_noun() -> None:
    """Proves one of a thing is singular, and a given plural replaces the s."""
    assert (count_of(1, "test"), count_of(2, "test")) == ("1 test", "2 tests")
    assert count_of(3, "fix", "fixes") == "3 fixes"
