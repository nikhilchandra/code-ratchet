"""Runs the git commands that carry a step: finding what changed, committing it, undoing it, and
saving half-made work so that it can be put back.

Every command leaves out the .ratchet folder, so ratchet's own files are never committed, undone
or counted as a step's change.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from ratchet.project import FOLDER


class GitError(RuntimeError):
    """Signals a git command that failed."""


def find_program(name: str) -> str:
    """Returns the full path of a program on the PATH, which Windows needs to find a .exe or .cmd.

    Args:
        name: The program's name, such as "git".

    Returns:
        The program's full path, or 'name' itself when the PATH does not hold it.
    """
    return shutil.which(name) or name


class Repo:
    """Runs git in one repository, on every file outside the .ratchet folder."""

    def __init__(self, root: Path) -> None:
        """Sets the repository that every command runs in.

        Args:
            root: The repository's top folder.
        """
        self.root = Path(root)
        # The pathspec of a step's files, which is everything but the .ratchet folder.
        self.step_paths = (".", f":(exclude){FOLDER}")

    def run(self, *arguments: str, check: bool = True) -> str:
        """Runs one git command and returns what it printed.

        Args:
            arguments: The command's arguments, such as ("status", "--short").
            check: True raises on a nonzero exit code.

        Returns:
            The command's standard output.

        Raises:
            GitError: The command failed and 'check' is True.
        """
        finished = subprocess.run(
            [find_program("git"), *arguments],
            capture_output=True, text=True, encoding="utf-8", cwd=self.root,
        )
        if check and finished.returncode != 0:
            raise GitError(f"git {' '.join(arguments)} failed: {finished.stderr.strip()}")
        return finished.stdout

    def last_commit(self) -> str:
        """Returns the short hash of the last commit."""
        return self.run("rev-parse", "--short", "HEAD").strip()

    def changed_paths(self) -> list[str]:
        """Returns every file outside .ratchet that differs from the last commit, new files
        included, with the new name of a renamed file."""
        entries = self.run(
            "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", *self.step_paths
        ).split("\0")
        paths = []
        index = 0
        while index < len(entries):
            entry = entries[index]
            index += 1
            if not entry:
                continue
            paths.append(entry[3:])
            # a rename or a copy is followed by the old name, which is skipped
            if entry[0] in "RC":
                index += 1
        return paths

    def diff(self, *paths: str) -> str:
        """Returns the diff of every change outside .ratchet against the last commit, new files
        included.

        The a/ and b/ path prefixes are fixed and color is off, so the owner's diff.noprefix,
        diff.mnemonicPrefix and color settings cannot change what a reader of the diff sees.

        Args:
            paths: The files to limit the diff to. None means every file.

        Returns:
            The diff, which is empty when nothing changed.
        """
        self.run("add", "-A", "--", *self.step_paths)
        try:
            return self.run("diff", "--cached", "--src-prefix=a/", "--dst-prefix=b/", "--no-color", "--", *paths)
        finally:
            self.run("reset", "-q")

    def commit(self, message: str) -> str:
        """Commits every change outside .ratchet and returns the new commit's short hash.

        Args:
            message: The commit message.

        Returns:
            The short hash of the commit.

        Raises:
            GitError: The commit failed, such as when nothing changed.
        """
        self.run("add", "-A", "--", *self.step_paths)
        self.run("commit", "-q", "-m", message)
        return self.last_commit()

    def undo(self) -> None:
        """Puts every file outside .ratchet back as it was at the last commit, and deletes new
        files that git does not ignore."""
        self.run("reset", "-q", "--hard", "HEAD")
        self.run("clean", "-fdq", "--", *self.step_paths)

    def save_work(self) -> bytes:
        """Returns every change outside .ratchet as a patch, new files included.

        The patch is kept as bytes, since a text pipe on Windows rewrites its line endings and
        the patch would then no longer apply.

        Returns:
            The patch, which is empty when nothing changed.
        """
        self.run("add", "-A", "--", *self.step_paths)
        try:
            return subprocess.run(
                [find_program("git"), "diff", "--cached", "--binary"],
                capture_output=True, cwd=self.root, check=True,
            ).stdout
        finally:
            self.run("reset", "-q")

    def restore_work(self, patch: bytes) -> None:
        """Puts every file outside .ratchet back to exactly the state that save_work() returned.

        Args:
            patch: A patch from save_work().

        Raises:
            GitError: The patch does not apply.
        """
        self.undo()
        if not patch:
            return
        finished = subprocess.run(
            [find_program("git"), "apply", "--whitespace=nowarn"],
            input=patch, capture_output=True, cwd=self.root,
        )
        if finished.returncode != 0:
            raise GitError(
                "the saved work could not be put back: "
                f"{finished.stderr.decode(errors='replace').strip()}"
            )
