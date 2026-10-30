"""Checks the refactor command's own settings, read from the [refactor] section of config.toml."""

from __future__ import annotations

from pathlib import Path

import pytest

from ratchet.project import ProjectError, load_project
from ratchet.refactor.settings import COMMAND, load_settings, read_problems
from rooster import Effort

SHARED = 'test_command = ["pytest"]\neditable = ["src/**"]\n'


def settings_from(root: Path, section: str = ""):
    """Writes a .ratchet folder whose config.toml ends with 'section', and returns the refactor
    settings read from it."""
    folder = root / ".ratchet"
    folder.mkdir()
    (folder / "config.toml").write_text(SHARED + section, encoding="utf-8")
    (folder / "rules.md").write_text("Views never import models.", encoding="utf-8")
    return load_settings(load_project(root, commands=[COMMAND]))


def test_the_section_is_read(tmp_path: Path) -> None:
    """Proves the caps and a role's settings come from [refactor]."""
    settings = settings_from(tmp_path, """
[refactor.caps]
rejections = 4

[refactor.implementer]
model = "sonnet"
effort = "medium"
budget_usd = 5.0
timeout_s = 1200
""")

    assert settings.caps.rejections == 4
    assert settings.implementer.effort == Effort.MEDIUM
    assert settings.implementer.budget_usd == 5.0


def test_unset_settings_take_their_defaults(tmp_path: Path) -> None:
    """Proves a setting left out takes its default."""
    settings = settings_from(tmp_path)

    assert (settings.caps.rejections, settings.caps.test_failures, settings.caps.sendbacks) == (
        3, 3, 2)
    assert (settings.planner.model, settings.planner.effort, settings.planner.budget_usd) == (
        "opus", Effort.HIGH, 3.0)
    assert (settings.implementer.model, settings.implementer.budget_usd) == ("sonnet", 8.0)
    assert (settings.reviewer.model, settings.reviewer.timeout_s) == ("opus", 1800)


@pytest.mark.parametrize(
    ("section", "named"),
    [
        ('[refactor.planner]\nbudget_usd = "lots"', "refactor.planner.budget_usd"),
        ('[refactor.reviewer]\neffort = "extreme"', "refactor.reviewer.effort"),
        ("[refactor.caps]\nrejections = 0", "refactor.caps.rejections"),
        ('[refactor.planner]\nmodle = "opus"', "refactor.planner.modle"),
        ('[refactor.plannr]\nmodel = "opus"', "refactor.plannr"),
    ],
)
def test_a_wrong_setting_is_named(tmp_path: Path, section: str, named: str) -> None:
    """Proves a wrong type, an unknown effort, a cap of zero or a misspelling names the setting
    that is wrong."""
    with pytest.raises(ProjectError, match=named.replace(".", r"\.")):
        settings_from(tmp_path, section + "\n")


def test_the_problem_list_is_read(tmp_path: Path) -> None:
    """Proves problems.md is read only when asked for, so a project may load without it."""
    settings_from(tmp_path)
    (tmp_path / ".ratchet" / "problems.md").write_text("## P1: too big", encoding="utf-8")

    assert read_problems(load_project(tmp_path, commands=[COMMAND])) == "## P1: too big"


def test_a_missing_or_empty_problem_list_is_named(tmp_path: Path) -> None:
    """Proves a problems.md that is missing or blank is reported by name."""
    settings_from(tmp_path)
    project = load_project(tmp_path, commands=[COMMAND])

    with pytest.raises(ProjectError, match="no problems.md"):
        read_problems(project)
    (tmp_path / ".ratchet" / "problems.md").write_text("\n", encoding="utf-8")
    with pytest.raises(ProjectError, match="problems.md is empty"):
        read_problems(project)
