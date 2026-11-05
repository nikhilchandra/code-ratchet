"""Checks the audit command's own settings, read from the [audit] section of config.toml."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ratchet.audit.settings import COMMAND, AuditSettings, load_settings
from ratchet.cli import COMMANDS
from ratchet.init_project import TEMPLATES
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
max_pairs = 10
max_cost_usd = 12.5

[audit.final_judge]
model = "sonnet"
effort = "medium"
budget_usd = 2.0
timeout_s = 300
""")

    assert settings.hide == ["vendor/**"]
    assert (settings.max_pairs, settings.max_cost_usd) == (10, 12.5)
    assert settings.final_judge.effort == Effort.MEDIUM
    assert settings.final_judge.budget_usd == 2.0


def test_the_two_reviewers_have_their_own_settings(tmp_path: Path) -> None:
    """Proves [audit.rules_reviewer] and [audit.standard_reviewer] are read into settings of their
    own, and that the old list checker, the duplicate judge and the final judge are read from
    sections named after them."""
    settings = settings_from(tmp_path, """
[audit.rules_reviewer]
model = "sonnet"
budget_usd = 2.0
timeout_s = 100

[audit.standard_reviewer]
model = "haiku"
budget_usd = 3.0
timeout_s = 200

[audit.old_list_checker]
model = "sonnet"
budget_usd = 4.0
timeout_s = 300

[audit.duplicate_judge]
model = "haiku"
budget_usd = 0.5
timeout_s = 400

[audit.final_judge]
model = "sonnet"
budget_usd = 5.0
timeout_s = 500
""")

    roles = {
        "rules_reviewer": ("sonnet", 2.0, 100),
        "standard_reviewer": ("haiku", 3.0, 200),
        "old_list_checker": ("sonnet", 4.0, 300),
        "duplicate_judge": ("haiku", 0.5, 400),
        "final_judge": ("sonnet", 5.0, 500),
    }
    for name, expected in roles.items():
        role = getattr(settings, name)
        assert (role.model, role.budget_usd, role.timeout_s) == expected, name


def test_unset_settings_take_their_defaults(tmp_path: Path) -> None:
    """Proves a setting left out takes its default."""
    settings = settings_from(tmp_path)

    assert settings.hide == []
    assert (settings.max_pairs, settings.max_cost_usd) == (40, 40.0)
    roles = {
        "auditor": ("opus", 10.0, 3600),
        "rules_reviewer": ("opus", 6.0, 2400),
        "standard_reviewer": ("opus", 6.0, 2400),
        "old_list_checker": ("opus", 6.0, 2400),
        "duplicate_judge": ("sonnet", 1.0, 600),
        "final_judge": ("opus", 6.0, 2400),
    }
    for name, expected in roles.items():
        role = getattr(settings, name)
        assert (role.model, role.budget_usd, role.timeout_s) == expected, name


def test_the_config_template_lists_each_role_with_its_default(tmp_path: Path) -> None:
    """Proves the [audit] settings that the config template shows commented out name every role
    by its name, and give the defaults when they are uncommented."""
    template = (TEMPLATES / "config.toml").read_text(encoding="utf-8")
    commented = template.split("# [audit]\n", 1)[1].splitlines()
    # keep the commented settings and drop the commented prose
    lines = ["[audit]"] + [line.removeprefix("# ") for line in commented
                           if re.match(r"# \w+ = ", line)]

    settings = settings_from(tmp_path, "\n".join(lines) + "\n")

    assert {line.split(" = ", 1)[0] for line in lines[1:]} == {
        "hide", "max_pairs", "max_cost_usd", "auditor", "rules_reviewer", "standard_reviewer",
        "old_list_checker", "duplicate_judge", "final_judge",
    }
    assert settings == AuditSettings()


@pytest.mark.parametrize(
    ("section", "named"),
    [
        ('[audit]\nhistory_with_ratchet = "strip"', "audit.history_with_ratchet"),
        ("[audit]\nmax_pairs = 0", "audit.max_pairs"),
        ('[audit.auditor]\nbudget_usd = "lots"', "audit.auditor.budget_usd"),
        ('[audit.duplicate_judge]\nmodle = "sonnet"', "audit.duplicate_judge.modle"),
        ('[audit.judge]\nmodel = "sonnet"', "audit.judge"),
        ('[audit.list_reviewer]\nmodel = "opus"\nbudget_usd = 6.0\ntimeout_s = 2400',
         "audit.list_reviewer"),
    ],
)
def test_a_wrong_setting_is_named(tmp_path: Path, section: str, named: str) -> None:
    """Proves a wrong value, a limit of zero, a misspelling, or a setting that no longer exists,
    such as history_with_ratchet or list_reviewer, names the setting that is wrong."""
    with pytest.raises(ProjectError, match=named.replace(".", r"\.")):
        settings_from(tmp_path, section + "\n")
