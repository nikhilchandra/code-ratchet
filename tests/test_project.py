"""Checks how ratchet reads a target project's .ratchet folder, and how it reports a broken one."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict

from ratchet.project import Project, ProjectError, load_project

CONFIG = """\
test_command = ["uv", "run", "pytest", "-q"]
editable = ["src/**", "tests/**"]
frozen = ["**/*.feature"]

[demo]
limit = 4
"""


class DemoSettings(BaseModel):
    """Holds the settings of a pretend command called demo."""

    model_config = ConfigDict(extra="forbid")

    limit: int = 1
    label: str = "none"


def make_folder(root: Path, config: str = CONFIG, rules: str = "Views never import models.") -> Path:
    """Writes a .ratchet folder into 'root' and returns 'root'."""
    folder = root / ".ratchet"
    folder.mkdir()
    (folder / "config.toml").write_text(config, encoding="utf-8")
    (folder / "rules.md").write_text(rules, encoding="utf-8")
    return root


def load(root: Path) -> Project:
    """Reads 'root' as a project whose only command is demo."""
    return load_project(root, commands=["demo"])


def test_a_complete_folder_loads(tmp_path: Path) -> None:
    """Proves the shared settings and the rules are read from the folder."""
    project = load(make_folder(tmp_path))

    assert isinstance(project, Project)
    assert project.root == tmp_path
    assert project.config.test_command == ["uv", "run", "pytest", "-q"]
    assert project.config.editable == ["src/**", "tests/**"]
    assert project.config.test_timeout_s == 900
    assert project.rules == "Views never import models."


def test_a_commands_section_is_checked_against_its_own_settings(tmp_path: Path) -> None:
    """Proves a command gets its own section, with defaults for what the section leaves out."""
    settings = load(make_folder(tmp_path)).command_settings("demo", DemoSettings)

    assert (settings.limit, settings.label) == (4, "none")


def test_a_missing_section_takes_the_defaults(tmp_path: Path) -> None:
    """Proves a command with no section in config.toml still runs, with its defaults."""
    config = 'test_command = ["pytest"]\neditable = ["src/**"]\n'
    settings = load(make_folder(tmp_path, config=config)).command_settings("demo", DemoSettings)

    assert settings == DemoSettings()


def test_the_ratchet_folder_is_always_frozen(tmp_path: Path) -> None:
    """Proves agents may never change .ratchet, whatever config.toml lists as frozen."""
    project = load(make_folder(tmp_path))

    assert project.frozen == ["**/*.feature", ".ratchet/**"]


@pytest.mark.parametrize(
    ("path", "allowed"),
    [
        ("src/app.py", True),
        ("src/deep/down/app.py", True),
        ("tests\\test_app.py", True),
        ("tests/features/login.feature", False),
        ("login.feature", False),
        (".ratchet/rules.md", False),
        ("pyproject.toml", False),
        ("src", False),
    ],
)
def test_may_change_follows_the_editable_and_frozen_patterns(
    tmp_path: Path, path: str, allowed: bool
) -> None:
    """Proves a file may change only when it matches an editable pattern and no frozen one,
    where ** stands for any number of folders, none included."""
    assert load(make_folder(tmp_path)).may_change(path) is allowed


def test_the_state_folder_sits_inside_the_ratchet_folder(tmp_path: Path) -> None:
    """Proves the ledger and the jobs are kept in .ratchet/state."""
    assert load(make_folder(tmp_path)).state_dir == tmp_path / ".ratchet" / "state"


def test_a_project_without_the_folder_is_refused(tmp_path: Path) -> None:
    """Proves a missing .ratchet folder names the folder and the files it needs."""
    with pytest.raises(ProjectError, match=r"no \.ratchet folder.*config\.toml and rules\.md"):
        load(tmp_path)


@pytest.mark.parametrize("name", ["config.toml", "rules.md"])
def test_each_missing_file_is_named(tmp_path: Path, name: str) -> None:
    """Proves a folder missing one of its two files says which one."""
    make_folder(tmp_path)
    (tmp_path / ".ratchet" / name).unlink()

    with pytest.raises(ProjectError, match=f"no {name}"):
        load(tmp_path)


def test_empty_rules_are_refused(tmp_path: Path) -> None:
    """Proves an empty rules.md is caught, since the agents would have nothing to work from."""
    with pytest.raises(ProjectError, match="rules.md is empty"):
        load(make_folder(tmp_path, rules="  \n"))


def test_comment_rules_are_read_when_the_project_has_them(tmp_path: Path) -> None:
    """Proves comments.md is optional, and read when it is there."""
    make_folder(tmp_path)
    assert load(tmp_path).comments is None

    (tmp_path / ".ratchet" / "comments.md").write_text("Use American spelling.", encoding="utf-8")
    assert load(tmp_path).comments == "Use American spelling."


def test_empty_comment_rules_are_refused(tmp_path: Path) -> None:
    """Proves an empty comments.md is caught, since it was most likely meant to hold rules."""
    make_folder(tmp_path)
    (tmp_path / ".ratchet" / "comments.md").write_text("\n", encoding="utf-8")

    with pytest.raises(ProjectError, match="comments.md is empty"):
        load(tmp_path)


def test_broken_toml_names_the_file(tmp_path: Path) -> None:
    """Proves a syntax error in config.toml is reported as such."""
    make_folder(tmp_path, config='test_command = ["pytest"\n')

    with pytest.raises(ProjectError, match="config.toml is not valid TOML"):
        load(tmp_path)


def test_a_section_no_command_reads_is_named(tmp_path: Path) -> None:
    """Proves a misspelled section is caught instead of being ignored."""
    make_folder(tmp_path, config=CONFIG + "\n[dmeo]\nlimit = 2\n")

    with pytest.raises(ProjectError, match=r"no command reads: dmeo\. ratchet's commands are demo"):
        load(tmp_path)


@pytest.mark.parametrize(
    ("change", "named"),
    [
        ('edtiable = ["src/**"]', "edtiable"),
        ('frozen = "src/**"', "frozen"),
        ("test_timeout_s = 0", "test_timeout_s"),
    ],
)
def test_a_wrong_shared_setting_is_named(tmp_path: Path, change: str, named: str) -> None:
    """Proves a misspelled shared setting or a wrong value names the setting that is wrong."""
    make_folder(tmp_path, config='test_command = ["pytest"]\neditable = ["src/**"]\n' + change + "\n")

    with pytest.raises(ProjectError, match=named):
        load(tmp_path)


@pytest.mark.parametrize(
    ("change", "named"),
    [('limit = "lots"', "demo.limit"), ('lable = "x"', "demo.lable")],
)
def test_a_wrong_command_setting_is_named_with_its_command(
    tmp_path: Path, change: str, named: str
) -> None:
    """Proves a wrong setting in a command's section names the command in front of it."""
    config = 'test_command = ["pytest"]\neditable = ["src/**"]\n[demo]\n' + change + "\n"
    project = load(make_folder(tmp_path, config=config))

    with pytest.raises(ProjectError, match=named.replace(".", r"\.")):
        project.command_settings("demo", DemoSettings)


@pytest.mark.parametrize("missing", ["test_command", "editable"])
def test_a_required_setting_is_named_when_missing(tmp_path: Path, missing: str) -> None:
    """Proves the two settings with no sensible default must be given."""
    lines = {"test_command": 'test_command = ["pytest"]', "editable": 'editable = ["src/**"]'}
    del lines[missing]
    make_folder(tmp_path, config="\n".join(lines.values()) + "\n")

    with pytest.raises(ProjectError, match=missing):
        load(tmp_path)
