"""The module contains the functions that make the copy of a project's code that the audit's
agents work in, and delete it again.

The copy is a clone with its own git history, so an agent can run git log, show and blame. It holds
only the history of the project's current branch, with no other branch, no tag and no remote, and
no object outside that history is left in it.
"""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

from ratchet.git import Repo

# The start of the name of every copy's folder, which remove_copy checks before it deletes.
COPY_PREFIX = "ratchet-audit-"


class CopyError(RuntimeError):
    """Signals a copy that could not be deleted."""


def make_copy(root: Path) -> Path:
    """Makes a clone of a project's committed code with no other branch, tag or remote.

    The clone holds only the history of the project's current branch, and no object outside that
    history is left in it. The clone is checked out at the project's HEAD, detached.

    Args:
        root: The project's own folder.

    Returns:
        The folder of the copy, which the caller deletes with remove_copy().

    Raises:
        GitError: A git command failed.
    """
    folder = Path(tempfile.mkdtemp(prefix=COPY_PREFIX))
    try:
        source = Repo(root)
        source.run("clone", "-q", "--no-local", "--single-branch", "--no-tags", str(root), str(folder))
        copy = Repo(folder)
        # detach at the project's head, then delete every branch and remote
        copy.run("checkout", "-q", "--detach", source.run("rev-parse", "HEAD").strip())
        for branch in copy.run("for-each-ref", "--format=%(refname)", "refs/heads").split():
            copy.run("update-ref", "-d", branch)
        for remote in copy.run("remote").split():
            copy.run("remote", "rm", remote)
        # expire the reflog and prune every object that the remaining refs cannot reach
        copy.run("reflog", "expire", "--expire=now", "--all")
        copy.run("gc", "-q", "--prune=now")
        return folder
    except BaseException:
        remove_copy(folder)
        raise


def remove_copy(folder: Path) -> None:
    """Deletes a copy, including git's read-only object files.

    Args:
        folder: The copy's folder. A folder that is already gone is left alone.

    Raises:
        CopyError: The folder is not a ratchet copy, meaning its name does not start with
            COPY_PREFIX or it is not directly inside the system's temp folder. Nothing is deleted.
    """
    def make_writable(function: Callable[[str], object], path: str, _: object) -> None:
        os.chmod(path, stat.S_IWRITE)
        function(path)

    if not folder.name.startswith(COPY_PREFIX) or (
        folder.resolve().parent != Path(tempfile.gettempdir()).resolve()
    ):
        raise CopyError(f"{folder} is not a ratchet copy, so it is not deleted")
    if folder.exists():
        shutil.rmtree(folder, onerror=make_writable)
