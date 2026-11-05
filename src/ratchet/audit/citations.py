"""The module contains the functions that check each citation an agent gave against the code, fix
the lines of a citation whose quote has moved, and report what was found.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ratchet.audit.agent_answers import Citation

OK = "ok"
MOVED = "moved"
NOT_FOUND = "not found"
NO_FILE = "no file"
NO_QUOTE = "no quote"


@dataclass(frozen=True)
class CitationResult:
    """The outcome of checking one citation.

    Attributes:
        citation: The citation as the agent gave it.
        status: "ok" when the quote is at the cited lines, "moved" when it was found elsewhere in
            the file, and "not found", "no file" or "no quote" when it cannot be trusted.
        fixed: The citation with its lines corrected when the status is "moved", the same citation
            when it is "ok", and None otherwise.
    """

    citation: Citation
    status: str
    fixed: Citation | None


def check_citation(root: Path, citation: Citation) -> CitationResult:
    """Checks that a citation's quote sits at its cited lines, and finds it again if it does not.

    Lines are compared with their leading and trailing whitespace removed. When the quote is not at
    the cited lines, the nearest place in the file where it appears is used instead.

    Args:
        root: The folder the citation's file is relative to.
        citation: The citation to check.

    Returns:
        The result, with the corrected citation when the quote was found.
    """
    quote = [line.strip() for line in citation.quote.strip().splitlines()]
    if not quote:
        return CitationResult(citation, NO_QUOTE, None)
    lines = _read_lines(root, citation.file)
    if lines is None:
        return CitationResult(citation, NO_FILE, None)
    starts = [
        index
        for index in range(len(lines) - len(quote) + 1)
        if lines[index : index + len(quote)] == quote
    ]
    if not starts:
        return CitationResult(citation, NOT_FOUND, None)
    # pick the match nearest the cited line and rebuild the range from the quote's length
    wanted = citation.line - 1
    nearest = min(starts, key=lambda index: abs(index - wanted))
    fixed = citation.model_copy(update={"line": nearest + 1, "end_line": nearest + len(quote)})
    if fixed == citation:
        return CitationResult(citation, OK, citation)
    return CitationResult(citation, MOVED, fixed)


def check_citations(root: Path, citations: list[Citation]) -> list[CitationResult]:
    """Checks every citation in the list.

    Args:
        root: The folder the citations' files are relative to.
        citations: The citations to check.

    Returns:
        One result for each citation, in the same order.
    """
    return [check_citation(root, citation) for citation in citations]


def render_report(results: list[CitationResult]) -> str:
    """Writes the citation report, with one line for each citation that was not fine as given.

    Args:
        results: The results from check_citations.

    Returns:
        The report as markdown, which counts the citations and lists the ones that need attention.
    """
    problems = [result for result in results if result.status != OK]
    lines = ["# Citation check", "", f"{len(results)} citations, {len(problems)} not fine as given.", ""]
    for result in problems:
        cited = result.citation
        place = f"{cited.file}:{cited.line}-{cited.end_line}"
        if result.fixed is not None:
            lines.append(f"- {place} moved to lines {result.fixed.line}-{result.fixed.end_line}")
        else:
            lines.append(f"- {place} {result.status}")
    return "\n".join(lines) + "\n"


def _read_lines(root: Path, file: str) -> list[str] | None:
    """Reads a file inside the root as a list of stripped lines.

    Args:
        root: The project root that the file must sit inside.
        file: The path of the file, relative to the root.

    Returns:
        The stripped lines of the file, or None when the file is missing, outside the root, or
        cannot be read as UTF-8.
    """
    base = root.resolve()
    path = (base / file).resolve()
    if base not in path.parents:
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return [line.strip() for line in text.splitlines()]
