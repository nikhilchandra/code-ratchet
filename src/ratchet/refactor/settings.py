"""Holds the refactor command's own settings, read from the [refactor] section of config.toml,
and reads the problem list it works from.

A setting left out takes the default given here.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ratchet.project import Project, RoleSettings, read_markdown

COMMAND = "refactor"
# The file in .ratchet that lists the problems the planner works through.
PROBLEMS = "problems.md"


class Caps(BaseModel):
    """Holds how many times a job may fail before ratchet gives up on it."""

    model_config = ConfigDict(extra="forbid")

    # The rejections in a row, and the test failures, after which a job is undone.
    rejections: int = Field(default=3, gt=0)
    test_failures: int = Field(default=3, gt=0)
    # The jobs on a problem that come back blocked or replanned, after which the problem is
    # set aside.
    sendbacks: int = Field(default=2, gt=0)


class RefactorSettings(BaseModel):
    """Holds the refactor command's caps and how each of its three roles runs."""

    model_config = ConfigDict(extra="forbid")

    caps: Caps = Caps()
    planner: RoleSettings = RoleSettings(model="opus", budget_usd=3.0, timeout_s=1800)
    implementer: RoleSettings = RoleSettings(model="sonnet", budget_usd=8.0, timeout_s=3600)
    reviewer: RoleSettings = RoleSettings(model="opus", budget_usd=3.0, timeout_s=1800)


def load_settings(project: Project) -> RefactorSettings:
    """Returns the refactor command's settings for one project.

    Args:
        project: The project, as read from its .ratchet folder.

    Returns:
        The settings in the project's [refactor] section, with defaults for any left out.

    Raises:
        ProjectError: The section holds a wrong setting.
    """
    return project.command_settings(COMMAND, RefactorSettings)


def read_problems(project: Project) -> str:
    """Returns the problem list that the planner works through.

    Args:
        project: The project, as read from its .ratchet folder.

    Returns:
        The text of problems.md.

    Raises:
        ProjectError: problems.md is missing or holds nothing but blank space.
    """
    return read_markdown(project.folder, PROBLEMS)
