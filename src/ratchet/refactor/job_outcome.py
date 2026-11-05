"""The module contains the enum that names how a refactor job ended."""

from __future__ import annotations

from enum import StrEnum


class JobOutcome(StrEnum):
    """Names how a refactor job ended."""

    # The planner found nothing left to plan.
    STOPPED = "stopped"
    COMMITTED = "committed"
    # The implementer could not carry out the plan, or the reviewer found the fault in the plan.
    BLOCKED = "blocked"
    REPLAN = "replan"
    # The step reached a cap on rejections or test failures.
    ABANDONED = "abandoned"
