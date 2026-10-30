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
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel
from rooster import Agent, AgentGraph, Command, Job, Tool

from ratchet.git import Repo
from ratchet.project import Project
from ratchet.pytest_runner import Outcome, find_regressions, format_failure_report, run_pytest
from ratchet.refactor.agent_answers import ImplementerAnswer, Plan, Review, Status, Verdict
from ratchet.refactor.agent_profiles import Role, write_profile
from ratchet.refactor.comment_fixes import apply_comment_fixes
from ratchet.refactor.review_reports import (
    find_unmet_checks,
    format_checks_record,
    format_review_report,
)
from ratchet.refactor.rule_checks import check_rules, snapshot_ratchet_files
from ratchet.refactor.settings import COMMAND, RefactorSettings


class RefactorError(RuntimeError):
    """Signals an agent call that did not produce an answer, which halts the job."""


class JobOutcome(StrEnum):
    """Names how a refactor job ended."""

    # The planner found nothing left to plan.
    STOPPED = "stopped"
    COMMITTED = "committed"
    # The implementer could not carry out the plan, or the reviewer found the fault in the plan.
    BLOCKED = "blocked"
    REPLAN = "replan"
    # The step reached a cap on rejections or test failures.
    ABANDONED = "abandoned"


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


def _ask(agent: Agent, prompt: str, folder: Path) -> BaseModel:
    """Runs one agent turn and returns its answer, saving the prompt beside the attempt.

    Args:
        agent: The agent to run.
        prompt: The turn's prompt.
        folder: The folder that the prompt is saved in, as <agent name>-prompt.md.

    Returns:
        The agent's answer, as an instance of its schema.

    Raises:
        RefactorError: The turn did not complete, or its answer did not fit the schema.
    """
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{agent.name}-prompt.md").write_text(prompt, encoding="utf-8")
    result = agent.run(prompt)
    if not result["ok"]:
        raise RefactorError(f"the {agent.name} gave no answer: {result['failure']}")
    return result["data"]


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
    check_plan: Callable[[Plan], str] = lambda plan: "",
) -> AgentGraph:
    """Returns the graph that carries one refactor job through its eight steps.

    The job's prompt is the planner's prompt. Each job keeps its prompts, reports, diffs and
    test logs in a folder named after it, beside its saved file.

    Args:
        project: The project, as read from its .ratchet folder.
        settings: The refactor command's settings.
        agents: The planner, the implementer and the reviewer, keyed by their role's name, as
            make_agents() returns them.
        baseline: Each test's outcome before the run started, which every test run is
            compared with.
        folder: The folder that the graph keeps its jobs in, under a folder named "refactor".
        check_plan: A function that returns why a plan may not go ahead, or "" when it may.
            A plan it refuses halts the job before any code changes.

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

    def send_back(job: Job, report: str) -> str:
        """Records a rejection with its report, and returns the step that follows it."""
        (attempt_folder(job) / "report.md").write_text(report, encoding="utf-8")
        job.data["report"] = report
        job.data["rejections"] += 1
        if job.data["rejections"] >= caps.rejections:
            return end(job, JobOutcome.ABANDONED, report)
        return "implement"

    @graph.step("plan", next=("implement", None))
    def plan(job: Job) -> str | None:
        answer = _ask(planner, job.prompt, job_folder(job))
        job.data.update(plan=answer.model_dump(mode="json", by_alias=True), attempt=0,
                        rejections=0, test_failures=0, report="")
        (job_folder(job) / "plan.json").write_text(json.dumps(job.data["plan"], indent=2),
                                                    encoding="utf-8")
        if answer.stop:
            job.data.update(outcome=JobOutcome.STOPPED, reason=answer.reason)
            return None
        refusal = check_plan(answer)
        if refusal:
            raise RefactorError(f"the plan was refused: {refusal}")
        return "implement"

    @graph.step("implement", next=("check-rules", "undo"))
    def implement(job: Job) -> str:
        job.data["attempt"] += 1
        diff = repo.diff() if job.data["report"] else ""
        work = repo.save_work()
        try:
            answer = _ask(implementer, _format_implementer_prompt(job, settings, diff),
                          attempt_folder(job))
        except RefactorError:
            # the cut-off turn's edits are dropped, so that a resumed job starts this attempt clean
            repo.restore_work(work)
            job.data["attempt"] -= 1
            raise
        job.data["answer"] = answer.model_dump(mode="json")
        if answer.status == Status.BLOCKED:
            return end(job, JobOutcome.BLOCKED, answer.blocked_reason)
        return "check-rules"

    @graph.step("check-rules", next=("test", "implement", "undo"))
    def check_rules_step(job: Job) -> str:
        answer = ImplementerAnswer.model_validate(job.data["answer"])
        report = check_rules(project, repo, answer, ratchet_files)
        return send_back(job, report) if report else "test"

    @graph.step("test", next=("review", "implement", "undo"))
    def test(job: Job) -> str:
        folder = attempt_folder(job)
        run = run_pytest(project.config.test_command, project.root, folder,
                         project.config.test_timeout_s)
        worse = find_regressions(baseline, run.outcomes) if run.outcomes else {}
        if not worse and run.outcomes:
            return "review"
        report = format_failure_report(worse, run, folder / "pytest.log")
        (folder / "report.md").write_text(report, encoding="utf-8")
        job.data["report"] = report
        job.data["rejections"] = 0
        job.data["test_failures"] += 1
        if job.data["test_failures"] >= caps.test_failures:
            return end(job, JobOutcome.ABANDONED, report)
        return "implement"

    @graph.step("review", next=("commit", "fix-comments", "implement", "undo"))
    def review(job: Job) -> str:
        folder = attempt_folder(job)
        diff = repo.diff()
        (folder / "diff.patch").write_text(diff, encoding="utf-8")
        the_plan = Plan.model_validate(job.data["plan"])
        verdict = _ask(reviewer, _format_reviewer_prompt(job, diff), folder)
        (folder / "checks.md").write_text(format_checks_record(the_plan, verdict), encoding="utf-8")

        # an accept with a check that the reviewer did not confirm becomes a reject
        unmet = find_unmet_checks(the_plan, verdict)
        if unmet and verdict.verdict in (Verdict.ACCEPT, Verdict.ACCEPT_AFTER_COMMENT_FIXES):
            numbers = ", ".join(str(number) for number in unmet)
            verdict.verdict = Verdict.REJECT
            verdict.summary = (f"The reviewer accepted the step, but the script rejected it, "
                               f"because the reviewer did not confirm checks {numbers}.")
        job.data["review"] = verdict.model_dump(mode="json", by_alias=True)
        job.data["notes"] = verdict.notes

        if verdict.verdict == Verdict.ACCEPT:
            return "commit"
        if verdict.verdict == Verdict.ACCEPT_AFTER_COMMENT_FIXES:
            return "fix-comments"
        report = format_review_report(the_plan, verdict)
        if verdict.verdict == Verdict.REPLAN:
            return end(job, JobOutcome.REPLAN, report)
        return send_back(job, report)

    @graph.step("fix-comments", next=("commit", "implement", "undo"))
    def fix_comments(job: Job) -> str:
        verdict = Review.model_validate(job.data["review"])
        failure = apply_comment_fixes(project, verdict)
        if not failure:
            (attempt_folder(job) / "comment-fixes.patch").write_text(repo.diff(), encoding="utf-8")
            job.data["comment_fixes"] = len(verdict.comment_fixes)
            return "commit"
        report = format_review_report(Plan.model_validate(job.data["plan"]), verdict)
        report += (f"\nThe script could not make the comment fixes, because {failure}. "
                   "Make them yourself.\n")
        return send_back(job, report)

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
