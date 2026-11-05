"""The module contains the functions that make the copy of a project's code that the audit's
agents work in, and delete it again.

The copy is a clone with its own git history, so an agent can run git log, show and blame. It has
no other branch, no tag and no remote, and its history holds no .ratchet files, so an agent cannot
reach an earlier audit's list through git.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

from ratchet.git import GitError, Repo, find_program
from ratchet.project import FOLDER

# The branch that holds the code's history while the strip rewrites it.
WORK_BRANCH = "ratchet-audit-work"


class CopyError(RuntimeError):
    """Signals a copy that could not be made, or a history that could not be cleaned."""


def make_copy(root: Path, hide: list[str], history_with_ratchet: str) -> Path:
    """Makes a clone of a project's committed code with no other branch, tag or remote.

    The history of the clone is cleaned of the .ratchet folder and of every path in 'hide'. The
    clone is checked out at the project's HEAD, detached.

    Args:
        root: The project's own folder.
        hide: Path patterns, relative to the project, that the copy must not hold anywhere in its
            history.
        history_with_ratchet: "strip" removes those paths from the copy's history, and "refuse"
            raises when the history holds any.

    Returns:
        The folder of the copy, which the caller deletes with remove_copy().

    Raises:
        CopyError: The history holds .ratchet files and the setting is "refuse", or the strip
            failed.
        GitError: A git command failed.
    """
    folder = Path(tempfile.mkdtemp(prefix="ratchet-audit-"))
    try:
        source = Repo(root)
        source.run("clone", "-q", "--no-local", str(root), str(folder))
        copy = Repo(folder)
        # detach at the project's head, then delete every branch, tag and remote
        copy.run("checkout", "-q", "--detach", source.run("rev-parse", "HEAD").strip())
        for branch in copy.run("for-each-ref", "--format=%(refname)", "refs/heads").split():
            copy.run("update-ref", "-d", branch)
        for tag in copy.run("tag", "--list").split():
            copy.run("tag", "-d", tag)
        for remote in copy.run("remote").split():
            copy.run("remote", "rm", remote)
        hidden = [FOLDER, *hide]
        if copy.run("log", "--all", "--format=%h", "--", *hidden).strip():
            if history_with_ratchet == "refuse":
                raise CopyError(
                    f"the code's history holds {', '.join(hidden)}, "
                    "and history_with_ratchet is set to refuse"
                )
            strip_history(folder, hidden)
        return folder
    except BaseException:
        remove_copy(folder)
        raise


def strip_history(folder: Path, hidden: list[str]) -> None:
    """Removes paths from every commit of a copy, and then every trace of the old commits.

    Args:
        folder: The copy's folder, with a detached HEAD and no branch.
        hidden: The paths to remove, as git pathspecs.

    Raises:
        CopyError: The rewrite failed or the history still holds one of the paths.
    """
    copy = Repo(folder)
    try:
        copy.run("branch", WORK_BRANCH, "HEAD")
        quoted = " ".join(shlex.quote(path) for path in hidden)
        finished = subprocess.run(
            [
                find_program("git"), "filter-branch", "-f", "--index-filter",
                f"git rm -r --cached --ignore-unmatch -q -- {quoted}", "--", "--all",
            ],
            capture_output=True, text=True, encoding="utf-8", cwd=folder,
            env={**os.environ, "FILTER_BRANCH_SQUELCH_WARNING": "1"},
        )
        if finished.returncode != 0:
            raise CopyError(f"git filter-branch failed: {finished.stderr.strip()}")
        # check out the rewritten head, then drop the temporary branch and the saved originals
        copy.run("checkout", "-q", "-f", "--detach", WORK_BRANCH)
        copy.run("update-ref", "-d", f"refs/heads/{WORK_BRANCH}")
        for reference in copy.run("for-each-ref", "--format=%(refname)", "refs/original").split():
            copy.run("update-ref", "-d", reference)
        copy.run("reflog", "expire", "--expire=now", "--all")
        copy.run("gc", "-q", "--prune=now")
        if copy.run("log", "--all", "--format=%h", "--", *hidden).strip():
            raise CopyError(f"the copy's history still holds {', '.join(hidden)} after the strip")
    except GitError as error:
        raise CopyError(f"the copy's history could not be cleaned: {error}") from error


def remove_copy(folder: Path) -> None:
    """Deletes a copy, including git's read-only object files.

    Args:
        folder: The copy's folder. A folder that is already gone is left alone.
    """
    def make_writable(function: Callable[[str], object], path: str, _: object) -> None:
        os.chmod(path, stat.S_IWRITE)
        function(path)

    if folder.exists():
        shutil.rmtree(folder, onerror=make_writable)
