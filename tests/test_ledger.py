"""Checks the ledger: recording jobs, each problem's status, and refusing plans."""

from __future__ import annotations

from pathlib import Path

from ratchet.refactor.agent_answers import Plan
from ratchet.refactor.job_graph import JobOutcome
from ratchet.refactor.ledger import (
    Ledger,
    ProblemEvent,
    ProblemRecord,
    ProblemStatus,
    load_ledger,
    record_job,
    save_ledger,
)
from ratchet.refactor.settings import Caps
from scripted_agents import plan

LIST = "problems-20261026-09h00m"


def finished(outcome: JobOutcome, **plan_changes: object) -> dict:
    """Returns a finished job's data with the given outcome and plan."""
    data = {"plan": plan(**plan_changes), "outcome": outcome, "reason": "", "attempt": 1}
    if outcome == JobOutcome.COMMITTED:
        data["commit"] = "abc1234"
    else:
        data["reason"] = f"it was {outcome}"
    return data


def test_a_committed_job_finishes_only_what_it_completes() -> None:
    """Proves a commit closes the problems in 'completes', and leaves others it advanced open."""
    ledger = Ledger(problem_list=LIST)

    record = record_job(ledger, finished(JobOutcome.COMMITTED, problems=["P1", "P2"],
                                         completes=["P1"]), "job-001", 1.25, Caps())

    assert (record.id, record.commit, record.cost_usd) == ("J01", "abc1234", 1.25)
    assert ledger.status_of("P1") == ProblemStatus.DONE
    assert ledger.status_of("P2") == ProblemStatus.OPEN
    assert ledger.status_of("P3") == ProblemStatus.OPEN


def test_an_abandoned_job_sets_aside_every_problem_it_worked_on() -> None:
    """Proves a job that reached a cap sets aside all of its problems."""
    ledger = Ledger(problem_list=LIST)

    record_job(ledger, finished(JobOutcome.ABANDONED, problems=["P1", "P2"]), "job-001", 1.0, Caps())

    assert ledger.status_of("P1") == ledger.status_of("P2") == ProblemStatus.ABANDONED


def test_a_problem_sent_back_twice_is_set_aside() -> None:
    """Proves blocked and replanned jobs count against their problems up to the cap."""
    ledger = Ledger(problem_list=LIST)

    record_job(ledger, finished(JobOutcome.BLOCKED), "job-001", 1.0, Caps())
    assert (ledger.status_of("P1"), ledger.problems["P1"].sendbacks) == (ProblemStatus.OPEN, 1)

    record_job(ledger, finished(JobOutcome.REPLAN), "job-002", 1.0, Caps())
    assert (ledger.status_of("P1"), ledger.problems["P1"].sendbacks) == (ProblemStatus.ABANDONED, 2)
    assert [record.id for record in ledger.jobs] == ["J01", "J02"]


def test_a_plan_is_refused_for_unknown_or_only_closed_problems() -> None:
    """Proves a plan must name a problem that is on the list and still open."""
    ledger = Ledger(problem_list=LIST)
    record_job(ledger, finished(JobOutcome.COMMITTED), "job-001", 1.0, Caps())
    known = ["P1", "P2"]

    def refusal(problems: list[str]) -> str:
        return ledger.find_plan_refusal(Plan.model_validate(plan(problems=problems)), known)

    assert refusal(["P9"]) == "it names problems that the list does not hold: P9"
    assert refusal(["P1"]) == "every problem it names is already closed: P1"
    assert refusal([]) == "it names no problem"
    assert refusal(["P1", "P2"]) == ""


def test_the_ledger_comes_back_as_it_was_saved(tmp_path: Path) -> None:
    """Proves a saved ledger loads unchanged, and a missing one loads empty."""
    path = tmp_path / LIST / "ledger.json"
    ledger = load_ledger(path, LIST)
    assert ledger == Ledger(problem_list=LIST)

    record_job(ledger, finished(JobOutcome.BLOCKED), "job-001", 1.0, Caps())
    save_ledger(ledger, path)

    assert load_ledger(path, LIST) == ledger



