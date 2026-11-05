"""Builds the graph that carries one refactor job, from the planner's plan to a commit or an
undo, and the three agents that the graph calls.

A job goes through these steps:
    plan: the planner picks the next step and writes its plan, or says that nothing is left.
    implement: the implementer makes the change, or reports that the plan cannot be done.
    check-rules: code checks the rules it can check by itself.
    test: the whole suite runs and is compared with the baseline.
    review: the reviewer judges the diff, and code turns an accept with an unmet check into a
        reject.
    fix-comments: code makes the reviewer's comment fixes when they change only comments.
    commit: the step is committed with the plan's title.
    undo: the code goes back to the last commit.

A failed check, a test failure or a rejection sends the job back to implement with a report,
until a cap is reached and the job is undone. When the job ends, job.data["outcome"] says how,
and job.data["reason"] says why for an outcome other than committed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from rooster import Agent, AgentGraph, Command, Job, Tool

from ratchet.agent_calls import NoAnswerError, ask
from ratchet.git import Repo
from ratchet.problem_history import problem_history
from ratchet.project import Project
from ratchet.records import records_log
from ratchet.pytest_runner import Outcome, find_regressions, format_failure_report, run_pytest
from ratchet.refactor.agent_answers import ImplementerAnswer, Plan, Review, Status, Verdict
from ratchet.refactor.agent_profiles import Role, write_profile
from ratchet.refactor.comment_fixes import apply_comment_fixes
from ratchet.refactor.job_outcome import JobOutcome
from ratchet.refactor.ledger import Ledger, ProblemEvent, ProblemRecord, save_ledger
from ratchet.refactor.problem_list import ProblemList
from ratchet.refactor.progress_lines import count_of, name_tests
from ratchet.refactor.review_reports import (
    find_unmet_checks,
    format_checks_record,
    format_review_report,
)
from ratchet.refactor.rule_checks import check_rules, snapshot_ratchet_files
from ratchet.refactor.settings import COMMAND, RefactorSettings

# How many of the latest jobs the planner sees.
RECENT_JOBS = 10

# How many of a problem's latest job events the planner sees.
RECENT_EVENTS = 3

# The kinds of event that come from a job, of which the planner sees only the latest.
JOB_EVENT_KINDS = ("job_started", "committed", "done", "sent_back", "abandoned")


# A function that shows what one step of a job did. It takes the job, the step's name and what
# happened in a few words, and as keywords the job's problems, the cost in dollars of the step's
# agent call, and a longer detail, as progress_lines.format_step_lines() takes them.
StepReport = Callable[..., None]


def _report_nothing(job: Job, step: str, text: str, **extras: object) -> None:
    """Shows nothing, for a graph that runs without a console."""


class RefactorError(RuntimeError):
    """Signals that a refactor run cannot go on, such as a refused plan, a dirty working tree, a
    suite that fails before the run, or a halted job."""


def make_agents(
    project: Project, settings: RefactorSettings, problems_file: str, folder: Path
) -> dict[str, Agent]:
    """Returns the planner, the implementer and the reviewer, each set up from its settings.

    Each agent starts every turn fresh, so its prompt carries everything it needs. Only the
    implementer may change files, and only those that the project allows.

    Args:
        project: The project, as read from its .ratchet folder.
        settings: The refactor command's settings.
        problems_file: The problem list's path from the project's folder, which the prompts
            name.
        folder: The folder for the agents' profiles and logs.

    Returns:
        The three agents, keyed by their role's name.
    """
    profiles = folder / "profiles"
    logs = folder / "logs"
    roles = {
        Role.PLANNER: (settings.planner, Plan, {}),
        Role.IMPLEMENTER: (settings.implementer, ImplementerAnswer, {
            "tools": Tool.AUTHOR,
            "commands": Command.PYTEST | Command.GIT_MV | Command.GIT_RM,
            "editable": tuple(project.config.editable),
            "frozen": tuple(project.frozen),
        }),
        Role.REVIEWER: (settings.reviewer, Review, {}),
    }
    agents = {}
    for role, (role_settings, answer, permissions) in roles.items():
        agents[str(role)] = Agent(
            str(role),
            write_profile(project, role, problems_file, profiles),
            cwd=project.root,
            log_dir=logs / role,
            model=role_settings.model,
            effort=role_settings.effort,
            schema=answer,
            max_work_usd=role_settings.budget_usd,
            timeout_s=role_settings.timeout_s,
            **({"tools": Tool.INSPECT} | permissions),
        )
    return agents


def _format_event(event: ProblemEvent, current_list: str) -> str:
    """Returns one problem event as a line, such as "2026-11-02 16:20 committed by J03: note".

    Args:
        event: The event.
        current_list: The name of the list the planner works on, which an event from that list
            does not name.

    Returns:
        The line, which names the event's list when it is an older one.
    """
    where = f" in {event.list}" if event.list and event.list != current_list else ""
    note = f": {event.note}" if event.note else ""
    return f"{event.when} {event.kind} by {event.by}{where}{note}"


def _format_planner_prompt(project: Project, job: Job, ledger: Ledger, problems_file: str) -> str:
    """Returns the planner's prompt: the status and history of every touched problem, the latest
    jobs, the records log, and any request from the person running ratchet.

    A problem's history shows its carried and reopened events from every list it passed
    through, and its last few job events.

    Args:
        project: The project, as read from its .ratchet folder.
        job: The job, whose prompt is a problem the person asks to be planned next, such as
            "P19", or "".
        ledger: The ledger of the problem list.
        problems_file: The problem list's path from the project's folder.

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
        events = problem_history(project, ledger.problem_list, problem_id)
        shown = [event for event in events if event.kind in ("carried", "reopened")]
        shown += [event for event in events if event.kind in JOB_EVENT_KINDS][-RECENT_EVENTS:]
        lines += [f"  - {_format_event(event, ledger.problem_list)}" for event in sorted(shown, key=events.index)]
    recent = ledger.jobs[-RECENT_JOBS:]
    lines += ["", f"## The last {len(recent)} jobs", ""]
    if recent:
        shown = [record.model_dump(mode="json", exclude={"slug", "cost_usd"}) for record in recent]
        lines += ["```json", json.dumps(shown, indent=2), "```"]
    else:
        lines.append("None yet.")
    commits = records_log(project.root)
    if commits:
        lines += ["", "## The records log", "", "The latest commits to the .ratchet records:", ""]
        lines += [f"- {commit}" for commit in commits]
    if job.prompt:
        lines += ["", "# A request from the person running ratchet", "",
                  f"Plan a step for {job.prompt} next, if the rules for ordering allow it."]
    return "\n".join(lines) + "\n"


