"""Checks a problem's history across problem lists on scratch projects."""

from __future__ import annotations

from pathlib import Path

import pytest

from ratchet.problem_history import problem_history
from ratchet.project import Project, ProjectError, load_project
from ratchet.records import commit_records, init_records
from ratchet.refactor.job_graph import JobOutcome
from ratchet.refactor.ledger import (
    JobRecord, Ledger, ProblemEvent, ProblemRecord, record_job, save_ledger,
)
from ratchet.refactor.settings import Caps
from scripted_agents import plan

FIRST = "problems-20261102-14h05m"
SECOND = "problems-20261105-10h11m"
THIRD = "problems-20261110-09h00m"


def make_project(root: Path, *names: str) -> Project:
    """Writes a minimal .ratchet folder with one list for each name and loads it."""
    folder = root / ".ratchet"
    folder.mkdir()
    (folder / "config.toml").write_text('test_command = ["pytest"]\neditable = ["src/**"]\n',
                                        encoding="utf-8")
    (folder / "rules.md").write_text("rules\n", encoding="utf-8")
    for name in names:
        (folder / f"{name}.md").write_text("## P7. strip handling written twice\n\ntext\n",
                                           encoding="utf-8")
    return load_project(root, commands=[])


def save(project: Project, name: str, ledger: Ledger) -> None:
    """Saves a ledger in its list's state folder."""
    save_ledger(ledger, project.state_dir / name / "ledger.json")


def event(when: str, kind: str, by: str = "audit", job: str = "") -> ProblemEvent:
    """Returns an event."""
    return ProblemEvent(when=when, kind=kind, by=by, job=job)


def test_one_ledger_gives_its_events_in_order(tmp_path: Path) -> None:
    """Proves a list with no carried_from gives its own events, each naming its list."""
    project = make_project(tmp_path, FIRST)
    save(project, FIRST, Ledger(problem_list=FIRST, problems={"P7": ProblemRecord(events=[
        event("2026-11-02 14:05", "opened"), event("2026-11-02 16:20", "done", "J03", "J03"),
    ])}))
    history = problem_history(project, FIRST, "P7")
    assert [(e.kind, e.list) for e in history] == [("opened", FIRST), ("done", FIRST)]


def test_a_chain_over_three_lists_reads_oldest_first(tmp_path: Path) -> None:
    """Proves carried_from is followed back through every older ledger."""
    project = make_project(tmp_path, FIRST, SECOND, THIRD)
    save(project, FIRST, Ledger(problem_list=FIRST, problems={"P7": ProblemRecord(events=[
        event("2026-11-02 14:05", "opened")])}))
    save(project, SECOND, Ledger(problem_list=SECOND, problems={"P7": ProblemRecord(
        carried_from=f"{FIRST} P7",
        events=[event("2026-11-05 10:11", "reopened", "re-audit")])}))
    save(project, THIRD, Ledger(problem_list=THIRD, problems={"P7": ProblemRecord(
        carried_from=f"{SECOND} P7",
        events=[event("2026-11-10 09:00", "carried", "re-audit")])}))
    history = problem_history(project, THIRD, "P7")
    assert [e.kind for e in history] == ["opened", "reopened", "carried"]
    assert [e.list for e in history] == [FIRST, SECOND, THIRD]


def test_the_id_in_carried_from_is_the_one_looked_up(tmp_path: Path) -> None:
    """Proves a problem that had another id in the older list is found by that id."""
    project = make_project(tmp_path, FIRST, SECOND)
    save(project, FIRST, Ledger(problem_list=FIRST, problems={"P3": ProblemRecord(events=[
        event("2026-11-02 14:05", "opened")])}))
    save(project, SECOND, Ledger(problem_list=SECOND, problems={"P7": ProblemRecord(
        carried_from=f"{FIRST} P3", events=[event("2026-11-05 10:11", "carried")])}))
    assert [e.list for e in problem_history(project, SECOND, "P7")] == [FIRST, SECOND]


def test_an_old_ledger_without_events_gets_events_from_its_jobs(tmp_path: Path) -> None:
    """Proves jobs become events at their ended time, after an opened event at the list's time."""
    project = make_project(tmp_path, FIRST)
    ledger = Ledger(problem_list=FIRST)
    data = {"plan": plan(problems=["P7"], completes=["P7"]), "outcome": JobOutcome.COMMITTED,
            "commit": "6918463"}
    record_job(ledger, data, "job-1", 1.0, Caps())
    ledger.jobs[0].ended = "2026-11-02 16:20"
    ledger.problems.clear()
    save(project, FIRST, ledger)
    history = problem_history(project, FIRST, "P7")
    assert [(e.when, e.kind) for e in history] == [
        ("2026-11-02 14:05", "opened"), ("2026-11-02 16:20", "done")]
    assert history[1].commit == "6918463"
    assert history[1].job == "J01"


