"""The module contains the walk over a unified diff that the map check and the coverage check share."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class DiffLine:
    """A line inside a hunk of a unified diff, with the files and line numbers it belongs to.

    old_path and new_path are the file's path in the old and the new version, with git's a/ or b/
    prefix removed. A file that is added has no old_path and a file that is deleted has no new_path.
    old_line and new_line are the numbers the line has before it is counted, so a removed line has
    no place in the new file and new_line is the line it sits before. The marker is ' ', '+' or '-',
    and text is the whole line, marker included.
    """

    old_path: str | None
    new_path: str | None
    old_line: int
    new_line: int
    marker: str
    text: str


def _path_of(header: str) -> str | None:
    """Returns the path in a '---' or '+++' header line, or None for /dev/null."""
    path = header[4:].split("\t")[0].strip()
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def diff_lines(diff_text: str) -> Iterator[DiffLine]:
    """Yields every context, added and removed line of a unified diff, in order.

    Each hunk is read for as many old and new lines as its header counts, so a removed line that
    starts with '--' is not mistaken for a file header. A '\\ No newline at end of file' note is
    skipped. A path with no a/ or b/ prefix is kept as it is.

    Args:
        diff_text: The unified diff.

    Yields:
        One DiffLine for each line inside a hunk.
    """
    old_path: str | None = None
    new_path: str | None = None
    lines = diff_text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.startswith("--- "):
            old_path = _path_of(line)
            continue
        if line.startswith("+++ "):
            new_path = _path_of(line)
            continue
        match = _HUNK.match(line)
        if not match:
            continue
        old_line = int(match.group(1))
        old_left = int(match.group(2) or 1)
        new_line = int(match.group(3))
        new_left = int(match.group(4) or 1)
        while (old_left > 0 or new_left > 0) and index < len(lines):
            text = lines[index]
            index += 1
            if text.startswith("\\"):
                continue
            # an empty line stands for a context line
            marker = text[:1] or " "
            if marker != "+":
                old_left -= 1
            if marker != "-":
                new_left -= 1
            yield DiffLine(old_path, new_path, old_line, new_line, marker, text)
            if marker != "+":
                old_line += 1
            if marker != "-":
                new_line += 1
