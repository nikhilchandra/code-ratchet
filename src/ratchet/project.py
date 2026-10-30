"""Reads a target project's .ratchet folder, which holds what ratchet needs to know about that
project.

The folder always holds two files:
    config.toml: the test command and the paths agents may change, which every command shares,
        followed by one section per command, such as [refactor].
    rules.md: what the project's code must become, which every agent reads.

It may also hold comments.md, the project's rules for comments and docstrings. Without it,
agents follow no comment style beyond keeping comments true.

Each command reads its own markdown files from the folder, such as problems.md for refactor,
and checks its own section of config.toml. ratchet keeps its own records in .ratchet/state,
which the project should not commit.
"""

from __future__ import annotations

import tomllib
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from rooster import Effort

FOLDER = ".ratchet"

SettingsT = TypeVar("SettingsT", bound=BaseModel)


class ProjectError(RuntimeError):
    """Signals a .ratchet folder that is missing, incomplete or wrongly written."""


class RoleSettings(BaseModel):
    """Holds how one role's agent runs, for any command that has roles."""

    model_config = ConfigDict(extra="forbid")

    model: str
    effort: Effort = Effort.HIGH
    # The most one call may spend, in dollars, and the longest it may take, in seconds.
    budget_usd: float = Field(gt=0)
    timeout_s: int = Field(gt=0)


class Config(BaseModel):
    """Holds the settings at the top of config.toml, which every command shares."""

    model_config = ConfigDict(extra="forbid")

    # The command that runs the whole test suite, and the longest it may take, in seconds.
    test_command: list[str] = Field(min_length=1)
    test_timeout_s: int = Field(default=900, gt=0)
    # Path patterns, relative to the project, that agents may change, and patterns they may
    # never change, even inside 'editable'.
    editable: list[str] = Field(min_length=1)
    frozen: list[str] = []


@dataclass(frozen=True)
class Project:
    """Holds a target project's shared settings and rules, as read from its .ratchet folder,
    and checks each command's own settings on request."""

    # The project's own folder, which holds .ratchet.
    root: Path
    config: Config
    # The text of rules.md, and of comments.md or None when the project has no comments.md.
    rules: str
    comments: str | None = None
    # Each command's section of config.toml, unchecked until the command asks for it.
    sections: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    @property
    def folder(self) -> Path:
        """Returns the project's .ratchet folder."""
        return self.root / FOLDER

    @property
    def state_dir(self) -> Path:
        """Returns the folder where ratchet keeps its ledger and its jobs."""
        return self.folder / "state"

    @property
    def frozen(self) -> list[str]:
        """Returns the patterns that agents may never change, which always include .ratchet."""
        return [*self.config.frozen, f"{FOLDER}/**"]

    def may_change(self, path: str) -> bool:
        """Answers whether agents may change a file, which must match an editable pattern and no
        frozen one.

        Args:
            path: The file's path relative to the project, with forward or back slashes.

        Returns:
            True when the file matches 'editable' and does not match 'frozen'.
        """
        file = PurePosixPath(path.replace("\\", "/"))
        return any(file.full_match(pattern) for pattern in self.config.editable) and not any(
            file.full_match(pattern) for pattern in self.frozen
        )

    def command_settings(self, command: str, model: type[SettingsT]) -> SettingsT:
        """Returns one command's section of config.toml, checked against that command's model.
        A section left out of config.toml takes the model's defaults.

        Args:
            command: The command's name, such as "refactor".
            model: The command's settings class.

        Returns:
            The command's settings.

        Raises:
            ProjectError: The section holds a wrong setting. The message names the setting
                with the command in front, such as "refactor.planner.model".
        """
        try:
            return model.model_validate(self.sections.get(command, {}))
        except ValidationError as exc:
            raise ProjectError(
                f"{self.folder / 'config.toml'} has settings that are wrong:\n"
                f"{_format_errors(exc, prefix=command)}"
            ) from exc


def _format_errors(error: ValidationError, prefix: str = "") -> str:
    """Returns pydantic's errors as one line per wrong setting.

    Args:
        error: The errors from checking config.toml.
        prefix: The section the errors came from, which is put in front of each setting.

    Returns:
        Lines such as "refactor.planner.budget_usd: Input should be a valid number".
    """
    lines = []
    for problem in error.errors():
        where = ".".join(str(part) for part in (prefix, *problem["loc"]) if part != "")
        lines.append(f"  {where}: {problem['msg']}")
    return "\n".join(lines)


def read_markdown(folder: Path, name: str) -> str:
    """Returns the text of one markdown file in the .ratchet folder, which must not be blank.

    Args:
        folder: The .ratchet folder.
        name: The file's name, such as "rules.md" or "problems.md".

    Returns:
        The file's text.

    Raises:
        ProjectError: The file is missing or holds nothing but blank space.
    """
    path = folder / name
    if not path.is_file():
        raise ProjectError(f"{folder} has no {name}")
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise ProjectError(f"{path} is empty")
    return text


def load_project(root: Path, *, commands: Collection[str]) -> Project:
    """Reads a target project's .ratchet folder.

    Args:
        root: The project's own folder, which holds .ratchet.
        commands: The names of ratchet's commands. Only these may have a section in
            config.toml, so that a misspelled section is caught.

    Returns:
        The project's shared settings and rules, with each command's section kept for later.

    Raises:
        ProjectError: The folder or one of its files is missing, rules.md is empty,
            comments.md is there but empty, or config.toml is not valid TOML, holds a wrong shared setting or has a section that
            no command reads.
    """
    root = Path(root)
    folder = root / FOLDER
    if not folder.is_dir():
        raise ProjectError(
            f"{root} has no {FOLDER} folder. ratchet needs {FOLDER}/config.toml and rules.md "
            "there."
        )

    config_path = folder / "config.toml"
    if not config_path.is_file():
        raise ProjectError(f"{folder} has no config.toml")
    try:
        settings = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ProjectError(f"{config_path} is not valid TOML: {exc}") from exc

    # a table is a command's section, and everything else is a shared setting
    sections = {name: value for name, value in settings.items() if isinstance(value, dict)}
    unknown = sorted(set(sections) - set(commands))
    if unknown:
        raise ProjectError(
            f"{config_path} has sections that no command reads: {', '.join(unknown)}. "
            f"ratchet's commands are {', '.join(sorted(commands))}."
        )
    shared = {name: value for name, value in settings.items() if name not in sections}
    try:
        config = Config.model_validate(shared)
    except ValidationError as exc:
        raise ProjectError(f"{config_path} has settings that are wrong:\n{_format_errors(exc)}") from exc

    comments = read_markdown(folder, "comments.md") if (folder / "comments.md").exists() else None
    return Project(
        root=root,
        config=config,
        rules=read_markdown(folder, "rules.md"),
        comments=comments,
        sections=sections,
    )