def kinds(ledger: Ledger, problem_id: str) -> list[str]:
    """Returns the kinds of the events recorded for a problem, in order."""
    return [event.kind for event in ledger.problems[problem_id].events]


def test_an_old_ledger_without_events_or_carried_from_loads(tmp_path: Path) -> None:
    """Proves a ledger saved before events existed still loads, with empty events."""
    path = tmp_path / "ledger.json"
    path.write_text(
        '{"problem_list": "' + LIST + '", "jobs": [], '
        '"problems": {"P1": {"status": "done", "sendbacks": 1}}}',
        encoding="utf-8",
    )

    ledger = load_ledger(path, LIST)

    assert ledger.status_of("P1") == ProblemStatus.DONE
    assert ledger.problems["P1"].carried_from == ""
    assert ledger.problems["P1"].events == []


def test_events_survive_a_save_and_load(tmp_path: Path) -> None:
    """Proves recorded events are written to the file and read back."""
    ledger = Ledger(problem_list=LIST)
    record_job(ledger, finished(JobOutcome.COMMITTED), "job-001", 1.0, Caps())
    path = tmp_path / "ledger.json"

    save_ledger(ledger, path)

    assert load_ledger(path, LIST).problems["P1"].events == ledger.problems["P1"].events
    assert isinstance(ledger.problems["P1"].events[0], ProblemEvent)


def test_a_committed_job_records_done_or_committed_once_for_each_problem() -> None:
    """Proves a completed problem gets a done event, and an advanced one a committed event."""
    ledger = Ledger(problem_list=LIST)

    record_job(ledger, finished(JobOutcome.COMMITTED, problems=["P1", "P2"], completes=["P1"]),
               "job-001", 1.0, Caps())

    assert kinds(ledger, "P1") == ["done"]
    assert kinds(ledger, "P2") == ["committed"]
    event = ledger.problems["P1"].events[0]
    assert (event.by, event.job, event.commit) == ("J01", "J01", "abc1234")


def test_an_abandoned_job_records_abandoned() -> None:
    """Proves an abandoned job gives each of its problems one abandoned event with the reason."""
    ledger = Ledger(problem_list=LIST)

    record_job(ledger, finished(JobOutcome.ABANDONED, problems=["P1", "P2"]), "job-001", 1.0, Caps())

    assert kinds(ledger, "P1") == kinds(ledger, "P2") == ["abandoned"]
    assert ledger.problems["P1"].events[0].note == "it was abandoned"


def test_a_sent_back_job_records_sent_back_and_abandoned_at_the_cap() -> None:
    """Proves sent_back is recorded each time, and abandoned as well when the cap is reached."""
    ledger = Ledger(problem_list=LIST)

    record_job(ledger, finished(JobOutcome.BLOCKED), "job-001", 1.0, Caps())
    assert kinds(ledger, "P1") == ["sent_back"]

    record_job(ledger, finished(JobOutcome.REPLAN), "job-002", 1.0, Caps())
    assert kinds(ledger, "P1") == ["sent_back", "sent_back", "abandoned"]
    assert ledger.problems["P1"].events[-1].job == "J02"


def test_the_next_job_number_counts_jobs_that_left_only_a_job_started_event() -> None:
    """Proves the next job number follows both the recorded jobs and the job_started events, and
    leaves out a carried event that names another list's job."""
    assert Ledger(problem_list=LIST).next_job_number() == 1
    ledger = Ledger(problem_list=LIST)
    record_job(ledger, finished(JobOutcome.COMMITTED), "job-001", 1.0, Caps())
    ledger.problems["P2"] = ProblemRecord(events=[
        ProblemEvent(when="2026-10-26 09:00", kind="carried", by="J09", job="J09",
                     list="problems-20261020-09h00m"),
        ProblemEvent(when="2026-10-26 09:05", kind="job_started", by="J02", job="J02"),
    ])

    assert ledger.next_job_number() == 3
