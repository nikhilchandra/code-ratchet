"""Holds the shapes of the audit agents' answers, which each agent's reply must fit.

Every field is required, so an agent always answers every field, with an empty string or an
empty list where it has nothing to say.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Answer(BaseModel):
    """Refuses any field that the shape does not name."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------- findings


class Grade(StrEnum):
    """Says what a finding rests on: a rule the owner stated, a rule inferred from the code, or
    the general standard."""

    STATED = "stated"
    INFERRED = "inferred"
    STANDARD = "standard"


class Citation(_Answer):
    """Holds one place in the code, the exact text found there, and what it shows."""

    file: str
    line: int = Field(ge=1)
    end_line: int
    quote: str
    shows: str


class TestReach(_Answer):
    """Holds one test file that reaches the cited code, and how it does."""

    # Keeps pytest from collecting this class as a test.
    __test__ = False

    file: str
    how: str


class Finding(_Answer):
    """Holds one problem in the code, with the rule it breaks and the evidence for it."""

    # The module, class or function that the problem is about.
    subject: str
    title: str
    what_is_wrong: str
    grade: Grade
    # The rule quoted from rules.md, or a standard id such as S7.
    breaks: str
    # Citations showing that most of the code follows the inferred rule. Empty unless the grade
    # is inferred.
    inferred_from: list[Citation]
    history: str
    evidence: list[Citation]
    simpler_option: str
    done_when: list[str]
    # The ids or titles of problems that must be fixed first.
    waits_on: list[str]
    tests: list[TestReach]


class QuestionKind(StrEnum):
    """Says whether a question is about the design or about the rules."""

    DESIGN = "design"
    RULES = "rules"


class Question(_Answer):
    """Holds one thing that the agent cannot settle and asks the owner."""

    kind: QuestionKind
    title: str
    evidence: list[Citation]
    why_it_may_be_deliberate: str
    what_would_settle: str


class Bug(_Answer):
    """Holds one wrong behavior that the agent noticed, which is not a design problem."""

    title: str
    evidence: list[Citation]
    what_happens: str


# ---------------------------------------------------------------- the auditor's answer


class UnitState(StrEnum):
    """Says how the auditor judged one module or class."""

    FINE = "fine"
    PROBLEM = "problem"
    QUESTION = "question"


class UnitVerdict(_Answer):
    """Holds the auditor's verdict on one module or class of the inventory."""

    name: str
    verdict: UnitState
    note: str


class AuditorAnswer(_Answer):
    """Holds the auditor's answer, which is also the answer of the blind audit."""

    # How far the code has drifted from the target design, in plain words.
    drift: str
    units: list[UnitVerdict]
    problems: list[Finding]
    questions: list[Question]
    bugs: list[Bug]


# ---------------------------------------------------------------- the list reviewers' answer


class ReviewVerdict(StrEnum):
    """Says whether a reviewer agrees with an entry, corrects it, or disputes it."""

    AGREE = "agree"
    CORRECT = "correct"
    DISPUTE = "dispute"


class EntryReview(_Answer):
    """Holds a reviewer's verdict on one entry of the auditor's list."""

    label: str
    verdict: ReviewVerdict
    reason: str
    # The full corrected entry when the verdict is correct, and the entry as it stands otherwise.
    finding: Finding


class ListReview(_Answer):
    """Holds a reviewer's answer on the whole list, with anything the reviewer adds."""

    reviews: list[EntryReview]
    added: list[Finding]
    questions: list[Question]
    bugs: list[Bug]


# ---------------------------------------------------------------- the rechecker's answer


class OldVerdict(StrEnum):
    """Says what has become of an entry of the old list."""

    DONE = "done"
    REOPEN = "reopen"
    OPEN = "open"
    CORRECTED = "corrected"
    GONE = "gone"
    BLOCKED = "blocked"


class OldEntryCheck(_Answer):
    """Holds the rechecker's verdict on one old problem."""

    id: str
    verdict: OldVerdict
    what_is_left: str
    # The entry as it reads now, with its current subject and current citations. It is required
    # for every id, the finished ones included.
    finding: Finding


class QuestionDecision(StrEnum):
    """Says what an answered question becomes."""

    PROBLEM = "problem"
    DROPPED = "dropped"
    PARKED = "parked"
    RULE = "rule"


class AnsweredQuestion(_Answer):
    """Holds what the rechecker decides for one question that the owner has answered."""

    id: str
    decision: QuestionDecision
    reason: str
    finding: Finding
    # The text to add to rules.md when the decision is rule, and "" otherwise.
    rule_text: str


class RecheckAnswer(_Answer):
    """Holds the rechecker's answer on an old list."""

    checks: list[OldEntryCheck]
    answered_questions: list[AnsweredQuestion]
    from_reviewer_notes: list[Finding]


# ---------------------------------------------------------------- the pair judge's answer


class PairVerdict(_Answer):
    """Holds the judge's decision on one numbered pair of findings."""

    pair: int
    same: bool
    reason: str


class PairAnswer(_Answer):
    """Holds the pair judge's decisions."""

    pairs: list[PairVerdict]


# ---------------------------------------------------------------- the checker's answer


class Outcome(StrEnum):
    """Says how the checker settles one group."""

    KEEP = "keep"
    MERGE_INTO = "merge_into"
    SPLIT = "split"
    DROP = "drop"
    QUESTION = "question"
    BUG = "bug"


class CheckedEntry(_Answer):
    """Holds one entry that a group comes out as."""

    # True for the one entry that continues the group's old problem.
    continues_old: bool
    finding: Finding


class GroupCheck(_Answer):
    """Holds the checker's decision on one group."""

    key: str
    outcome: Outcome
    # The key of a related group when the outcome is merge_into, and "" otherwise.
    merge_into: str
    reason: str
    entries: list[CheckedEntry]


class QuestionVerdict(_Answer):
    """Holds what the checker decides for one question that the owner has answered."""

    label: str
    decision: QuestionDecision
    reason: str
    finding: Finding
    rule_text: str


class CheckAnswer(_Answer):
    """Holds the checker's answer on every group."""

    checks: list[GroupCheck]
    decisions: list[QuestionVerdict]
