"""Holds the shapes of the five refactor agents' answers, which each agent's reply must fit.

Every field is required, so an agent always answers every field, with an empty string or an
empty list where it has nothing to say.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Answer(BaseModel):
    """Refuses any field that the shape does not name."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------- the planner's answer


class Change(_Answer):
    """Holds one file that a step changes, and what changes in it."""

    file: str
    what: str


class Added(_Answer):
    """Holds one class, method or parameter that a step adds, and the requirement it serves."""

    kind: str
    name: str
    file: str
    requirement: str
    why_not_simpler: str


class Removed(_Answer):
    """Holds one class, method, parameter or module that a step removes."""

    kind: str
    name: str
    file: str


class Moved(_Answer):
    """Holds one thing that a step moves, and where it moves from and to."""

    name: str
    # The JSON field is "from", which is a Python keyword.
    source: str = Field(alias="from")
    to: str


class AffectedTest(_Answer):
    """Holds one test that a step's change to the code affects, and the change the test rules
    allow it."""

    file: str
    why: str
    allowed_change: str


class Plan(_Answer):
    """Holds the planner's answer, which is either the plan for the next step or a stop."""

    # True when nothing more can be planned, with the reason. A plan has False and "".
    stop: bool
    reason: str
    # A title in the style of a commit message, which becomes the step's commit message, and a
    # label of at most 60 characters, which the console shows. Then the problem ids the step
    # works on, and the problem ids it finishes.
    step_title: str
    summary: str
    problems: list[str]
    completes: list[str]
    # The functions the step changes, each as package.module.qualname, such as
    # "invoices.pricing.line_total". The script builds their pathway map for the super-planner.
    functions: list[str]
    why_now: str
    changes: list[Change]
    added: list[Added]
    removed: list[Removed]
    moved: list[Moved]
    tests_affected: list[AffectedTest]
    # Statements about the code after the step, which the reviewer confirms one by one.
    checks: list[str]
    risks: str


# ---------------------------------------------------------------- the super-planner's answer


class SuperVerdict(StrEnum):
    """Says whether the plan goes ahead or goes back to the planner."""

    APPROVE = "approve"
    REVISE = "revise"


class WantedTest(_Answer):
    """Holds one test that the super-planner wants written before the step runs."""

    # The new test file, such as "tests/test_export_totals.py".
    file: str
    name: str
    # The path through the code that the test pins, and the fact it checks on that path.
    covers: str
    checks: str


class SuperPlan(_Answer):
    """Holds the super-planner's verdict on a plan, the paths the plan touches, and the tests to
    write."""

    verdict: SuperVerdict
    summary: str
    # Every path through the plan's functions, as entry point -> ... -> function, and those of
    # them that no test exercises.
    paths: list[str]
    untested_paths: list[str]
    tests_to_write: list[WantedTest]
    # What the planner must change in the plan when the verdict is revise, otherwise empty.
    revisions: list[str]


# ---------------------------------------------------------------- the test writer's answer


class WrittenTest(_Answer):
    """Holds one test that the test writer wrote, and the path it covers."""

    file: str
    name: str
    covers: str


class WriterAnswer(_Answer):
    """Holds the test writer's answer after it has written the tests asked for."""

    summary: str
    tests: list[WrittenTest]


# ---------------------------------------------------------------- the implementer's answer


class Status(StrEnum):
    """Says whether the implementer finished the step or could not carry out the plan."""

    DONE = "done"
    BLOCKED = "blocked"


class ChangedTest(_Answer):
    """Holds one test that the implementer changed, and the code change that required it."""

    file: str
    change: str
    why: str


class ImplementerAnswer(_Answer):
    """Holds the implementer's answer after it has made, or failed to make, one step."""

    status: Status
    summary: str
    deviations: list[str]
    tests_changed: list[ChangedTest]
    # What stops the plan when the status is blocked, and "" otherwise.
    blocked_reason: str


# ---------------------------------------------------------------- the reviewer's answer


class Verdict(StrEnum):
    """Says what happens to a step after its review."""

    ACCEPT = "accept"
    ACCEPT_AFTER_COMMENT_FIXES = "accept_after_comment_fixes"
    REJECT = "reject"
    REPLAN = "replan"


class Finding(_Answer):
    """Holds one fault that the reviewer found, where it is, and what would fix it."""

    file: str
    line: int
    problem: str
    fix: str


class Criterion(_Answer):
    """Holds the reviewer's judgment on one criterion of the rubric."""

    # The JSON field is "pass", which is a Python keyword.
    passed: bool = Field(alias="pass")
    findings: list[Finding]


class Criteria(_Answer):
    """Holds the reviewer's judgment on each of the nine criteria of the rubric."""

    scope: Criterion
    readable: Criterion
    editable: Criterion
    necessary: Criterion
    placed: Criterion
    modular: Criterion
    behavior: Criterion
    tests: Criterion
    comments: Criterion


class PlanCheck(_Answer):
    """Holds the reviewer's answer to one of the plan's checks, counted from 1."""

    check: int
    passed: bool = Field(alias="pass")
    evidence: str


class CommentFix(_Answer):
    """Holds one comment fix, which replaces the text 'old' in a file with the text 'new'."""

    file: str
    old: str
    new: str


class Review(_Answer):
    """Holds the reviewer's verdict on one step, with its findings and fixes."""

    verdict: Verdict
    summary: str
    criteria: Criteria
    plan_checks: list[PlanCheck]
    comment_fixes: list[CommentFix]
    # Matters of taste, which never change the verdict.
    notes: list[str]
