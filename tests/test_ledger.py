"""Checks the ledger: recording jobs, each problem's status, refusing plans, and the planner's
view of it."""

from __future__ import annotations

from pathlib import Path

from ratchet.refactor.agent_answers import Plan
from ratchet.refactor.job_graph import JobOutcome
from ratchet.refactor.ledger import (
    Ledger,
    ProblemStatus,
    find_plan_refusal,
    format_planner_view,
    load_ledger,
    record_job,
    save_ledger,
)
from ratchet.refactor.settings import Caps
from scripted_agents import plan

LIST = "problems-20261026-09h00m"
FILE = f".ratchet/{LIST}.md"


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
        return find_plan_refusal(ledger, Plan.model_validate(plan(problems=problems)), known)

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


def test_the_planner_sees_the_statuses_and_only_the_last_ten_jobs() -> None:
    """Proves the view lists each touched problem's status and the latest ten jobs, and leaves
    out the older ones."""
    ledger = Ledger(problem_list=LIST)
    for number in range(12):
        record_job(ledger, finished(JobOutcome.BLOCKED, problems=[f"P{number}"],
                                    step_title=f"step {number}"), f"job-{number}", 1.0, Caps())

    view = format_planner_view(ledger, FILE)

    assert f"The problem list is `{FILE}`. Jobs run on it so far: 12." in view
    assert "- P0: open, sent back 1 time\n" in view
    assert "## The last 10 jobs" in view
    assert '"step 2"' in view and '"step 1"' not in view
    assert "cost_usd" not in view and "A request" not in view


def test_an_empty_ledger_says_everything_is_open_and_carries_the_focus() -> None:
    """Proves the first job's view says no problem is touched yet, and passes on the focus."""
    view = format_planner_view(Ledger(problem_list=LIST), FILE, focus="P2")

    assert "None yet. Every problem on the list is open." in view
    assert "Plan a step for P2 next, if the rules for ordering allow it." in view