def _format_implementer_prompt(job: Job, settings: RefactorSettings, diff: str) -> str:
    """Returns the implementer's prompt: the plan, the attempt, and on a later attempt the last
    report with the diff as it stands.

    Args:
        job: The job, whose data holds the plan, the attempt and the last report.
        settings: The refactor command's settings, which hold the caps.
        diff: The diff of the code as it stands.

    Returns:
        The prompt as markdown.
    """
    caps = settings.caps
    parts = [
        "# The plan for this step\n\n```json\n" + json.dumps(job.data["plan"], indent=2) + "\n```",
        f"# Attempt\n\nThis is attempt {job.data['attempt']}. The step is set aside after "
        f"{caps.rejections} rejections in a row or {caps.test_failures} test failures.",
    ]
    if job.data["report"]:
        parts.append("# The report on the last attempt\n\n" + job.data["report"])
        parts.append("# The diff of the code as it stands\n\n```diff\n" + diff + "\n```")
    return "\n\n".join(parts)


def _format_reviewer_prompt(job: Job, diff: str) -> str:
    """Returns the reviewer's prompt: the plan, the implementer's answer, the attempt and the diff.

    Args:
        job: The job, whose data holds the plan, the implementer's answer and the attempt.
        diff: The diff of the step against the last commit.

    Returns:
        The prompt as markdown.
    """
    return "\n\n".join([
        "# The plan\n\n```json\n" + json.dumps(job.data["plan"], indent=2) + "\n```",
        "# The implementer's answer\n\n```json\n" + json.dumps(job.data["answer"], indent=2) + "\n```",
        f"# Attempt\n\nThis is attempt {job.data['attempt']}. The whole suite passed on it.",
        "# The diff against the last commit\n\n```diff\n" + diff + "\n```",
    ])


