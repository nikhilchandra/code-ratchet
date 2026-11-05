"""The module contains the functions that keep a project's .ratchet folder as its own git
repository, called the records repository.

The repository tracks the problem lists, config.toml, rules.md, comments.md and each list's
ledger.json. It ignores every other file under state, such as job folders and audit folders.
Its commits carry a fixed identity, so they work on a machine with no git identity set.
"""

from __future__ import annotations

from pathlib import Path

from ratchet.git import Repo
from ratchet.project import FOLDER

# The records repository's .gitignore, which keeps each state/<list>/ledger.json and ignores the
# rest of state.
IGNORE_LINES = (
    "/state/*",
    "!/state/*/",
    "/state/*/*",
    "!/state/*/ledger.json",
)

# The identity that every records commit carries.
IDENTITY = ("-c", "user.name=ratchet", "-c", "user.email=ratchet@localhost")


def records_repo(root: Path) -> Repo:
    """Returns the git helper for a project's records repository.

    Args:
        root: The project's own folder, which holds .ratchet.

    Returns:
        A helper that runs git inside the .ratchet folder.
    """
    return Repo(root / FOLDER)


def has_records(root: Path) -> bool:
    """Answers whether a project's .ratchet folder is its own repository.

    Args:
        root: The project's own folder, which holds .ratchet.

    Returns:
        True when .ratchet holds a .git entry that git takes as the folder's own repository. A
        damaged .git, which git would pass over for the project's repository, gives False.
    """
    if not (root / FOLDER / ".git").exists():
        return False
    return records_repo(root).run("rev-parse", "--git-dir", check=False).strip() == ".git"


def init_records(root: Path) -> bool:
    """Makes the .ratchet folder a repository and writes its .gitignore, each only when still
    needed.

    Args:
        root: The project's own folder, which holds .ratchet.

    Returns:
        True when this call changed anything.

    Raises:
        GitError: git init failed.
    """
    folder = root / FOLDER
    folder.mkdir(exist_ok=True)
    changed = False
    if not has_records(root):
        records_repo(root).run("init", "-q", "-b", "main")
        changed = True
    ignore = folder / ".gitignore"
    text = "\n".join(IGNORE_LINES) + "\n"
    if not ignore.is_file() or ignore.read_text(encoding="utf-8") != text:
        ignore.write_text(text, encoding="utf-8", newline="\n")
        changed = True
    return changed


def commit_records(root: Path, message: str) -> str | None:
    """Commits every tracked change in the records repository.

    Args:
        root: The project's own folder, which holds .ratchet.
        message: The commit message.

    Returns:
        The short hash of the new commit, or None when nothing changed.

    Raises:
        GitError: The folder is not a repository or the commit failed.
    """
    repo = records_repo(root)
    repo.run("add", "-A")
    if not repo.run("status", "--porcelain").strip():
        return None
    repo.run(*IDENTITY, "commit", "-q", "-m", message)
    return repo.last_commit()


def records_log(root: Path, limit: int = 20) -> list[str]:
    """Reads the newest commits of the records repository.

    Args:
        root: The project's own folder, which holds .ratchet.
        limit: The most commits to return.

    Returns:
        One line per commit, newest first, such as "6918463 2026-11-02 14:05 reopen P7: reason".
        The list is empty when there is no repository or no commit.
    """
    if not has_records(root):
        return []
    output = records_repo(root).run(
        "log", f"-n{limit}", "--date=format:%Y-%m-%d %H:%M", "--format=%h %cd %s", check=False
    )
    return output.splitlines()
