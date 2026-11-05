"""Checks how each refactor agent's instructions are built from the general prompts and the
project's own rules."""

from __future__ import annotations

from pathlib import Path

import pytest

from ratchet.project import Project, load_project
from ratchet.refactor.agent_answers import Plan
from ratchet.refactor.agent_profiles import (
    NO_COMMENT_RULES,
    STANDARD,
    Role,
    build_profile,
    write_profile,
)
from ratchet.refactor.settings import COMMAND
from rooster import Agent

PROBLEMS_FILE = ".ratchet/problems-20261026-09h00m.md"

CONFIG = """\
test_command = ["uv", "run", "pytest", "-q"]
editable = ["src/**", "tests/**"]
frozen = ["**/*.feature"]
"""


def make_project(root: Path, comments: str | None = "Use American spelling.") -> Project:
    """Writes a .ratchet folder into 'root' and returns the project read from it."""
    folder = root / ".ratchet"
    folder.mkdir()
    (folder / "config.toml").write_text(CONFIG, encoding="utf-8")
    (folder / "rules.md").write_text("1. Views never import models.\n", encoding="utf-8")
    if comments is not None:
        (folder / "comments.md").write_text(comments, encoding="utf-8")
    return load_project(root, commands=[COMMAND])


@pytest.mark.parametrize("role", list(Role))
def test_a_profile_holds_the_projects_rules_and_settings(tmp_path: Path, role: Role) -> None:
    """Proves every profile has the shared part with the project's rules, paths, test command
    and comment rules filled in, followed by the role's own part."""
    profile = build_profile(make_project(tmp_path), role, PROBLEMS_FILE)

    assert profile.startswith("# The refactor")
    assert f"# Your role: the {role}" in profile
    assert "1. Views never import models." in profile
    assert "Use American spelling." in profile
    assert "`src/**` or `tests/**`" in profile
    assert "`**/*.feature` or `.ratchet/**`" in profile
    assert "`uv run pytest -q`" in profile
    assert f"`{PROBLEMS_FILE}`" in profile
    assert "S21." in profile
    assert "$" not in profile


def test_the_test_command_keeps_its_quotes(tmp_path: Path) -> None:
    """Proves an empty argument, as in pytest -m "", is shown in quotes rather than dropped."""
    make_project(tmp_path)
    (tmp_path / ".ratchet" / "config.toml").write_text(
        CONFIG.replace('"-q"]', '"-m", "", "-k", "not slow"]'), encoding="utf-8")
    project = load_project(tmp_path, commands=[COMMAND])

    assert '`uv run pytest -m "" -k "not slow"`' in build_profile(project, Role.PLANNER, PROBLEMS_FILE)


def test_a_project_without_comment_rules_says_so(tmp_path: Path) -> None:
    """Proves the comments section still reads sensibly when the project has no comments.md."""
    profile = build_profile(make_project(tmp_path, comments=None), Role.IMPLEMENTER, PROBLEMS_FILE)

    assert NO_COMMENT_RULES in profile


def test_a_written_profile_is_accepted_by_an_agent(tmp_path: Path) -> None:
    """Proves the file that write_profile makes is a profile an Agent takes, with its answer
    shape, without running it."""
    project = make_project(tmp_path)

    path = write_profile(project, Role.PLANNER, PROBLEMS_FILE, tmp_path / "profiles")

    assert path == tmp_path / "profiles" / "planner.md"
    assert path.read_text(encoding="utf-8") == build_profile(project, Role.PLANNER, PROBLEMS_FILE)
    Agent("planner", path, cwd=tmp_path, log_dir=tmp_path / "logs", schema=Plan)


def test_the_standard_has_every_check_once() -> None:
    """Proves the general standard numbers its checks S1 to S21, each defined once."""
    standard = STANDARD.read_text(encoding="utf-8")

    for number in range(1, 22):
        assert standard.count(f"**S{number}.") == 1


def test_the_reviewers_criteria_name_their_checks(tmp_path: Path) -> None:
    """Proves the reviewer's criteria point at the standard's ids."""
    profile = build_profile(make_project(tmp_path), Role.REVIEWER, PROBLEMS_FILE)

    assert "**Necessary (S3, S4, S9, S18).**" in profile
    assert "**Modular (S1, S2, S6 and the stated rules).**" in profile
    assert "**Comments (S21).**" in profile
