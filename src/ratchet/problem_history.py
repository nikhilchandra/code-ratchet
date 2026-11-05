"""The module contains the function that reads one problem's history across problem lists.

A problem that a re-audit carries into a newer list keeps a 'carried_from' note in the newer
list's ledger. The history follows those notes back through the older ledgers, so that the
problem reads as one timeline. The module only reads files and never calls an agent.
"""

from __future__ import annotations

from datetime import datetime

from ratchet.project import Project, ProjectError, read_markdown
from ratchet.records import has_records, records_repo
from ratchet.refactor.job_outcome import JobOutcome
from ratchet.refactor.ledger import Ledger, ProblemEvent, load_ledger
from ratchet.refactor.problem_list import find_problem_lists, split_entries

TIME_FORMAT = "%Y-%m-%d %H:%M"


def publish_time(project: Project, name: str, created: datetime) -> str:
    """Returns when a problem list was published.

    Args:
        project: The project, as read from its .ratchet folder.
        name: The list's name, such as "problems-20261102-14h05m".
        created: The time in the list's file name.

    Returns:
        The time of the records commit that added the list's file, or else the time in the
        file's name, as "2026-11-02 14:05".
    """
    if has_records(project.root):
        output = records_repo(project.root).run(
            "log", "--diff-filter=A", f"--date=format:{TIME_FORMAT}", "--format=%cd",
            "--", f"{name}.md", check=False,
        )
        lines = output.splitlines()
        if lines:
            # the last line is the oldest commit
            return lines[-1].strip()
    return created.strftime(TIME_FORMAT)


def _events_from_jobs(ledger: Ledger, problem_id: str) -> list[ProblemEvent]:
    """Makes events for a problem from the jobs that named it, for a ledger with no events.

    Args:
        ledger: The ledger of the problem's list.
        problem_id: The problem's id.

    Returns:
        One event for each job that named the problem, at the job's 'ended' time.
    """
    events = []
    for job in ledger.jobs:
        if problem_id not in job.problems:
            continue
        if job.outcome == JobOutcome.COMMITTED:
            kind = "done" if problem_id in job.completes else "committed"
        elif job.outcome == JobOutcome.ABANDONED:
            kind = "abandoned"
        else:
            kind = "sent_back"
        events.append(ProblemEvent(when=job.ended, kind=kind, by=job.id, job=job.id,
                                   commit=job.commit, note=job.reason))
    return events


def problem_history(project: Project, list_name: str, problem_id: str) -> list[ProblemEvent]:
    """Returns one problem's events, following 'carried_from' back through the older lists.

    A ledger with no events for the problem gets events made from its jobs, and an 'opened'
    event at the time its list was published. An event with no time takes the 'ended' time of
    its job, or else the time its list was published.

    Args:
        project: The project, as read from its .ratchet folder.
        list_name: The name of the newest list to start from, such as "problems-20261102-14h05m".
        problem_id: The problem's id in that list, such as "P7".

    Returns:
        The events, oldest list first and in recorded order within each list. Each event's
        'list' names the list it came from. The result is empty when no list holds the problem
        and no ledger has an event for it.

    Raises:
        ProjectError: The list does not exist, or a file that starts with "problems" is
            misnamed.
    """
    lists = {path.stem: (created, path) for created, path in find_problem_lists(project)}
    if list_name not in lists:
        raise ProjectError(f"{project.folder} has no problem list named {list_name}")

    chain: list[list[ProblemEvent]] = []
    visited: set[str] = set()
    current: tuple[str, str] | None = (list_name, problem_id)
    while current is not None and current[0] in lists and current[0] not in visited:
        name, wanted = current
        visited.add(name)
        created, path = lists[name]
        published = publish_time(project, name, created)
        ledger = load_ledger(project.state_dir / name / "ledger.json", name)
        record = ledger.problems.get(wanted)
        jobs = {job.id: job for job in ledger.jobs}

        if record is not None and record.events:
            source = record.events
        else:
            source = _events_from_jobs(ledger, wanted)
            text = read_markdown(project.folder, path.name)
            held = any(entry.id == wanted for entry in split_entries(text))
            if held:
                source = [ProblemEvent(when=published, kind="opened", by="audit"), *source]

        events = []
        for event in source:
            when = event.when
            if not when:
                job = jobs.get(event.job)
                when = job.ended if job else published
            events.append(event.model_copy(update={"when": when, "list": name}))
        chain.append(events)

        # follow the note to the older list and problem
        carried = record.carried_from if record else ""
        older = carried.split()
        current = (older[0], older[1]) if len(older) == 2 else None

    return [event for events in reversed(chain) for event in events]
