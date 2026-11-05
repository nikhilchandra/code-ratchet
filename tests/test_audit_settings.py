"""Checks the audit command's own settings, read from the [audit] section of config.toml."""

from __future__ import annotations

from pathlib import Path

import pytest

from ratchet.audit.settings import COMMAND, load_settings
from ratchet.cli import COMMANDS
from ratchet.project import ProjectError, load_project
from rooster import Effort

SHARED = 'test_command = ["pytest"]\neditable = ["src/**"]\n'


def settings_from(root: Path, section: str = ""):
    """Writes a .ratchet folder whose config.toml ends with 'section', and returns the audit
    settings read from it."""
    folder = root / ".ratchet"
    folder.mkdir()
    (folder / "config.toml").write_text(SHARED + section, encoding="utf-8")
    (folder / "rules.md").write_text("Views never import models.", encoding="utf-8")
    return load_settings(load_project(root, commands=COMMANDS))


def test_audit_is_a_command() -> None:
    """Proves config.toml may hold an [audit] section."""
    assert COMMAND in COMMANDS


def test_the_section_is_read(tmp_path: Path) -> None:
    """Proves the limits and a role's settings come from [audit]."""
    settings = settings_from(tmp_path, """
[audit]
hide = ["vendor/**"]
history_with_ratchet = "refuse"
max_pairs = 10
max_cost_usd = 12.5

[audit.checker]
model = "sonnet"
effort = "medium"
budget_usd = 2.0
timeout_s = 300
""")

    assert settings.hide == ["vendor/**"]
    assert settings.history_with_ratchet == "refuse"
    assert (settings.max_pairs, settings.max_cost_usd) == (10, 12.5)
    assert settings.checker.effort == Effort.MEDIUM
    assert settings.checker.budget_usd == 2.0


def test_unset_settings_take_their_defaults(tmp_path: Path) -> None:
    """Proves a setting left out takes its default."""
    settings = settings_from(tmp_path)

    assert settings.hide == []
    assert settings.history_with_ratchet == "strip"
    assert (settings.max_pairs, settings.max_cost_usd) == (40, 40.0)
    roles = {
        "auditor": ("opus", 10.0, 3600),
        "list_reviewer": ("opus", 6.0, 2400),
        "rechecker": ("opus", 6.0, 2400),
        "pair_judge": ("sonnet", 1.0, 600),
        "checker": ("opus", 6.0, 2400),
    }
    for name, expected in roles.items():
        role = getattr(settings, name)
        assert (role.model, role.budget_usd, role.timeout_s) == expected


@pytest.mark.parametrize(
    ("section", "named"),
    [
        ('[audit]\nhistory_with_ratchet = "ignore"', "audit.history_with_ratchet"),
        ("[audit]\nmax_pairs = 0", "audit.max_pairs"),
        ('[audit.auditor]\nbudget_usd = "lots"', "audit.auditor.budget_usd"),
        ('[audit.pair_judge]\nmodle = "sonnet"', "audit.pair_judge.modle"),
        ('[audit.judge]\nmodel = "sonnet"', "audit.judge"),
    ],
)
def test_a_wrong_setting_is_named(tmp_path: Path, section: str, named: str) -> None:
    """Proves a wrong value, a limit of zero or a misspelling names the setting that is wrong."""
    with pytest.raises(ProjectError, match=named.replace(".", r"\.")):
        settings_from(tmp_path, section + "\n")
