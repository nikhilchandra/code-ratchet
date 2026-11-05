"""Checks the shapes of the refactor agents' answers against saved example answers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from ratchet.refactor.agent_answers import (
    ImplementerAnswer,
    Plan,
    Review,
    Status,
    SuperPlan,
    SuperVerdict,
    Verdict,
    WriterAnswer,
)

ANSWERS = Path(__file__).parent / "answers"

SAVED = [
    ("plan.json", Plan),
    ("plan-stop.json", Plan),
    ("implementer-done.json", ImplementerAnswer),
    ("implementer-blocked.json", ImplementerAnswer),
    ("review-accept.json", Review),
    ("review-comment-fixes.json", Review),
    ("review-reject.json", Review),
    ("review-replan.json", Review),
    ("super-plan-approve.json", SuperPlan),
    ("super-plan-revise.json", SuperPlan),
    ("test-writer.json", WriterAnswer),
]


def saved(name: str) -> dict:
    """Returns one saved example answer."""
    return json.loads((ANSWERS / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(("name", "shape"), SAVED)
def test_a_saved_answer_fits_and_comes_back_unchanged(name: str, shape: type[BaseModel]) -> None:
    """Proves each kind of saved answer fits its shape, and that nothing is lost,
    including the "from" and "pass" fields, whose Python names differ."""
    answer = shape.model_validate(saved(name))

    assert answer.model_dump(mode="json", by_alias=True) == saved(name)


def test_the_answers_read_as_python_values() -> None:
    """Proves the answers' fields can be read by their Python names and enums."""
    plan = Plan.model_validate(saved("plan.json"))
    review = Review.model_validate(saved("review-comment-fixes.json"))
    blocked = ImplementerAnswer.model_validate(saved("implementer-blocked.json"))

    assert plan.stop is False and plan.checks
    assert review.verdict is Verdict.ACCEPT_AFTER_COMMENT_FIXES
    assert review.criteria.comments.passed is False and review.comment_fixes
    assert blocked.status is Status.BLOCKED and blocked.blocked_reason


def test_the_super_planners_answers_read_as_python_values() -> None:
    """Proves the plan names its functions, an approval lists its tests, and a revise its
    revisions."""
    plan = Plan.model_validate(saved("plan.json"))
    approve = SuperPlan.model_validate(saved("super-plan-approve.json"))
    revise = SuperPlan.model_validate(saved("super-plan-revise.json"))
    written = WriterAnswer.model_validate(saved("test-writer.json"))

    assert "invoices.pricing.line_total" in plan.functions
    assert approve.verdict is SuperVerdict.APPROVE and len(approve.tests_to_write) == 2
    assert approve.tests_to_write[0].file == "tests/test_export_totals.py"
    assert revise.verdict is SuperVerdict.REVISE and revise.revisions
    assert [test.file for test in written.tests] == [
        test.file for test in approve.tests_to_write]


def test_a_missing_field_is_refused() -> None:
    """Proves every field must be present, as the prompts tell the agents."""
    answer = saved("plan.json")
    del answer["checks"]

    with pytest.raises(ValidationError, match="checks"):
        Plan.model_validate(answer)


def test_an_unknown_field_is_refused() -> None:
    """Proves a field the shape does not name is refused, at the top and inside a criterion."""
    answer = saved("review-accept.json")
    answer["criteria"]["scope"]["severity"] = "high"

    with pytest.raises(ValidationError, match="severity"):
        Review.model_validate(answer)


def test_an_unknown_verdict_is_refused() -> None:
    """Proves the verdict must be one of the four the script acts on."""
    answer = saved("review-accept.json")
    answer["verdict"] = "maybe"

    with pytest.raises(ValidationError, match="verdict"):
        Review.model_validate(answer)


@pytest.mark.parametrize("shape", [Plan, ImplementerAnswer, Review, SuperPlan, WriterAnswer])
def test_the_schema_requires_every_field_and_uses_the_json_names(shape: type[BaseModel]) -> None:
    """Proves the schema sent to Claude requires every field, refuses others, and names the
    fields as the prompts do."""
    schema = shape.model_json_schema()
    shapes = [schema, *schema.get("$defs", {}).values()]

    for part in shapes:
        if part.get("type") == "object":
            assert set(part["required"]) == set(part["properties"]), part["title"]
            assert part["additionalProperties"] is False, part["title"]
    names = {name for part in shapes for name in part.get("properties", {})}
    assert "source" not in names and "passed" not in names
