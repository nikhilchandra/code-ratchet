"""Builds each refactor agent's instructions from ratchet's general prompts and the project's own
rules.

An agent's profile is the shared prompt followed by its role's prompt. The shared prompt takes
the project's rules.md and comments.md, and both prompts take the paths and the test command
from the project's config.toml, so that no prompt names a particular project.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from string import Template

from ratchet.project import Project

PROMPTS = Path(__file__).parent / "prompts"

# What the comments section says when the project has no comments.md.
NO_COMMENT_RULES = "The project sets no further rules for comments."


class Role(StrEnum):
    """Names the three refactor agents, each of which has a prompt of its own."""

    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"


def _format_patterns(patterns: list[str]) -> str:
    """Returns path patterns as a list for a prompt, such as "`src/**` or `tests/**`".

    Args:
        patterns: The path patterns.

    Returns:
        The patterns, each in backticks, joined with commas and a final "or".
    """
    quoted = [f"`{pattern}`" for pattern in patterns]
    if len(quoted) < 2:
        return "".join(quoted)
    return ", ".join(quoted[:-1]) + " or " + quoted[-1]


def _format_command(words: list[str]) -> str:
    """Returns a command as it would be typed, such as 'uv run pytest -m ""'.

    Args:
        words: The command and its arguments.

    Returns:
        The words joined by spaces, with any word that is empty or holds a space in double
        quotes.
    """
    return " ".join(f'"{word}"' if not word or " " in word else word for word in words)


def build_profile(project: Project, role: Role) -> str:
    """Returns one agent's instructions, with the project's rules and settings filled in.

    Args:
        project: The project, as read from its .ratchet folder.
        role: The agent the instructions are for.

    Returns:
        The shared prompt and the role's prompt, joined by a blank line.
    """
    comments = NO_COMMENT_RULES
    if project.comments:
        comments = "Follow these rules in that code.\n\n" + project.comments.strip()
    values = {
        "rules": project.rules.strip(),
        "comments": comments,
        "editable": _format_patterns(project.config.editable),
        "frozen": _format_patterns(project.frozen),
        "test_command": _format_command(project.config.test_command),
    }
    parts = [PROMPTS / "shared.md", PROMPTS / f"{role}.md"]
    return "\n\n".join(
        Template(path.read_text(encoding="utf-8")).substitute(values).strip() for path in parts
    ) + "\n"


def write_profile(project: Project, role: Role, folder: Path) -> Path:
    """Writes one agent's instructions to a file, which an Agent takes as its profile.

    Args:
        project: The project, as read from its .ratchet folder.
        role: The agent the instructions are for.
        folder: The folder to write the file in, which is made if it does not exist.

    Returns:
        The file, named after the role, such as "planner.md".
    """
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{role}.md"
    path.write_text(build_profile(project, role), encoding="utf-8")
    return path
