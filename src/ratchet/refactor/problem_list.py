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
# Any heading, with its marks and its text.
HEADING = re.compile(r"^(#+)\s+(.*?)\s*$")
# A heading that starts with a problem, question or dropped id, with the rest as its title.
ENTRY_HEADING = re.compile(r"([PQD]\d+)\b\.?\s*(.*)")
# The field lines an entry may hold under its heading.
FIELD_LINE = re.compile(r"^(Status|Sources|Grade|Subject|Carried from|After):[ \t]*(.*?)\s*$")


@dataclass(frozen=True)
class Entry:
    """Holds one entry of a problem list, which is a problem, a question or a dropped entry."""

    # The entry's id, such as "P7", "Q2" or "D1".
    id: str
    # The heading's text after the id.
    title: str
    # The text of the nearest heading above that is not an entry, or "" when there is none.
    section: str
    # The entry's text from its heading to the next heading of the same or a higher level.
    text: str
    # The value of each field line the entry holds, such as "Status", by the name as written.
    fields: dict[str, str]

    @property
    def is_problem(self) -> bool:
        """Returns whether the entry is a problem, and not a question or a dropped entry."""
        return self.id.startswith("P")


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
    def entries(self) -> list[Entry]:
        """Returns the list's entries, which are read from its text."""
        return split_entries(self.text)

    @property
    def name(self) -> str:
        """Returns the list's file name without .md, which also names its ledger's folder."""
        return self.path.stem

    @property
    def relative_path(self) -> str:
        """Returns the list's path from the project's folder, such as
        ".ratchet/problems-20261026-14h32m.md"."""
        return f"{FOLDER}/{self.path.name}"


def find_problem_lists(project: Project) -> list[tuple[datetime, Path]]:
    """Returns the date and path of every problem list in the .ratchet folder, oldest first.

    Args:
        project: The project, as read from its .ratchet folder.

    Returns:
        One pair for each file named like a problem list.

    Raises:
        ProjectError: A file that starts with "problems" is misnamed.
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
    return sorted(found)


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
    found = find_problem_lists(project)
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


def split_entries(text: str) -> list[Entry]:
    """Reads the entries of a problem list, in the order of their headings.

    Args:
        text: The list's markdown.

    Returns:
        One entry for each heading that starts with a P, Q or D id.
    """
    lines = text.splitlines()
    entries: list[Entry] = []
    section = ""
    # the level of the entry whose text the loop is inside, or 0 outside every entry
    inside = 0
    for start, line in enumerate(lines):
        heading = HEADING.match(line)
        if heading is None:
            continue
        marks, words = heading.groups()
        match = ENTRY_HEADING.fullmatch(words)
        if match is None:
            # a heading inside an entry belongs to the entry
            if len(marks) > inside:
                if inside == 0:
                    section = words
                continue
            inside = 0
            section = words
            continue
        inside = len(marks)
        # the entry runs to the next heading of the same or a higher level
        end = len(lines)
        for later in range(start + 1, len(lines)):
            other = HEADING.match(lines[later])
            if other is not None and len(other.group(1)) <= len(marks):
                end = later
                break
        body = lines[start:end]
        fields: dict[str, str] = {}
        for body_line in body[1:]:
            field = FIELD_LINE.match(body_line)
            if field is not None:
                fields.setdefault(field.group(1), field.group(2))
        entries.append(Entry(id=match.group(1), title=match.group(2), section=section,
                             text="\n".join(body).strip(), fields=fields))
    return entries


def highest_problem_id(project: Project) -> int:
    """Returns the highest problem number across every list in the .ratchet folder.

    Args:
        project: The project, as read from its .ratchet folder.

    Returns:
        The number of the highest P id in any list, such as 19 for "P19", or 0 when no list
        holds a problem.

    Raises:
        ProjectError: A file that starts with "problems" is misnamed.
    """
    highest = 0
    for _, path in find_problem_lists(project):
        text = read_markdown(project.folder, path.name)
        for entry in split_entries(text):
            if entry.is_problem:
                highest = max(highest, int(entry.id[1:]))
    return highest
