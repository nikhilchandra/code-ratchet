"""The module contains the functions that follow the evidence of an old problem list into the
code as it stands now, so that the rechecker starts from where each cited line went.
"""

from __future__ import annotations

import re
from pathlib import Path

from ratchet.audit.agent_answers import Citation
from ratchet.audit.citations import MOVED, NO_FILE, NO_QUOTE, NOT_FOUND, OK, check_citation
from ratchet.git import GitError, Repo
from ratchet.refactor.problem_list import Entry

# An evidence line of a list, such as "- app/views.py:12-14: the copy".
EVIDENCE_LINE = re.compile(r"^- (\S+?):(\d+)-(\d+): (.*)$")
# the indent of each line of a quote
QUOTE_INDENT = "      "


def old_citations(entry: Entry) -> list[Citation]:
    """Reads the evidence citations out of an old list's entry.

    Args:
        entry: The entry, as read by split_entries.

    Returns:
        One citation for each evidence line, in order, with the lines of its indented quote. A line
        whose numbers are not a valid range is skipped.
    """
    citations: list[Citation] = []
    lines = entry.text.splitlines()
    index = 0
    while index < len(lines):
        match = EVIDENCE_LINE.match(lines[index])
        index += 1
        if match is None:
            continue
        file, first, last, shows = match.groups()
        quote: list[str] = []
        while index < len(lines) and lines[index].startswith(QUOTE_INDENT):
            quote.append(lines[index][len(QUOTE_INDENT):])
            index += 1
        line, end_line = int(first), int(last)
        if line < 1 or end_line < line:
            continue
        citations.append(Citation(file=file, line=line, end_line=end_line,
                                  quote="\n".join(quote), shows=shows))
    return citations


def renamed_files(repo: Repo, since: str) -> dict[str, str]:
    """Finds the files that git sees as renamed between a commit and HEAD.

    Args:
        repo: The repository to ask.
        since: The commit the old list was made at.

    Returns:
        A map from each old path to its new path. It is empty when git cannot compare the commits.
    """
    try:
        output = repo.run("diff", "--name-status", "-M", since, "HEAD")
    except GitError:
        return {}
    renames: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].startswith("R"):
            renames[parts[1]] = parts[2]
    return renames


def build_evidence_hints(
    root: Path, entries: list[Entry], renames: dict[str, str] | None = None
) -> str:
    """Writes the hints for the rechecker, one line for each citation of each old problem.

    The old line numbers are only used to find the quote again. The rechecker must still give the
    current citations itself.

    Args:
        root: The folder holding the current code.
        entries: The entries of the old list. Questions and dropped entries are left out.
        renames: The map from old paths to new paths, from renamed_files. None means no renames.

    Returns:
        The hints as markdown. A problem with no evidence lines is listed as having none.
    """
    renames = renames or {}
    lines = ["# Evidence hints", ""]
    for entry in entries:
        if not entry.is_problem:
            continue
        lines.append(f"## {entry.id}. {entry.title}")
        citations = old_citations(entry)
        if not citations:
            lines.append("- no evidence lines in the old list")
        for citation in citations:
            lines.append(f"- {_describe(root, citation, renames)}")
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def _describe(root: Path, citation: Citation, renames: dict[str, str]) -> str:
    """Says where one old citation stands now.

    Args:
        root: The folder holding the current code.
        citation: The citation from the old list.
        renames: The map from old paths to new paths.

    Returns:
        A phrase that starts with the old place, such as "a.py:3-4 is unchanged".
    """
    place = f"{citation.file}:{citation.line}-{citation.end_line}"
    result = check_citation(root, citation)
    if result.status == OK:
        return f"{place} is unchanged"
    if result.fixed is not None and result.status == MOVED:
        return f"{place} is now at {citation.file}:{result.fixed.line}-{result.fixed.end_line}"
    new_file = renames.get(citation.file)
    if new_file is not None:
        moved = check_citation(root, citation.model_copy(update={"file": new_file}))
        if moved.fixed is not None:
            return (f"{place} is now at {new_file}:{moved.fixed.line}-{moved.fixed.end_line}, "
                    f"the file was renamed")
        return f"{place} was in a file renamed to {new_file}, and the quote is not found there"
    reasons = {
        NO_FILE: "the file no longer exists",
        NOT_FOUND: "the quote is not found in the file",
        NO_QUOTE: "the old list gave no quote",
    }
    return f"{place}: {reasons[result.status]}"
