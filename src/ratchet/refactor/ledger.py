"""Keeps the ledger of one problem list: every job that worked on it, and each problem's status.

Each problem list has its own ledger, in .ratchet/state/<list name>/ledger.json. A new list
therefore starts with an empty ledger, and a run on the same list carries on where the last one
stopped. The planner is sent a short view of the ledger: the status of every problem that a job
has touched, and the last few jobs.
"""

from __future__ import annotations

import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel

from ratchet.refactor.agent_answers import Plan
from ratchet.refactor.job_graph import JobOutcome
from ratchet.refactor.settings import Caps

# How many of the latest jobs the planner sees.
RECENT_JOBS = 10


class ProblemStatus(StrEnum):
    """Names where a problem stands."""

    OPEN = "open"
    DONE = "done"
    # Set aside after a job on it was abandoned, or after it was sent back too often.
    ABANDONED = "abandoned"


class ProblemRecord(BaseModel):
    """Holds one problem's status, and how many jobs on it came back blocked or replanned."""

    status: ProblemStatus = ProblemStatus.OPEN
    sendbacks: int = 0


class JobRecord(BaseModel):
    """Holds what one finished job did and how it ended."""

    # A number in the order the jobs ran, such as "J07", and the job's own name in the graph.
    id: str
    slug: str
    title: str
    problems: list[str]
    completes: list[str]
    outcome: JobOutcome
    # Why the job did not end in a commit, which is "" for a committed job.
    reason: str = ""
    commit: str = ""
    attempts: int
    comment_fixes: int = 0
    notes: list[str] = []
    cost_usd: float
    # When the job ended, as "2026-10-26 14:32".
    ended: str


class Ledger(BaseModel):
    """Holds every job on one problem list and the status of each problem those jobs touched."""

    # The problem list's name, such as "problems-20261026-14h32m".
    problem_list: str
    jobs: list[JobRecord] = []
    problems: dict[str, ProblemRecord] = {}

    def status_of(self, problem_id: str) -> ProblemStatus:
        """Returns a problem's status, which is open when no job has touched it."""
        record = self.problems.get(problem_id)
        return record.status if record else ProblemStatus.OPEN


def load_ledger(path: Path, problem_list: str) -> Ledger:
    """Returns the ledger saved at 'path', or an empty one when there is none yet.

    Args:
        path: The ledger's file.
        problem_list: The name of the problem list that the ledger belongs to.

    Returns:
        The ledger.
    """
    if not path.is_file():
        return Ledger(problem_list=problem_list)
    return Ledger.model_validate_json(path.read_text(encoding="utf-8"))


def save_ledger(ledger: Ledger, path: Path) -> None:
    """Writes the ledger to 'path', replacing the file in one step so a crash cannot truncate it.

    Args:
        ledger: The ledger.
        path: The ledger's file. Its folder is made if it does not exist.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".json.tmp")
    temp.write_text(ledger.model_dump_json(indent=2), encoding="utf-8")
    temp.replace(path)


def record_job(ledger: Ledger, job_data: dict, slug: str, cost_usd: float, caps: Caps) -> JobRecord:
    """Adds a finished job to the ledger and updates the problems that it worked on.

    A committed job finishes the problems its plan says it completes. An abandoned job sets
    aside every problem it worked on. A blocked or replanned job counts a sendback against each
    of its problems, and a problem that reaches the cap on sendbacks is set aside.

    Args:
        ledger: The ledger to add to.
        job_data: The finished job's data, which holds its plan and its outcome.
        slug: The job's name in the graph.
        cost_usd: What the job spent, in dollars.
        caps: The refactor command's caps.

    Returns:
        The record that was added.
    """
    plan = Plan.model_validate(job_data["plan"])
    outcome = JobOutcome(job_data["outcome"])
    record = JobRecord(
        id=f"J{len(ledger.jobs) + 1:02d}",
        slug=slug,
        title=plan.step_title,
        problems=plan.problems,
        completes=plan.completes,
        outcome=outcome,
        reason=job_data.get("reason", ""),
        commit=job_data.get("commit", ""),
        attempts=job_data.get("attempt", 0),
        comment_fixes=job_data.get("comment_fixes", 0),
        notes=job_data.get("notes", []),
        cost_usd=round(cost_usd, 4),
        ended=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )
    ledger.jobs.append(record)
    for problem_id in plan.problems:
        problem = ledger.problems.setdefault(problem_id, ProblemRecord())
        if outcome == JobOutcome.COMMITTED and problem_id in plan.completes:
            problem.status = ProblemStatus.DONE
        elif outcome == JobOutcome.ABANDONED:
            problem.status = ProblemStatus.ABANDONED
        elif outcome in (JobOutcome.BLOCKED, JobOutcome.REPLAN):
            problem.sendbacks += 1
            if problem.sendbacks >= caps.sendbacks:
                problem.status = ProblemStatus.ABANDONED
    return record


def find_plan_refusal(ledger: Ledger, plan: Plan, known_ids: list[str]) -> str:
    """Returns why a plan may not go ahead, or "" when it may.

    A plan is refused when it names a problem that the list does not hold, or when every
    problem it names is already done or set aside.

    Args:
        ledger: The ledger of the problem list.
        plan: The planner's plan.
        known_ids: The ids that the problem list holds.

    Returns:
        The reason for refusing the plan, or "".
    """
    unknown = [problem_id for problem_id in plan.problems if problem_id not in known_ids]
    if unknown:
        return f"it names problems that the list does not hold: {', '.join(unknown)}"
    if not plan.problems:
        return "it names no problem"
    closed = [problem_id for problem_id in plan.problems
              if ledger.status_of(problem_id) != ProblemStatus.OPEN]
    if len(closed) == len(plan.problems):
        return f"every problem it names is already closed: {', '.join(closed)}"
    return ""


def format_planner_view(ledger: Ledger, problems_file: str, focus: str = "") -> str:
    """Returns the planner's prompt: the status of every touched problem, the latest jobs, and
    any request from the person running ratchet.

    Args:
        ledger: The ledger of the problem list.
        problems_file: The problem list's path from the project's folder.
        focus: A problem the person asks to be planned next, such as "P19", or "".

    Returns:
        The prompt as markdown.
    """
    count = len(ledger.jobs)
    lines = ["# The ledger", "",
             f"The problem list is `{problems_file}`. Jobs run on it so far: {count}.", "",
             "## The problems that jobs have worked on", ""]
    if not ledger.problems:
        lines.append("None yet. Every problem on the list is open.")
    for problem_id, problem in ledger.problems.items():
        times = "time" if problem.sendbacks == 1 else "times"
        sent_back = f", sent back {problem.sendbacks} {times}" if problem.sendbacks else ""
        lines.append(f"- {problem_id}: {problem.status}{sent_back}")
    recent = ledger.jobs[-RECENT_JOBS:]
    lines += ["", f"## The last {len(recent)} jobs", ""]
    if recent:
        shown = [record.model_dump(mode="json", exclude={"slug", "cost_usd"}) for record in recent]
        lines += ["```json", json.dumps(shown, indent=2), "```"]
    else:
        lines.append("None yet.")
    if focus:
        lines += ["", "# A request from the person running ratchet", "",
                  f"Plan a step for {focus} next, if the rules for ordering allow it."]
    return "\n".join(lines) + "\n"
