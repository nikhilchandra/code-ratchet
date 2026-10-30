"""Turns a review into the records and reports that follow it: which of the plan's checks went
unmet, the record of every check that is saved beside the attempt, and the report that sends a
rejected step back to the implementer."""

from __future__ import annotations

from ratchet.refactor.agent_answers import Plan, Review


def find_unmet_checks(plan: Plan, review: Review) -> list[int]:
    """Returns the number of each of the plan's checks that the reviewer did not confirm.

    Args:
        plan: The plan, whose checks are numbered from 1.
        review: The review, which answers the checks by number.

    Returns:
        The unmet checks' numbers, in order. A check the reviewer did not answer is unmet.
    """
    confirmed = {answer.check for answer in review.plan_checks if answer.passed}
    return [number for number in range(1, len(plan.checks) + 1) if number not in confirmed]


def format_checks_record(plan: Plan, review: Review) -> str:
    """Returns each of the plan's checks with its number, whether it was met, and the evidence.

    Args:
        plan: The plan.
        review: The review.

    Returns:
        The record as markdown.
    """
    answers = {answer.check: answer for answer in review.plan_checks}
    unmet = find_unmet_checks(plan, review)
    lines = ["# The planner's checks", ""]
    for number, text in enumerate(plan.checks, 1):
        answer = answers.get(number)
        evidence = answer.evidence if answer else "the reviewer gave no answer"
        lines += [f"{number}. {'not met' if number in unmet else 'met'}: {text}", f"   {evidence}", ""]
    return "\n".join(lines)


def format_review_report(plan: Plan, review: Review) -> str:
    """Returns the unmet checks, the must-fix findings and the comment fixes as a report for the
    implementer.

    Args:
        plan: The plan.
        review: The review that rejected the step.

    Returns:
        The report as markdown.
    """
    lines = ["# The step was rejected", "", review.summary, ""]
    unmet = find_unmet_checks(plan, review)
    if unmet:
        answers = {answer.check: answer for answer in review.plan_checks}
        lines += ["## The planner's checks", ""]
        for number in unmet:
            answer = answers.get(number)
            found = f"The reviewer found: {answer.evidence}" if answer else "The reviewer did not confirm it."
            lines += [f"- Check {number}: {plan.checks[number - 1]}", f"  {found}"]
        lines.append("")
    for name, criterion in review.criteria:
        if criterion.passed:
            continue
        lines += [f"## {name}", ""]
        for finding in criterion.findings:
            lines.append(f"- `{finding.file}:{finding.line}` {finding.problem} Fix: {finding.fix}")
        lines.append("")
    if review.comment_fixes:
        lines += ["## The comment fixes", "",
                  "The reviewer wrote these replacements. Make each one exactly as written.", ""]
        for fix in review.comment_fixes:
            lines += [f"In `{fix.file}`, replace", "", "```", fix.old, "```", "", "with", "",
                      "```", fix.new, "```", ""]
    return "\n".join(lines)
