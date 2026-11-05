"""Checks the shapes of the audit agents' answers against saved example answers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from ratchet.audit.agent_answers import (
    AuditorAnswer,
    FinalJudgeAnswer,
    ListReview,
    Outcome,
    DuplicateJudgeAnswer,
    OldListCheckerAnswer,
)

ANSWERS = Path(__file__).parent / "answers"

SAVED = [
    ("audit-auditor.json", AuditorAnswer),
    ("audit-list-review.json", ListReview),
    ("audit-old-list-checker.json", OldListCheckerAnswer),
    ("audit-duplicate-judge.json", DuplicateJudgeAnswer),
    ("audit-final-judge.json", FinalJudgeAnswer),
]


def saved(name: str) -> dict:
    """Returns one saved example answer."""
    return json.loads((ANSWERS / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize(("name", "shape"), SAVED)
def test_a_saved_answer_fits_and_comes_back_unchanged(name: str, shape: type[BaseModel]) -> None:
    """Proves each saved answer fits its shape and that nothing is lost."""
    answer = shape.model_validate(saved(name))

    assert answer.model_dump(mode="json", by_alias=True) == saved(name)


def test_the_saved_final_judge_answer_holds_every_outcome() -> None:
    """Proves the saved final judge answer has a group check for each of the six outcomes."""
    answer = FinalJudgeAnswer.model_validate(saved("audit-final-judge.json"))

    assert {check.outcome for check in answer.checks} == set(Outcome)


def test_a_missing_field_is_refused() -> None:
    """Proves every field must be present."""
    answer = saved("audit-auditor.json")
    del answer["problems"][0]["breaks"]

    with pytest.raises(ValidationError, match="breaks"):
        AuditorAnswer.model_validate(answer)


def test_an_unknown_field_is_refused() -> None:
    """Proves a field the shape does not name is refused inside a nested finding."""
    answer = saved("audit-old-list-checker.json")
    answer["checks"][0]["finding"]["severity"] = "high"

    with pytest.raises(ValidationError, match="severity"):
        OldListCheckerAnswer.model_validate(answer)


def test_a_citation_line_below_one_is_refused() -> None:
    """Proves a citation must start at line 1 or later."""
    answer = saved("audit-auditor.json")
    answer["problems"][0]["evidence"][0]["line"] = 0

    with pytest.raises(ValidationError, match="line"):
        AuditorAnswer.model_validate(answer)


def test_an_unknown_grade_is_refused() -> None:
    """Proves the grade must be stated, inferred or standard."""
    answer = saved("audit-auditor.json")
    answer["problems"][0]["grade"] = "guessed"

    with pytest.raises(ValidationError, match="grade"):
        AuditorAnswer.model_validate(answer)


@pytest.mark.parametrize(
    "shape", [AuditorAnswer, ListReview, OldListCheckerAnswer, DuplicateJudgeAnswer, FinalJudgeAnswer]
)
def test_the_schema_requires_every_field(shape: type[BaseModel]) -> None:
    """Proves the schema sent to Claude requires every field and refuses others."""
    schema = shape.model_json_schema()
    shapes = [schema, *schema.get("$defs", {}).values()]

    for part in shapes:
        if part.get("type") == "object":
            assert set(part["required"]) == set(part["properties"]), part["title"]
            assert part["additionalProperties"] is False, part["title"]
