"""Finds the newest problem list in a project's .ratchet folder.

A problem list is a markdown file named after the minute it was made, such as
problems-20261026-14h32m.md. A new list never replaces an old one, so the folder keeps every
list, and ratchet works on the newest. Each problem has a heading that starts with its id, such
as "## P19. The controller is too big", which is how ratchet knows which ids the list holds.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ratchet.project import FOLDER, Project, ProjectError, read_markdown

# A problem list's file name, which holds the date and the time it was made.
FILE_NAME = re.compile(r"problems-(\d{8})-(\d{2})h(\d{2})m\.md")
# A heading that starts with a problem id, such as "#### P3. Something".
PROBLEM_HEADING = re.compile(r"^#+\s+(P\d+)\b", re.MULTILINE)


@dataclass(frozen=True)
class ProblemList:
    """Holds one problem list, as read from its file."""

    path: Path
    # When the list was made, from its file name.
    created: datetime
    text: str
    # The id of every problem the list holds, in the order of their headings.
    ids: list[str]

    @property
    def name(self) -> str:
        """Returns the list's file name without .md, which also names its ledger's folder."""
        return self.path.stem

    @property
    def relative_path(self) -> str:
        """Returns the list's path from the project's folder, such as
        ".ratchet/problems-20261026-14h32m.md"."""
        return f"{FOLDER}/{self.path.name}"


def find_latest_problem_list(project: Project) -> ProblemList:
    """Returns the newest problem list in the .ratchet folder.

    Args:
        project: The project, as read from its .ratchet folder.

    Returns:
        The list whose file name holds the latest date and time.

    Raises:
        ProjectError: The folder holds no problem list, a file that starts with "problems"
            is misnamed, or the newest list is empty or has no problem headings.
    """
    found: list[tuple[datetime, Path]] = []
    misnamed: list[str] = []
    for path in sorted(project.folder.glob("problems*.md")):
        match = FILE_NAME.fullmatch(path.name)
        if match is None:
            misnamed.append(path.name)
            continue
        day, hour, minute = match.groups()
        try:
            created = datetime.strptime(f"{day}{hour}{minute}", "%Y%m%d%H%M")
        except ValueError:
            misnamed.append(path.name)
            continue
        found.append((created, path))

    if misnamed:
        raise ProjectError(
            f"{project.folder} has problem lists that are misnamed: {', '.join(misnamed)}. "
            "Name each one after the minute it was made, such as problems-20261026-14h32m.md."
        )
    if not found:
        raise ProjectError(
            f"{project.folder} has no problem list. Write one named after the minute it was "
            "made, such as problems-20261026-14h32m.md."
        )

    created, path = max(found)
    text = read_markdown(project.folder, path.name)
    ids = list(dict.fromkeys(PROBLEM_HEADING.findall(text)))
    if not ids:
        raise ProjectError(
            f"{path} has no problem headings. Start each problem's heading with its id, such "
            'as "## P19. The controller is too big".'
        )
    return ProblemList(path=path, created=created, text=text, ids=ids)
