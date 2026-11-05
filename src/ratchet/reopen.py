"""The module contains the function behind `ratchet reopen`, which marks a problem on the newest
problem list as open again.

It changes the list's ledger and commits that change in the records repository. It never calls
an agent.
"""

from __future__ import annotations

from datetime import datetime

from ratchet.problem_history import TIME_FORMAT
from ratchet.project import Project, ProjectError, read_markdown
from ratchet.records import commit_records, has_records
from ratchet.refactor.ledger import (
    ProblemEvent,
    ProblemRecord,
    ProblemStatus,
    load_ledger,
    save_ledger,
)
from ratchet.refactor.problem_list import find_problem_lists, split_entries


def reopen_problem(project: Project, problem_id: str, reason: str) -> str:
    """Sets a problem on the newest list back to open and records why.

    The problem's status becomes open and its sendbacks become 0. The ledger gains a 'reopened'
    event by the owner, with the reason as its note, and the change is committed in the records
    repository with the message "reopen P7: reason".

    Args:
        project: The project, as read from its .ratchet folder.
        problem_id: The problem's id, such as "P7".
        reason: Why the problem is being reopened.

    Returns:
        The name of the list that was changed.

    Raises:
        ProjectError: The project has no records repository, the reason is empty, the folder
            holds no problem list, or the id is not a problem on the newest list.
        GitError: The commit failed.
    """
    if not has_records(project.root):
        raise ProjectError("this project has not been set up yet. Run `ratchet init` first.")
    reason = reason.strip()
    if not reason:
        raise ProjectError("a reason is needed, as in `ratchet reopen P7 \"still repeated\"`")
    found = find_problem_lists(project)
    if not found:
        raise ProjectError(f"{project.folder} has no problem list. Run `ratchet audit` to make one.")
    path = found[-1][1]
    name = path.stem

    # look the id up in the newest list
    entries = {entry.id: entry for entry in split_entries(read_markdown(project.folder, path.name))}
    entry = entries.get(problem_id)
    if entry is None:
        raise ProjectError(f"{name} does not hold {problem_id}")
    if not entry.is_problem:
        raise ProjectError(
            f'{problem_id} is not a problem, it is under "{entry.section}" in {name}')

    # reset the problem and record the event
    ledger_path = project.state_dir / name / "ledger.json"
    ledger = load_ledger(ledger_path, name)
    record = ledger.problems.setdefault(problem_id, ProblemRecord())
    record.status = ProblemStatus.OPEN
    record.sendbacks = 0
    record.events.append(ProblemEvent(
        when=datetime.now().strftime(TIME_FORMAT), kind="reopened", by="owner", note=reason))
    save_ledger(ledger, ledger_path)
    commit_records(project.root, f"reopen {problem_id}: {reason}")
    return name
