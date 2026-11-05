"""The module contains the function behind `ratchet init`, which sets up a project's .ratchet
folder and its records repository, and moves a project that tracked .ratchet in its own git
history over to the records repository.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ratchet.git import Repo
from ratchet.project import FOLDER
from ratchet.records import commit_records, has_records, init_records, records_repo

TEMPLATES = Path(__file__).parent / "templates"

# The lines of a .gitignore that already keep the .ratchet folder out of the project's git.
IGNORE_FORMS = (FOLDER, f"{FOLDER}/", f"/{FOLDER}", f"/{FOLDER}/")

# What the owner is told about the files of a .ratchet folder that the project's git stops tracking.
RESTORE_NOTE = (
    "The files stay on disk in this clone, but checking out an older commit can replace or delete "
    "them, and pulling this commit into another clone deletes them there. In this clone, git -C "
    f"{FOLDER} restore . brings them back from the records repository. Another clone has no "
    f"records repository, so copy the {FOLDER} folder over from this one."
)


class InitError(RuntimeError):
    """Signals a project that init cannot set up, or a move that the owner has not agreed to."""


def init_project(
    root: Path, *, assume_yes: bool, confirm: Callable[[str], bool] | None
) -> list[str]:
    """Sets up the .ratchet folder of a project, doing each step only when it is still needed.

    The steps are writing config.toml and rules.md from the templates, making .ratchet its own
    repository, untracking .ratchet in the project's git, ignoring it there, and making the first
    records commit. A second run changes nothing.

    Args:
        root: The project's own folder, which must be a git repository.
        assume_yes: True agrees to untrack .ratchet without asking.
        confirm: Asks the owner a yes or no question, or None when there is no terminal.

    Returns:
        One line for each change made, which is empty when nothing needed changing.

    Raises:
        InitError: The project is not a git repository, has changes outside .ratchet, or tracks
            .ratchet and the owner has not agreed to untrack it.
        GitError: A git command failed.
    """
    project = Repo(root)
    if project.run("rev-parse", "--is-inside-work-tree", check=False).strip() != "true":
        raise InitError(f"{root} is not a git repository")
    if project.changed_paths():
        raise InitError(
            f"{root} has changes that are not committed. Commit or set them aside, then run "
            "ratchet init again."
        )

    tracked = bool(project.run("ls-files", "--", FOLDER).strip())
    if tracked and not assume_yes:
        question = (
            f"This project's git tracks {FOLDER}. ratchet init will stop tracking it, add it to "
            f".gitignore and commit that. {RESTORE_NOTE}"
        )
        if confirm is None:
            raise InitError(f"{question} Run ratchet init --yes to agree.")
        if not confirm(question):
            raise InitError("ratchet init stopped, and nothing was changed")

    actions = []
    written = _write_templates(root)
    actions.extend(f"wrote {FOLDER}/{name}" for name in written)
    if init_records(root):
        actions.append(f"set up {FOLDER} as its own repository")

    if tracked:
        project.run("rm", "-r", "--cached", "-q", "--", FOLDER)
        if not _is_ignored(root):
            _add_ignore_line(root)
        project.run("add", ".gitignore")
        project.run("commit", "-q", "-m", f"chore: stop tracking {FOLDER} in this repository")
        actions.append(f"stopped tracking {FOLDER} in the project's git. {RESTORE_NOTE}")
    elif not _is_ignored(root):
        _add_ignore_line(root)
        project.run("add", ".gitignore")
        project.run("commit", "-q", "-m", f"chore: ignore {FOLDER}", "--", ".gitignore")
        actions.append(f"added {FOLDER}/ to .gitignore")

    records = records_repo(root)
    if not records.run("rev-parse", "--verify", "-q", "HEAD", check=False).strip():
        commit_records(root, _first_message(project))
        actions.append("made the first commit in the records repository")
    elif written:
        commit_records(root, "ratchet init: write " + " and ".join(written))
        actions.append("committed the new files in the records repository")
    return actions


def _write_templates(root: Path) -> list[str]:
    """Writes config.toml and rules.md from the templates, leaving a file that exists alone.

    Args:
        root: The project's own folder.

    Returns:
        The names of the files written.
    """
    folder = root / FOLDER
    folder.mkdir(exist_ok=True)
    written = []
    for name in ("config.toml", "rules.md"):
        target = folder / name
        if not target.exists():
            target.write_text((TEMPLATES / name).read_text(encoding="utf-8"),
                              encoding="utf-8", newline="\n")
            written.append(name)
    return written


def _is_ignored(root: Path) -> bool:
    """Answers whether the project's .gitignore already has a line for the .ratchet folder.

    Args:
        root: The project's own folder.

    Returns:
        True when one of its lines names the .ratchet folder.
    """
    ignore = root / ".gitignore"
    if not ignore.is_file():
        return False
    lines = ignore.read_text(encoding="utf-8").splitlines()
    return any(line.strip() in IGNORE_FORMS for line in lines)


def _add_ignore_line(root: Path) -> None:
    """Adds a line for the .ratchet folder to the project's .gitignore, making the file if needed.

    Args:
        root: The project's own folder.
    """
    ignore = root / ".gitignore"
    text = ignore.read_text(encoding="utf-8") if ignore.is_file() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    ignore.write_text(f"{text}{FOLDER}/\n", encoding="utf-8", newline="\n")


def _first_message(project: Repo) -> str:
    """Returns the message of the first records commit, which lists the project's commits that
    touched .ratchet.

    Args:
        project: The project's git helper.

    Returns:
        The commit message.
    """
    commits = project.run("log", "--format=%h %cd %s", "--date=short", "--", FOLDER).splitlines()
    message = "ratchet init: start the records repository"
    if commits:
        message += "\n\nProject commits that touched .ratchet:\n" + "\n".join(
            f"- {line}" for line in commits)
    return message