def build_job_graph(
    project: Project,
    settings: RefactorSettings,
    agents: dict[str, Agent],
    baseline: dict[str, Outcome],
    folder: Path,
    ledger: Ledger,
    ledger_path: Path,
    problem_list: ProblemList,
    report: StepReport = _report_nothing,
) -> AgentGraph:
    """Returns the graph that carries one refactor job through its eight steps.

    The job's prompt is a problem that the person running ratchet asks to be planned next, such
    as "P19", or "". Each job keeps its prompts, reports, diffs and test logs in a folder named
    after it, beside its saved file. Every step except commit and undo reports what it did
    through 'report', and check-rules reports only a broken rule.

    Args:
        project: The project, as read from its .ratchet folder.
        settings: The refactor command's settings.
        agents: The planner, the implementer and the reviewer, keyed by their role's name, as
            make_agents() returns them.
        baseline: Each test's outcome before the run started, which every test run is
            compared with.
        folder: The folder that the graph keeps its jobs in, under a folder named "refactor".
        ledger: The ledger of the problem list, which the planner's prompt is made from. A
            plan that the ledger refuses halts the job before any code changes. A plan that
            passes gives each of its problems a job_started event.
        ledger_path: The ledger's file, which the plan step saves right after it records
            job_started.
        problem_list: The problem list that the jobs work on.
        report: The function that shows what each step did, as StepReport describes.

    Returns:
        The graph, ready to start jobs.
    """
    graph = AgentGraph(COMMAND, agents, start="plan", state_dir=folder)
    repo = Repo(project.root)
    caps = settings.caps
    planner = agents[Role.PLANNER]
    implementer = agents[Role.IMPLEMENTER]
    reviewer = agents[Role.REVIEWER]
    # The .ratchet files as they were when the graph was built, which no step may change.
    ratchet_files = snapshot_ratchet_files(project)

    def job_folder(job: Job) -> Path:
        """Returns the folder that holds one job's files."""
        return graph.state_dir / job.slug

    def attempt_folder(job: Job) -> Path:
        """Returns the folder that holds the current attempt's files."""
        folder = job_folder(job) / f"attempt-{job.data['attempt']}"
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    def end(job: Job, outcome: JobOutcome, reason: str = "") -> str:
        """Records how the job ended and returns "undo", the step that every such end goes to."""
        job.data["outcome"] = outcome
        job.data["reason"] = reason
        return "undo"

    def send_back(job: Job, report_text: str) -> str:
        """Records a rejection with its report, and returns the step that follows it."""
        (attempt_folder(job) / "report.md").write_text(report_text, encoding="utf-8")
        job.data["report"] = report_text
        job.data["rejections"] += 1
        if job.data["rejections"] >= caps.rejections:
            return end(job, JobOutcome.ABANDONED, report_text)
        return "implement"

    def where_next(job: Job, after: str) -> str:
        """Returns where the job goes after a failure, such as "-> attempt 2" or "-> undo"."""
        if after == "implement":
            return f"-> attempt {job.data['attempt'] + 1}"
        if job.data.get("outcome") != JobOutcome.ABANDONED:
            return "-> undo"
        if job.data["test_failures"] >= caps.test_failures:
            return f"-> undo, at the cap of {count_of(caps.test_failures, 'test failure')}"
        return f"-> undo, at the cap of {count_of(caps.rejections, 'rejection')}"

    @graph.step("plan", next=("implement", None))
    def plan(job: Job) -> str | None:
        prompt = _format_planner_prompt(project, job, ledger, problem_list.relative_path)
        reply = ask(planner, prompt, job_folder(job))
        answer, cost = reply.data, reply.cost_usd
        job.data.update(plan=answer.model_dump(mode="json", by_alias=True), attempt=0,
                        rejections=0, test_failures=0, report="")
        (job_folder(job) / "plan.json").write_text(json.dumps(job.data["plan"], indent=2),
                                                    encoding="utf-8")
        if answer.stop:
            report(job, "plan", "nothing left to plan", cost_usd=cost, detail=answer.reason)
            job.data.update(outcome=JobOutcome.STOPPED, reason=answer.reason)
            return None
        report(job, "plan", answer.summary, problems=", ".join(answer.problems), cost_usd=cost)
        refusal = ledger.find_plan_refusal(answer, problem_list.ids)
        if refusal:
            report(job, "plan", f"refused: {refusal}")
            raise RefactorError(f"the plan was refused: {refusal}")

        # record job_started for each of the plan's problems and save the ledger
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        job_id = f"J{len(ledger.jobs) + 1:02d}"
        for problem_id in answer.problems:
            problem = ledger.problems.setdefault(problem_id, ProblemRecord())
            problem.events.append(ProblemEvent(when=now, kind="job_started", by=job_id,
                                               job=job_id, note=answer.summary))
        save_ledger(ledger, ledger_path)
        return "implement"

    @graph.step("implement", next=("check-rules", "undo"))
    def implement(job: Job) -> str:
        job.data["attempt"] += 1
        diff = repo.diff() if job.data["report"] else ""
        work = repo.save_work()
        try:
            reply = ask(implementer, _format_implementer_prompt(job, settings, diff),
                        attempt_folder(job))
        except NoAnswerError as error:
            # the cut-off turn's edits are dropped, so that a resumed job starts this attempt clean
            repo.restore_work(work)
            report(job, "implement", f"attempt {job.data['attempt']} gave no answer",
                   detail=str(error))
            job.data["attempt"] -= 1
            raise
        answer, cost = reply.data, reply.cost_usd
        job.data["answer"] = answer.model_dump(mode="json")
        if answer.status == Status.BLOCKED:
            report(job, "implement", f"attempt {job.data['attempt']} blocked -> undo",
                   cost_usd=cost, detail=answer.blocked_reason)
            return end(job, JobOutcome.BLOCKED, answer.blocked_reason)
        report(job, "implement", f"attempt {job.data['attempt']}", cost_usd=cost)
        return "check-rules"

    @graph.step("check-rules", next=("test", "implement", "undo"))
    def check_rules_step(job: Job) -> str:
        answer = ImplementerAnswer.model_validate(job.data["answer"])
        broken = check_rules(project, repo, answer, ratchet_files)
        if not broken:
            return "test"
        rules = [line[2:] for line in broken.splitlines() if line.startswith("- ")]
        after = send_back(job, broken)
        report(job, "check-rules", f"broke {count_of(len(rules), 'rule')} {where_next(job, after)}",
               detail="; ".join(rules))
        return after

    @graph.step("test", next=("review", "implement", "undo"))
    def test(job: Job) -> str:
        folder = attempt_folder(job)
        run = run_pytest(project.config.test_command, project.root, folder,
                         project.config.test_timeout_s)
        worse = find_regressions(baseline, run.outcomes) if run.outcomes else {}
        if not worse and run.outcomes:
            report(job, "test", f"passed ({count_of(len(run.outcomes), 'test')})")
            return "review"
        failure = format_failure_report(worse, run, folder / "pytest.log")
        (folder / "report.md").write_text(failure, encoding="utf-8")
        job.data["report"] = failure
        job.data["rejections"] = 0
        job.data["test_failures"] += 1
        after = "implement"
        if job.data["test_failures"] >= caps.test_failures:
            after = end(job, JobOutcome.ABANDONED, failure)
        what = (f"{len(worse)} newly failing: {name_tests(list(worse))}" if worse
                else "the test process crashed")
        report(job, "test", f"{what} {where_next(job, after)}")
        return after

    @graph.step("review", next=("commit", "fix-comments", "implement", "undo"))
    def review(job: Job) -> str:
        folder = attempt_folder(job)
        diff = repo.diff()
        (folder / "diff.patch").write_text(diff, encoding="utf-8")
        the_plan = Plan.model_validate(job.data["plan"])
        reply = ask(reviewer, _format_reviewer_prompt(job, diff), folder)
        verdict, cost = reply.data, reply.cost_usd
        (folder / "checks.md").write_text(format_checks_record(the_plan, verdict), encoding="utf-8")
        said = verdict.summary

        # an accept with a check that the reviewer did not confirm becomes a reject
        unmet = find_unmet_checks(the_plan, verdict)
        overruled = bool(unmet) and verdict.verdict in (Verdict.ACCEPT,
                                                        Verdict.ACCEPT_AFTER_COMMENT_FIXES)
        if overruled:
            numbers = ", ".join(str(number) for number in unmet)
            verdict.verdict = Verdict.REJECT
            verdict.summary = (f"The reviewer accepted the step, but the script rejected it, "
                               f"because the reviewer did not confirm checks {numbers}.")
        job.data["review"] = verdict.model_dump(mode="json", by_alias=True)
        job.data["notes"] = verdict.notes

        if verdict.verdict == Verdict.ACCEPT:
            report(job, "review", "accept", cost_usd=cost)
            return "commit"
        if verdict.verdict == Verdict.ACCEPT_AFTER_COMMENT_FIXES:
            fixes = count_of(len(verdict.comment_fixes), "comment fix", "comment fixes")
            report(job, "review", f"accept after {fixes}", cost_usd=cost)
            return "fix-comments"

        faults = [name for name, criterion in verdict.criteria if not criterion.passed]
        if unmet:
            checks = ", ".join(str(number) for number in unmet)
            faults.append(f"{'check' if len(unmet) == 1 else 'checks'} {checks} unmet")
        if overruled:
            faults.append("overruling the reviewer's accept")
        report_text = format_review_report(the_plan, verdict)
        if verdict.verdict == Verdict.REPLAN:
            after = end(job, JobOutcome.REPLAN, report_text)
        else:
            after = send_back(job, report_text)
        named = "; ".join(faults) or "no fault named"
        report(job, "review", f"{verdict.verdict}: {named} {where_next(job, after)}",
               cost_usd=cost, detail=said)
        return after

    @graph.step("fix-comments", next=("commit", "implement", "undo"))
    def fix_comments(job: Job) -> str:
        verdict = Review.model_validate(job.data["review"])
        failure = apply_comment_fixes(project, verdict)
        if not failure:
            (attempt_folder(job) / "comment-fixes.patch").write_text(repo.diff(), encoding="utf-8")
            job.data["comment_fixes"] = len(verdict.comment_fixes)
            made = count_of(len(verdict.comment_fixes), "fix", "fixes")
            report(job, "fix-comments", f"{made} made")
            return "commit"
        report_text = format_review_report(Plan.model_validate(job.data["plan"]), verdict)
        report_text += (f"\nThe script could not make the comment fixes, because {failure}. "
                        "Make them yourself.\n")
        after = send_back(job, report_text)
        report(job, "fix-comments", f"refused {where_next(job, after)}", detail=failure)
        return after

    @graph.step("commit", next=(None,))
    def commit(job: Job) -> None:
        job.data["commit"] = repo.commit(job.data["plan"]["step_title"])
        job.data["outcome"] = JobOutcome.COMMITTED
        job.data["reason"] = ""
        return None

    @graph.step("undo", next=(None,))
    def undo(job: Job) -> None:
        repo.undo()
        return None

    return graph
