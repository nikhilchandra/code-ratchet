"""The module contains the audit command's own settings, read from the [audit] section of
config.toml.

A setting left out takes the default given here.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ratchet.project import Project, RoleSettings

COMMAND = "audit"


class AuditSettings(BaseModel):
    """Holds the audit command's limits and how each of its five roles runs."""

    model_config = ConfigDict(extra="forbid")

    # Path patterns, relative to the project, that are left out of the audit's copy of the code.
    hide: list[str] = []
    # What to do when the code's git history holds old .ratchet files: remove them from the
    # copy, or refuse to audit.
    history_with_ratchet: Literal["strip", "refuse"] = "strip"
    # The most pairs of findings that the pair judge is asked about.
    max_pairs: int = Field(default=40, gt=0)
    # The most one audit may spend in dollars, counting every agent's budget.
    max_cost_usd: float = Field(default=40.0, gt=0)
    auditor: RoleSettings = RoleSettings(model="opus", budget_usd=10.0, timeout_s=3600)
    list_reviewer: RoleSettings = RoleSettings(model="opus", budget_usd=6.0, timeout_s=2400)
    rechecker: RoleSettings = RoleSettings(model="opus", budget_usd=6.0, timeout_s=2400)
    pair_judge: RoleSettings = RoleSettings(model="sonnet", budget_usd=1.0, timeout_s=600)
    checker: RoleSettings = RoleSettings(model="opus", budget_usd=6.0, timeout_s=2400)


def load_settings(project: Project) -> AuditSettings:
    """Returns the audit command's settings for one project.

    Args:
        project: The project, as read from its .ratchet folder.

    Returns:
        The settings in the project's [audit] section, with defaults for any left out.

    Raises:
        ProjectError: The section holds a wrong setting.
    """
    return project.command_settings(COMMAND, AuditSettings)
