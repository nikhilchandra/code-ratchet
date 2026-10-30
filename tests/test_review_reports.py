"""Checks the records and reports made from a review, using saved example answers."""

from __future__ import annotations

import json
from pathlib import Path

from ratchet.refactor.agent_answers import Plan, Review
from ratchet.refactor.review_reports import (
    find_unmet_checks,
    format_checks_record,
    format_review_report,
)

ANSWERS = Path(__file__).parent / "answers"


def saved(name: str) -> dict:
    """Returns one saved example answer."""
    return json.loads((ANSWERS / name).read_text(encoding="utf-8"))


def plan_with_checks(count: int) -> Plan:
    """Returns the saved plan with its checks replaced by 'count' numbered ones."""
    return Plan.model_validate(saved("plan.json") | {"checks": [f"check {n}" for n in range(1, count + 1)]})


def review_answering(*answers: tuple[int, bool]) -> Review:
    """Returns the saved accepting review with its check answers replaced."""
    plan_checks = [{"check": number, "pass": passed, "evidence": f"evidence {number}"}
                   for number, passed in answers]
    return Review.model_validate(saved("review-accept.json") | {"plan_checks": plan_checks})


def test_a_failed_or_unanswered_check_is_unmet() -> None:
    """Proves a check counts as met only when the reviewer answered it with a pass."""
    unmet = find_unmet_checks(plan_with_checks(3), review_answering((1, True), (2, False)))

    assert unmet == [2, 3]


def test_the_record_lists_every_check_with_its_evidence() -> None:
    """Proves the record shows each check, met or not, with the evidence or its absence."""
    record = format_checks_record(plan_with_checks(2), review_answering((1, True)))

    assert "1. met: check 1\n   evidence 1" in record
    assert "2. not met: check 2\n   the reviewer gave no answer" in record


def test_the_report_holds_the_findings_and_the_fixes() -> None:
    """Proves a real rejecting review gives a report with its failing criteria, and a real
    comment-fix review spells out each fix."""
    reject = Review.model_validate(saved("review-reject.json"))
    fixes = Review.model_validate(saved("review-comment-fixes.json"))
    plan = Plan.model_validate(saved("plan.json"))

    report = format_review_report(plan, reject)
    failing = [name for name, criterion in reject.criteria if not criterion.passed]

    assert report.startswith("# The step was rejected\n\n" + reject.summary)
    assert all(f"## {name}" in report for name in failing)
    assert "## The comment fixes" in format_review_report(plan, fixes)
