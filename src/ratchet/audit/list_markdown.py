"""The module contains the functions that render an audit's draft as a problem list, remove the
owner notes from a draft, and check that a draft can be published.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass, field

from ratchet.audit.agent_answers import Citation
from ratchet.refactor.problem_list import split_entries

# The statuses a problem may have in a draft.
VALID_STATUSES = ("open", "new", "reopened", "blocked")
# The marker that starts a note for the owner, which is removed when the list is published.
NOTE_MARKER = "> Owner note:"
# The heading of the section that holds the notes that belong to no entry.
NOTES_HEADING = "## Owner notes"
OPEN_HEADING = "## Open"
# The heading of the section that holds the questions for the owner, without its marker.
QUESTIONS_HEADING = "Questions for the owner"


@dataclass
class DraftProblem:
    """Holds one problem of a draft, with every line the list shows for it.

    Attributes:
        id: The problem's id, such as "P7".
        title: The text after the id in the heading.
        status: One of VALID_STATUSES.
        sources: The labels of the items that were grouped into it, such as "C1" or "O P7".
        grade: "stated", "inferred" or "standard".
        breaks: The rule quoted from rules.md, or a standard id such as S7. Empty for none.
        subject: The module, class or function the problem is about.
        what_is_wrong: The problem's text.
        carried_from: The older list and problem it continues, such as
            "problems-20261102-14h05m P7", or "".
        after: The ids of the problems that must be done first.
        evidence: The citations that show the problem.
        simpler_option: The simpler design that would fix it.
        done_when: The conditions that mean it is fixed.
        tests: The tests that reach the code, each as a pair of the file and how it reaches it.
        notes: Notes for the owner, which are removed when the list is published.
    """

    id: str
    title: str
    status: str
    sources: list[str]
    grade: str
    breaks: str
    subject: str
    what_is_wrong: str
    carried_from: str = ""
    after: list[str] = field(default_factory=list)
    evidence: list[Citation] = field(default_factory=list)
    simpler_option: str = ""
    done_when: list[str] = field(default_factory=list)
    tests: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class DraftQuestion:
    """Holds one question for the owner, which the owner answers on its Answer line."""

    id: str
    title: str
    text: str


@dataclass
class DraftDropped:
    """Holds one entry that the audit dropped, with the reason."""

    id: str
    title: str
    reason: str


@dataclass
class DraftDone:
    """Holds one old problem that the audit found done or gone.

    Attributes:
        id: The problem's id, such as "P7".
        title: The problem's title.
        verdict: "done" or "gone".
        reason: What the rechecker said was left, or "" for nothing.
    """

    id: str
    title: str
    verdict: str
    reason: str


@dataclass
class Draft:
    """Holds everything that goes into a draft problem list.

    Attributes:
        problems: The open problems.
        questions: The questions for the owner.
        dropped: The dropped entries.
        done: The old problems that are done or gone.
        bugs: One line for each bug noticed.
        notes: Notes for the owner that belong to no entry.
    """

    problems: list[DraftProblem] = field(default_factory=list)
    questions: list[DraftQuestion] = field(default_factory=list)
    dropped: list[DraftDropped] = field(default_factory=list)
    done: list[DraftDone] = field(default_factory=list)
    bugs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def render_draft(draft: Draft) -> str:
    """Renders a draft as the markdown of a problem list.

    Args:
        draft: The draft to render.

    Returns:
        The list's text. Every problem has a heading under "Open", and the questions, the dropped
        entries, the done problems and the bugs follow in their own sections. A done problem is a
        bullet, so no reader takes it for a problem.
    """
    lines = ["# Problems", ""]
    if draft.notes:
        lines += [NOTES_HEADING, ""]
        lines += [f"- {' '.join(note.split())}" for note in draft.notes]
        lines.append("")
    lines += [OPEN_HEADING, ""]
    for problem in draft.problems:
        lines += _render_problem(problem)
    lines += [f"## {QUESTIONS_HEADING}", ""]
    for question in draft.questions:
        lines += [f"### {question.id}. {question.title}", "", question.text.strip(), "",
                  "Answer:", ""]
    lines += ["## Dropped", ""]
    for dropped in draft.dropped:
        lines += [f"### {dropped.id}. {dropped.title}", "", dropped.reason.strip(), ""]
    lines += ["## Done", ""]
    for done in draft.done:
        line = f"- {done.id}. {done.title} ({done.verdict})"
        if done.reason.strip():
            line += f": {done.reason}"
        lines.append(" ".join(line.split()))
    lines.append("")
    lines += ["## Bugs noticed", ""]
    lines += [f"- {bug}" for bug in draft.bugs]
    lines.append("")
    return "\n".join(lines)


def strip_owner_notes(text: str) -> str:
    """Removes the owner notes from a draft.

    Args:
        text: The draft's markdown.

    Returns:
        The text without the "Owner notes" section and without any line that starts with the
        owner note marker.
    """
    kept: list[str] = []
    in_notes = False
    for line in text.splitlines():
        if line.strip() == NOTES_HEADING:
            in_notes = True
            continue
        if in_notes and line.startswith("#"):
            in_notes = False
        if in_notes or line.startswith(NOTE_MARKER):
            continue
        kept.append(line)
    # collapse the blank runs that the removed lines leave behind
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).rstrip("\n") + "\n"


def check_draft(text: str, done_ids: Collection[str]) -> list[str]:
    """Checks that a draft can be published.

    Args:
        text: The draft's markdown.
        done_ids: The ids of the problems that are already done. A draft may reuse one only on an
            entry whose 'Carried from' ends with that same id.

    Returns:
        One message for each fault found, or an empty list when the draft is sound. A fault is a
        repeated P id, a done P id on an entry that does not carry that problem forward, a
        missing or invalid Status, an After that names a P id the draft lacks, or a P heading
        outside the Open section.
    """
    entries = [entry for entry in split_entries(text) if entry.is_problem]
    present = {entry.id for entry in entries}
    errors: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.id in seen:
            errors.append(f"{entry.id} appears more than once")
        seen.add(entry.id)
        # a carried entry is the same problem coming back, so it may keep its done id
        carried_id = entry.fields.get("Carried from", "").split()[-1:]
        if entry.id in done_ids and carried_id != [entry.id]:
            errors.append(f"{entry.id} reuses the id of a problem that is done")
        status = entry.fields.get("Status", "")
        if status not in VALID_STATUSES:
            errors.append(
                f"{entry.id} has Status '{status}', which is not one of "
                f"{', '.join(VALID_STATUSES)}"
            )
        for name in re.findall(r"[^\s,]+", entry.fields.get("After", "")):
            if name not in present:
                errors.append(f"{entry.id} comes After {name}, which is not in the draft")
        if entry.section != "Open":
            errors.append(f"{entry.id} is under '{entry.section}' and must be under Open")
    return errors


def _render_problem(problem: DraftProblem) -> list[str]:
    """Returns the lines of one problem's entry, ending with a blank line."""
    grade = problem.grade
    if problem.breaks:
        grade += f" (Breaks: {problem.breaks})"
    lines = [f"### {problem.id}. {problem.title}", "",
             f"Status: {problem.status}",
             f"Sources: {', '.join(problem.sources)}",
             f"Grade: {grade}",
             f"Subject: {problem.subject}"]
    if problem.carried_from:
        lines.append(f"Carried from: {problem.carried_from}")
    if problem.after:
        lines.append(f"After: {', '.join(problem.after)}")
    lines += ["", problem.what_is_wrong.strip(), ""]
    for note in problem.notes:
        # join the note's lines into one line
        lines += [f"{NOTE_MARKER} {' '.join(note.split())}", ""]
    if problem.evidence:
        lines += ["Evidence:", ""]
        for citation in problem.evidence:
            lines.append(f"- {citation.file}:{citation.line}-{citation.end_line}: {citation.shows}")
            # indent each line of the quote
            lines += [f"      {quoted}" for quoted in citation.quote.splitlines()]
        lines.append("")
    if problem.simpler_option:
        lines += ["Simpler option:", "", problem.simpler_option.strip(), ""]
    if problem.done_when:
        lines += ["Done when:", ""] + [f"- {item}" for item in problem.done_when] + [""]
    if problem.tests:
        lines += ["Tests:", ""] + [f"- {name}: {how}" for name, how in problem.tests] + [""]
    return lines