def old_ledger() -> Ledger:
    """Returns a ledger from before problem events, with one job J01 that completed P7."""
    ledger = Ledger(problem_list=FIRST)
    data = {"plan": plan(problems=["P7"], completes=["P7"]), "outcome": JobOutcome.COMMITTED,
            "commit": "6918463"}
    record_job(ledger, data, "job-1", 1.0, Caps())
    ledger.jobs[0].ended = "2026-11-02 16:20"
    ledger.problems.clear()
    return ledger


def test_an_old_ledger_keeps_its_jobs_after_a_new_event(tmp_path: Path) -> None:
    """Proves an owner's reopen on an old ledger keeps the opened event and the job's event."""
    project = make_project(tmp_path, FIRST)
    ledger = old_ledger()
    ledger.problems["P7"] = ProblemRecord(events=[event("2026-11-03 09:00", "reopened", "owner")])
    save(project, FIRST, ledger)
    history = problem_history(project, FIRST, "P7")
    assert [(e.when, e.kind) for e in history] == [
        ("2026-11-02 14:05", "opened"), ("2026-11-02 16:20", "done"),
        ("2026-11-03 09:00", "reopened")]
    assert history[1].job == "J01"
    assert all(e.list == FIRST for e in history)


def test_an_old_ledger_keeps_its_jobs_after_a_new_job_starts(tmp_path: Path) -> None:
    """Proves a job_started for a job with no record yet does not hide the older job."""
    project = make_project(tmp_path, FIRST)
    ledger = old_ledger()
    ledger.problems["P7"] = ProblemRecord(events=[
        event("2026-11-03 09:00", "job_started", "J02", "J02")])
    save(project, FIRST, ledger)
    history = problem_history(project, FIRST, "P7")
    assert [(e.when, e.kind, e.job) for e in history] == [
        ("2026-11-02 14:05", "opened", ""), ("2026-11-02 16:20", "done", "J01"),
        ("2026-11-03 09:00", "job_started", "J02")]


def test_a_job_with_events_is_not_made_twice(tmp_path: Path) -> None:
    """Proves a job with its own events gives them once, beside an older job that has none."""
    project = make_project(tmp_path, FIRST)
    ledger = old_ledger()
    data = {"plan": plan(problems=["P7"], completes=[]), "outcome": JobOutcome.COMMITTED,
            "commit": "a41c0de"}
    record_job(ledger, data, "job-2", 1.0, Caps())
    ledger.jobs[1].ended = "2026-11-03 09:00"
    ledger.problems["P7"].events[0].when = "2026-11-03 09:00"
    save(project, FIRST, ledger)
    history = problem_history(project, FIRST, "P7")
    assert [(e.when, e.kind, e.job) for e in history] == [
        ("2026-11-02 14:05", "opened", ""), ("2026-11-02 16:20", "done", "J01"),
        ("2026-11-03 09:00", "committed", "J02")]


def test_the_publish_time_is_the_records_commit_that_added_the_list(tmp_path: Path) -> None:
    """Proves the opened event takes the time of the records commit when there is one."""
    project = make_project(tmp_path, FIRST)
    init_records(tmp_path)
    assert commit_records(tmp_path, "publish")
    history = problem_history(project, FIRST, "P7")
    assert history[0].kind == "opened"
    assert history[0].when != "2026-11-02 14:05"


def test_an_event_without_a_time_takes_its_jobs_time_or_the_publish_time(tmp_path: Path) -> None:
    """Proves the two fallbacks for an empty 'when'."""
    project = make_project(tmp_path, FIRST)
    ledger = Ledger(problem_list=FIRST, problems={"P7": ProblemRecord(events=[
        event("", "committed", "J01", "J01"), event("", "reopened", "owner")])})
    ledger.jobs.append(JobRecord(
        id="J01", slug="J01", title="t", problems=["P7"], completes=[],
        outcome=JobOutcome.COMMITTED, attempts=1, cost_usd=0.0, ended="2026-11-03 08:00"))
    save(project, FIRST, ledger)
    history = problem_history(project, FIRST, "P7")
    assert [e.when for e in history] == ["2026-11-03 08:00", "2026-11-02 14:05"]


def test_an_id_with_no_record_gives_no_history(tmp_path: Path) -> None:
    """Proves an id that no list holds and no ledger records gives an empty timeline."""
    project = make_project(tmp_path, FIRST)
    assert problem_history(project, FIRST, "P99") == []


def test_an_unknown_list_is_refused(tmp_path: Path) -> None:
    """Proves a list name that does not exist raises."""
    project = make_project(tmp_path, FIRST)
    with pytest.raises(ProjectError, match="no problem list named"):
        problem_history(project, "problems-20200101-00h00m", "P7")
