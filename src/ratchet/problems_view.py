"""The module contains the functions that show a project's problem lists and one problem's
history as plain text.

It reads the lists, their ledgers and the records repository, and never calls an agent. The
status of a problem comes from its list's ledger, and the grade comes from the 'Grade:' line in
the list.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from ratchet.problem_history import problem_history, publish_time
from ratchet.project import Project, ProjectError, read_markdown
from ratchet.refactor.ledger import ProblemEvent, load_ledger
from ratchet.refactor.problem_list import Entry, find_problem_lists, split_entries
from ratchet.refactor.progress_lines import count_of


def _lists_newest_first(project: Project) -> list[tuple[datetime, Path]]:
    """Returns every problem list, newest first.

    Args:
        project: The project, as read from its .ratchet folder.

    Returns:
        The time in each list's file name and the path of the list, newest first.

    Raises:
        ProjectError: The folder holds no problem list, or a list file is misnamed.
    """
    found = find_problem_lists(project)
    if not found:
        raise ProjectError(f"{project.folder} has no problem list. Run `ratchet audit` to make one.")
    return list(reversed(found))


def _status_text(project: Project, name: str, problem_id: str) -> str:
    """Returns a problem's status from its list's ledger.

    Args:
        project: The project, as read from its .ratchet folder.
        name: The list's name.
        problem_id: The problem's id.

    Returns:
        The ledger's status, except "reopened" when the problem is open and its latest event
        is a reopen.
    """
    ledger = load_ledger(project.state_dir / name / "ledger.json", name)
    status = ledger.status_of(problem_id).value
    record = ledger.problems.get(problem_id)
    if status == "open" and record and record.events and record.events[-1].kind == "reopened":
        return "reopened"
    return status


def _list_lines(project: Project, created: datetime, path: Path, heading: bool) -> list[str]:
    """Returns the lines that show one list.

    Args:
        project: The project, as read from its .ratchet folder.
        created: The time in the list's file name.
        path: The list's file.
        heading: Whether to start with a line naming the list and its publish time.

    Returns:
        A line for each problem with its id, status, grade and title, then a last line that
        counts the questions and the dropped entries.
    """
    entries = split_entries(read_markdown(project.folder, path.name))
    rows = [
        (entry.id, _status_text(project, path.stem, entry.id),
         entry.fields.get("Grade", "").split(" ")[0] or "-", entry.title)
        for entry in entries if entry.is_problem
    ]
    widths = [max((len(row[column]) for row in rows), default=0) for column in range(3)]
    lines = []
    if heading:
        lines.append(f"{path.stem}  {publish_time(project, path.stem, created)}")
    for problem_id, status, grade, title in rows:
        lines.append(f"{problem_id:<{widths[0]}}  {status:<{widths[1]}}  "
                     f"{grade:<{widths[2]}}  {title}")
    questions = sum(1 for entry in entries if entry.id.startswith("Q"))
    dropped = sum(1 for entry in entries if entry.id.startswith("D"))
    lines.append(f"{count_of(questions, 'question')}, "
                 f"{count_of(dropped, 'dropped entry', 'dropped entries')}")
    return lines


def show_lists(project: Project, every_list: bool) -> str:
    """Returns the text that shows the newest problem list, or every list when asked.

    Args:
        project: The project, as read from its .ratchet folder.
        every_list: True to show every list, oldest first, each under a heading line.

    Returns:
        The lines, joined by newlines.

    Raises:
        ProjectError: The folder holds no problem list, or a list file is misnamed.
    """
    newest_first = _lists_newest_first(project)
    if not every_list:
        created, path = newest_first[0]
        return "\n".join(_list_lines(project, created, path, heading=False))
    blocks = ["\n".join(_list_lines(project, created, path, heading=True))
              for created, path in reversed(newest_first)]
    return "\n\n".join(blocks)


def _find_problem(project: Project, problem_id: str) -> tuple[str, Entry]:
    """Returns the name of the newest list that holds an id, with the id's entry.

    Args:
        project: The project, as read from its .ratchet folder.
        problem_id: The problem's id, such as "P7".

    Returns:
        The name of the newest list that holds the id, together with that id's entry.

    Raises:
        ProjectError: No list holds the id, or the id is a question or a dropped entry.
    """
    for _, path in _lists_newest_first(project):
        for entry in split_entries(read_markdown(project.folder, path.name)):
            if entry.id != problem_id:
                continue
            if not entry.is_problem:
                raise ProjectError(
                    f'{problem_id} is not a problem, it is under "{entry.section}" in {path.stem}'
                )
            return path.stem, entry
    raise ProjectError(f"no problem list holds {problem_id}")


def show_problem(project: Project, problem_id: str) -> str:
    """Returns the full entry of a problem from the newest list that holds it.

    Args:
        project: The project, as read from its .ratchet folder.
        problem_id: The problem's id, such as "P7".

    Returns:
        A line naming the list, then the entry's text.

    Raises:
        ProjectError: No list holds the id, or the id is a question or a dropped entry.
    """
    name, entry = _find_problem(project, problem_id)
    return f"{name}\n\n{entry.text}"


def describe_event(event: ProblemEvent) -> str:
    """Returns the fixed wording for one event, such as "J03 committed 6918463, marked done".

    Args:
        event: The event.

    Returns:
        The wording, which depends on the event's kind.
    """
    note = f": {event.note}" if event.note else ""
    quoted = f': "{event.note}"' if event.note else ""
    actor = {"re-audit": "a re-audit", "audit": "an audit", "owner": "the owner"}.get(
        event.by, event.by
    )
    slug = f" ({event.note})" if event.note else ""
    if event.kind == "opened":
        return f"opened in {event.list}{slug}"
    if event.kind == "carried":
        return f"carried into {event.list}{slug}"
    if event.kind == "job_started":
        return f"{event.job} started"
    if event.kind == "committed":
        return f"{event.job} committed {event.commit}"
    if event.kind == "done":
        return f"{event.job} committed {event.commit}, marked done"
    if event.kind == "sent_back":
        return f"{event.job} sent back{note}"
    if event.kind == "abandoned":
        if event.job:
            return f"{event.job} abandoned{note}"
        return f"abandoned by {actor}{note}"
    if event.kind == "reopened":
        return f"reopened by {actor}{quoted}"
    return f"{event.kind} by {actor}{note}"


def show_history(project: Project, problem_id: str) -> str:
    """Returns one problem's history as a heading line and one line for each event.

    Args:
        project: The project, as read from its .ratchet folder.
        problem_id: The problem's id, such as "P7".

    Returns:
        The id and title, then each event's time and wording, oldest first.

    Raises:
        ProjectError: No list holds the id, or the id is a question or a dropped entry.
    """
    name, entry = _find_problem(project, problem_id)
    lines = [f"{problem_id}  {entry.title}"]
    for event in problem_history(project, name, problem_id):
        lines.append(f"{event.when}  {describe_event(event)}")
    return "\n".join(lines)
