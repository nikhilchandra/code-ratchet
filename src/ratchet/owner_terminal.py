"""The module contains the functions that put an audit's questions and its draft in front of the
owner in a terminal.

The input function, the output function and the editor are passed in, so tests can replace them.
Nothing here calls an agent.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Collection, Iterable
from pathlib import Path
from typing import TextIO

from ratchet.audit.list_markdown import Draft, DraftQuestion, check_draft

# The answers that leave a question unanswered.
SKIP_ANSWERS = ("", "don't know", "dont know", "skip")


def is_interactive(stdin: TextIO | None = None, stdout: TextIO | None = None) -> bool:
    """Tells whether the owner can be asked questions.

    Args:
        stdin: The input stream, which is sys.stdin when omitted.
        stdout: The output stream, which is sys.stdout when omitted.

    Returns:
        True when both streams are a terminal.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    return stdin.isatty() and stdout.isatty()


def ask_questions(
    questions: list[DraftQuestion],
    ask: Callable[[str], str] = input,
    show: Callable[[str], None] = print,
) -> dict[str, str]:
    """Shows each question and reads the owner's answer.

    Args:
        questions: The questions for the owner.
        ask: Reads one line, given the prompt to show.
        show: Writes one line to the owner.

    Returns:
        The answer to each question that was answered, keyed by the question's id. A question
        whose answer is empty or "don't know" is left out.
    """
    answers: dict[str, str] = {}
    for question in questions:
        show("")
        show(f"{question.id}. {question.title}")
        if question.text:
            show(question.text)
        answer = ask("Answer (empty to skip): ").strip()
        if answer.lower() not in SKIP_ANSWERS:
            answers[question.id] = answer
    return answers


def draft_summary(draft: Draft) -> str:
    """Builds the summary of a draft that the owner sees before approving.

    Args:
        draft: The draft.

    Returns:
        The counts of problems by status and by grade, a line for each problem with its status,
        grade and sources, and then the owner notes.
    """
    lines = [
        f"{len(draft.problems)} problems, {len(draft.questions)} questions, "
        f"{len(draft.dropped)} dropped, {len(draft.bugs)} bugs",
        "By status: " + _counts(problem.status for problem in draft.problems),
        "By grade: " + _counts(problem.grade for problem in draft.problems),
        "",
    ]
    for problem in draft.problems:
        lines.append(
            f"{problem.id}  {problem.status}  {problem.grade}  "
            f"[{', '.join(problem.sources)}]  {problem.title}"
        )
    notes = draft.notes + [note for problem in draft.problems for note in problem.notes]
    if notes:
        lines.append("")
        lines.append("Owner notes:")
        lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines)


def _counts(values: Iterable[str]) -> str:
    """Returns the values counted and joined, such as "open 2, new 1", or "none"."""
    counted = Counter(values)
    return ", ".join(f"{name} {count}" for name, count in sorted(counted.items())) or "none"


def default_editor_command() -> str:
    """Returns the editor to open a draft with.

    Returns:
        The VISUAL variable, else the EDITOR variable, else notepad on Windows and vi elsewhere.
    """
    default = "notepad" if sys.platform == "win32" else "vi"
    return os.environ.get("VISUAL") or os.environ.get("EDITOR") or default


def open_editor(path: Path) -> None:
    """Opens a file in the owner's editor and waits until it closes.

    Args:
        path: The file to edit.
    """
    subprocess.run(f'{default_editor_command()} "{path}"', shell=True, check=False)


def review_draft(
    draft: Draft,
    draft_path: Path,
    done_ids: Collection[str],
    head_moved: Callable[[], bool],
    ask: Callable[[str], str] = input,
    show: Callable[[str], None] = print,
    edit: Callable[[Path], None] = open_editor,
) -> dict | None:
    """Shows the draft's summary and asks the owner what to do with it.

    The owner may approve, edit, discard or decide later. Approving checks the draft file with
    check_draft and checks that HEAD has not moved, and a failed check is shown and the owner is
    asked again. Editing opens the draft file and then checks it before asking again.

    Args:
        draft: The draft, for the summary.
        draft_path: The draft's markdown file, which the editor changes.
        done_ids: The ids of the problems that are already done.
        head_moved: Tells whether the project's HEAD has moved since the audit started.
        ask: Reads one line, given the prompt to show.
        show: Writes one line to the owner.
        edit: Opens a file for the owner to change.

    Returns:
        {"decision": "approve"} or {"decision": "discard"} to resume the job, or None when the
        owner chose to decide later.
    """
    show(draft_summary(draft))
    while True:
        choice = ask("[a]pprove, [e]dit, [d]iscard, [l]ater: ").strip().lower()
        if choice in ("a", "approve"):
            errors = check_draft(draft_path.read_text(encoding="utf-8"), done_ids)
            if head_moved():
                errors.append("HEAD has moved since the audit started, so the list is out of date")
            if not errors:
                return {"decision": "approve"}
            _show_errors(errors, show)
        elif choice in ("e", "edit"):
            edit(draft_path)
            _show_errors(check_draft(draft_path.read_text(encoding="utf-8"), done_ids), show)
        elif choice in ("d", "discard"):
            return {"decision": "discard"}
        elif choice in ("l", "later"):
            return None
        else:
            show("Please answer a, e, d or l.")


def _show_errors(errors: list[str], show: Callable[[str], None]) -> None:
    """Shows each fault found in a draft."""
    for error in errors:
        show(f"problem with the draft: {error}")
