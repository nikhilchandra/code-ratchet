"""Formats the console's progress lines: one aligned line per step of a job, with an optional
quoted detail beneath it.

A line reads like this, after the time that the run's log puts in front:

    job-007  P3     plan          line totals come from pricing             $0.99
    job-008         review        reject: scope; check 2 unmet -> attempt 3  $0.16
                                  "The diff also renames _round_cents, which the plan does not
                                  name."

The details behind each line stay in the job's folder.
"""

from __future__ import annotations

import shutil
import textwrap

# The widths of the job, problems and step columns.
JOB_WIDTH = 8
PROBLEMS_WIDTH = 6
STEP_WIDTH = 13
# The narrowest console that the lines are fitted to, and the most lines a detail may take.
MIN_WIDTH = 80
DETAIL_LINES = 2
# How many failing tests a line names before it gives the count of the rest.
TESTS_NAMED = 3


def count_of(number: int, noun: str, plural: str = "") -> str:
    """Returns a number with its noun, such as "1 test" or "3 tests".

    Args:
        number: How many there are.
        noun: The noun in the singular.
        plural: The noun in the plural, when it is not the singular with an s, such as "fixes".

    Returns:
        The number and the noun, in the plural unless the number is 1.
    """
    return f"{number} {noun if number == 1 else plural or noun + 's'}"


def console_width() -> int:
    """Returns the console's width in characters, and never less than MIN_WIDTH."""
    return max(shutil.get_terminal_size((100, 24)).columns, MIN_WIDTH)


def format_step_lines(
    job: str,
    step: str,
    text: str,
    *,
    problems: str = "",
    cost_usd: float | None = None,
    detail: str = "",
    width: int,
    indent: int = 0,
) -> list[str]:
    """Returns the lines for one step: the step's line, and the detail wrapped beneath it.

    Args:
        job: The job's name, such as "job-007".
        step: The step's name, such as "review".
        text: What happened, in a few words.
        problems: The problem ids the job works on, shown on the plan's line only.
        cost_usd: What the step's agent call cost, in dollars, or None for a step with no agent.
        detail: A longer text shown in quotes beneath the line, or "".
        width: The console's width in characters.
        indent: The characters that the log puts in front of every line, which the lines are
            fitted around.

    Returns:
        The step's line, cut with "..." when it is too wide, then up to DETAIL_LINES lines of
        detail.
    """
    head = f"{job:<{JOB_WIDTH}} {problems:<{PROBLEMS_WIDTH}} {step:<{STEP_WIDTH}} "
    cost = f"  ${cost_usd:.2f}" if cost_usd is not None else ""
    room = width - indent - len(head) - len(cost)
    if len(text) > room:
        text = text[: max(room - 3, 0)] + "..."
    lines = [head + text + cost]
    if detail:
        # the log puts 'indent' spaces before these lines, as it puts the time before the first
        column = " " * len(head)
        wrapped = textwrap.wrap(f'"{" ".join(detail.split())}"', width - indent - len(column),
                                max_lines=DETAIL_LINES, placeholder=' ..."')
        lines += [column + line for line in wrapped]
    return lines


def name_tests(test_ids: list[str]) -> str:
    """Returns the failing tests for a line, naming the first few and counting the rest.

    Args:
        test_ids: Test ids as the JUnit report gives them, such as "tests.test_app::test_add".

    Returns:
        Names such as "test_app::test_add, test_app::test_sub +2 more".
    """
    short = [f"{test_id.split('::')[0].rsplit('.', 1)[-1]}::{test_id.split('::', 1)[-1]}"
             if "::" in test_id else test_id for test_id in test_ids]
    named = ", ".join(short[:TESTS_NAMED])
    rest = len(short) - TESTS_NAMED
    return f"{named} +{rest} more" if rest > 0 else named
