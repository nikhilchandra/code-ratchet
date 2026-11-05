"""Builds the graph that carries one refactor job, from the planner's plan to a commit or an
undo, and the five agents that the graph calls.

A job goes through these steps:
    plan: the planner picks the next step and writes its plan, or says that nothing is left.
    super-plan: code builds the pathway map of the plan's functions, and the super-planner
        approves the plan with the tests it wants written, or sends it back to plan.
    write-tests: the test writer writes the tests, and code checks that they pass on the code as
        it is.
    implement: the implementer makes the change, or reports that the plan cannot be done.
    check-map: code maps the functions that the change touches, and a function that neither the
        plan nor an earlier approval names goes to review-map.
    review-map: the super-planner approves the functions outside the plan, or sends the job back
        to implement.
    check-rules: code checks the rules it can check by itself.
    test: the whole suite runs and is compared with the baseline. When coverage is on, every
        added or changed line in src must also be run by some test.
    review: the reviewer judges the diff, and code turns an accept with an unmet check into a
        reject.
    fix-comments: code makes the reviewer's comment fixes when they change only comments.
    commit: the step is committed with the plan's title.
    undo: the code goes back to the last commit.

A failed check, a test failure, an untested line or a rejection sends the job back to implement with a report, a
revise sends it back to plan, and a refused test write sends it back to write-tests, each until a
cap is reached and the job is undone. When the job ends, job.data["outcome"] says how,
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
from ratchet.pathways import MODULE_PART, build_map, describe, functions_of_diff
from ratchet.pytest_runner import (
    Outcome,
    OutcomeKind,
    find_regressions,
    find_untested_lines,
    format_failure_report,
    format_untested_report,
    outcomes_of_files,
    run_pytest,
)
from ratchet.refactor.agent_answers import (
    ImplementerAnswer,
    MapReview,
    Plan,
    Review,
    Status,
    SuperPlan,
    SuperVerdict,
    Verdict,
    WantedTest,
    WriterAnswer,
)
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
from ratchet.refactor.rule_checks import check_rules, find_violations, snapshot_ratchet_files
from ratchet.refactor.settings import COMMAND, RefactorSettings
from ratchet.refactor.written_tests import find_write_faults, read_written, save_written

# How many of the latest jobs the planner sees.
RECENT_JOBS = 10

# How many of a problem's latest job events the planner sees.
RECENT_EVENTS = 3

# The kinds of event that come from a job, of which the planner sees only the latest.
JOB_EVENT_KINDS = ("job_started", "committed", "done", "sent_back", "abandoned")


# How many untested lines a console line names before it counts the rest.
LINES_NAMED = 3


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
    """Returns the five agents, each set up from its settings.

    Each agent starts every turn fresh, so its prompt carries everything it needs. Only the test
    writer and the implementer may change files, and only those that the project allows.

    Args:
        project: The project, as read from its .ratchet folder.
        settings: The refactor command's settings.
        problems_file: The problem list's path from the project's folder, which the prompts
            name.
        folder: The folder for the agents' profiles and logs.

    Returns:
        The five agents, keyed by their role's name.
    """
    profiles = folder / "profiles"
    logs = folder / "logs"
    roles = {
        Role.PLANNER: (settings.planner, Plan, {}),
        Role.SUPER_PLANNER: (settings.super_planner, SuperPlan, {}),
        Role.TEST_WRITER: (settings.test_writer, WriterAnswer, {
            "tools": Tool.AUTHOR,
            "commands": Command.PYTEST,
            "editable": tuple(project.config.editable),
            "frozen": tuple(project.frozen),
        }),
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
            "P19", or "". When the super-planner sent a plan back, the job's data holds that
            plan and what the super-planner asked for.
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
    revised = job.data.get("revised")
    if revised:
        lines += ["", "# The super-planner sent your plan back", "",
                  "Make a plan that meets every revision below, or stop if no plan can.", "",
                  "## The plan that was sent back", "",
                  "```json", json.dumps(revised["plan"], indent=2), "```", "",
                  "## What the super-planner said", "", revised["summary"], "",
                  "## The revisions", ""]
        lines += [f"- {revision}" for revision in revised["revisions"]]
    return "\n".join(lines) + "\n"


def _format_super_planner_prompt(plan: dict, pathways: dict, rules: str) -> str:
    """Returns the super-planner's prompt: the plan, the pathway map of its functions, and the
    project's rules.

    Args:
        plan: The planner's plan, as the job's data holds it.
        pathways: The pathway map of the plan's functions, as ratchet.pathways.describe() returns
            it.
        rules: The text of the project's rules.md.

    Returns:
        The prompt as markdown.
    """
    return "\n\n".join([
        "# The plan\n\n```json\n" + json.dumps(plan, indent=2) + "\n```",
        "# The pathway map of the plan's functions\n\n```json\n"
        + json.dumps(pathways, indent=2) + "\n```",
        "# The project's rules\n\n" + rules.strip(),
    ])


def _format_map_review_prompt(
    plan: dict, outside: list[str], pathways: dict, diff: str, rules: str
) -> str:
    """Returns the super-planner's prompt for the functions that the change touches outside the
    plan: the plan, those functions with their pathway map, the diff, and the project's rules.

    Args:
        plan: The planner's plan, as the job's data holds it.
        outside: The changed functions that neither the plan nor an earlier approval names.
        pathways: The pathway map of those functions, as ratchet.pathways.describe() returns it.
        diff: The diff of the code as it stands.
        rules: The text of the project's rules.md.

    Returns:
        The prompt as markdown.
    """
    functions = "\n".join(f"- `{name}`" for name in outside)
    return "\n\n".join([
        "# The implementer changed functions that the plan does not name\n\n"
        "Approve them when the plan's step needs them, and revise when it does not. A revise "
        "sends the job back to the implementer with your revisions, and an approve lets the "
        "functions stay. Answer in the shape that your instructions give for the functions outside "
        "the plan, which holds only a verdict, a summary and revisions.",
        "# The functions outside the plan\n\n" + functions,
        "# The plan\n\n```json\n" + json.dumps(plan, indent=2) + "\n```",
        "# The pathway map of the functions outside the plan\n\n```json\n"
        + json.dumps(pathways, indent=2) + "\n```",
        "# The diff of the code as it stands\n\n```diff\n" + diff + "\n```",
        "# The project's rules\n\n" + rules.strip(),
    ])


def _name_lines(untested: dict[str, list[int]]) -> str:
    """Returns the untested lines for a console line, naming the first few as "src/app.py:12" and
    counting the rest."""
    named = [f"{name}:{number}" for name, numbers in untested.items() for number in numbers]
    shown = ", ".join(named[:LINES_NAMED])
    rest = len(named) - LINES_NAMED
    return f"{shown} +{rest} more" if rest > 0 else shown


def _format_writer_prompt(wanted: list[WantedTest], report: str) -> str:
    """Returns the test writer's prompt: the tests to write, and on a later turn the report on
    the last one.

    Args:
        wanted: The tests that the super-planner asked for.
        report: The report on the test writer's last turn, or "".

    Returns:
        The prompt as markdown.
    """
    lines = ["# The tests to write", ""]
    for test in wanted:
        lines += [f"- `{test.file}`: `{test.name}` covers {test.covers}", f"  - checks: {test.checks}"]
    prompt = "\n".join(lines)
    if report:
        prompt += "\n\n# The report on your last turn\n\n" + report
    return prompt


def _format_written_section(written: list[str]) -> str:
    """Returns the part of a prompt that names the tests which the test writer wrote.

    Args:
        written: The paths of the written tests.

    Returns:
        A markdown section.
    """
    lines = ["# The tests the test writer wrote", "",
             "The test writer wrote these tests before the step, and they are frozen for it:", ""]
    return "\n".join([*lines, *[f"- `{path}`" for path in written]])


def _format_implementer_prompt(job: Job, settings: RefactorSettings, diff: str) -> str:
    """Returns the implementer's prompt: the plan, the attempt, and on a later attempt the last
    report with the diff as it stands.

    Args:
        job: The job, whose data holds the plan, the attempt, the last report and the written
            tests.
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
    if job.data.get("written"):
        parts.append(_format_written_section(job.data["written"]) +
                     "\n\nMake the code pass them. Do not change, move or delete them.")
    if job.data["report"]:
        parts.append("# The report on the last attempt\n\n" + job.data["report"])
        parts.append("# The diff of the code as it stands\n\n```diff\n" + diff + "\n```")
    return "\n\n".join(parts)


def _format_reviewer_prompt(job: Job, diff: str) -> str:
    """Returns the reviewer's prompt: the plan, the implementer's answer, the attempt and the diff.

    Args:
        job: The job, whose data holds the plan, the implementer's answer, the attempt and the
            written tests.
        diff: The diff of the step against the last commit.

    Returns:
        The prompt as markdown.
    """
    parts = [
        "# The plan\n\n```json\n" + json.dumps(job.data["plan"], indent=2) + "\n```",
        "# The implementer's answer\n\n```json\n" + json.dumps(job.data["answer"], indent=2) + "\n```",
        f"# Attempt\n\nThis is attempt {job.data['attempt']}. The whole suite passed on it.",
        "# The diff against the last commit\n\n```diff\n" + diff + "\n```",
    ]
    if job.data.get("written"):
        parts.insert(2, _format_written_section(job.data["written"]) +
                     "\n\nThey are in the diff, but judge the implementer's work against them and "
                     "not the tests themselves.")
    return "\n\n".join(parts)


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
    coverage: bool = False,
) -> AgentGraph:
    """Returns the graph that carries one refactor job through its twelve steps.

    The job's prompt is a problem that the person running ratchet asks to be planned next, such
    as "P19", or "". Each job keeps its prompts, reports, diffs and test logs in a folder named
    after it, beside its saved file. Every step except commit and undo reports what it did
    through 'report', and check-rules reports only a broken rule and check-map only a function
    outside the plan.

    Args:
        project: The project, as read from its .ratchet folder.
        settings: The refactor command's settings.
        agents: The five agents, keyed by their role's name, as
            make_agents() returns them.
        baseline: Each test's outcome before the run started, which every test run is
            compared with.
        folder: The folder that the graph keeps its jobs in, under a folder named "refactor".
        ledger: The ledger of the problem list, which the planner's prompt is made from. A
            plan that the ledger refuses halts the job before any code changes. A plan that
            passes gives each of its problems a job_started event, once for the job even when
            the super-planner sends the plan back.
        ledger_path: The ledger's file, which the plan step saves right after it records
            job_started.
        problem_list: The problem list that the jobs work on.
        report: The function that shows what each step did, as StepReport describes.
        coverage: Whether the test step also measures coverage, and sends the job back to implement
            when a line that the change adds or changes in src is run by no test. The caller sets
            it when pytest-cov loads in the project's test environment.

    Returns:
        The graph, ready to start jobs.
    """
    graph = AgentGraph(COMMAND, agents, start="plan", state_dir=folder)
    repo = Repo(project.root)
    caps = settings.caps
    planner = agents[Role.PLANNER]
    super_planner = agents[Role.SUPER_PLANNER]
    test_writer = agents[Role.TEST_WRITER]
    implementer = agents[Role.IMPLEMENTER]
    reviewer = agents[Role.REVIEWER]
    # The .ratchet files as they were when the graph was built, which no step may change.
    ratchet_files = snapshot_ratchet_files(project)
    # The implementer's own frozen paths, to which each job adds the tests that were written for it.
    implementer_frozen = tuple(implementer.frozen)

    def job_folder(job: Job) -> Path:
        """Returns the folder that holds one job's files."""
        return graph.state_dir / job.slug

    def planning_folder(job: Job) -> Path:
        """Returns the folder that holds the current planning round's files, which is the job's
        own folder for the first round and a folder for each round after a revise."""
        rounds = job.data.get("revisions", 0)
        return job_folder(job) / f"revision-{rounds}" if rounds else job_folder(job)

    def writing_folder(job: Job) -> Path:
        """Returns the folder that holds the current test-writing turn's files."""
        return job_folder(job) / f"write-tests-{job.data['rewrites'] + 1}"

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
        if after == "plan":
            return f"-> plan again, revision {job.data['revisions']} of {caps.revisions}"
        if after == "write-tests":
            return f"-> write again, rewrite {job.data['rewrites']} of {caps.test_rewrites}"
        if job.data.get("outcome") != JobOutcome.ABANDONED:
            return "-> undo"
        if job.data["revisions"] > caps.revisions:
            return f"-> undo, at the cap of {count_of(caps.revisions, 'revision')}"
        if job.data["rewrites"] > caps.test_rewrites:
            return f"-> undo, at the cap of {count_of(caps.test_rewrites, 'rewrite')}"
        if job.data["test_failures"] >= caps.test_failures:
            return f"-> undo, at the cap of {count_of(caps.test_failures, 'test failure')}"
        return f"-> undo, at the cap of {count_of(caps.rejections, 'rejection')}"

    @graph.step("plan", next=("super-plan", "undo", None))
    def plan(job: Job) -> str | None:
        prompt = _format_planner_prompt(project, job, ledger, problem_list.relative_path)
        reply = ask(planner, prompt, planning_folder(job))
        answer, cost = reply.data, reply.cost_usd
        replanned = bool(job.data.get("revisions"))
        written = answer.model_dump(mode="json", by_alias=True)
        (planning_folder(job) / "plan.json").write_text(json.dumps(written, indent=2),
                                                         encoding="utf-8")
        if replanned and answer.stop:
            # the job keeps the plan the super-planner last saw, so that the ledger records it
            report(job, "plan", "nothing left to plan after a revise -> undo", cost_usd=cost,
                   detail=answer.reason)
            return end(job, JobOutcome.ABANDONED,
                       f"The planner had nothing left to plan after a revise: {answer.reason}")
        if not replanned:
            job.data.update(attempt=0, rejections=0, test_failures=0, report="", revisions=0,
                            rewrites=0, started=[], written=[], write_report="", approved=[])
        job.data["plan"] = written
        if answer.stop:
            report(job, "plan", "nothing left to plan", cost_usd=cost, detail=answer.reason)
            job.data.update(outcome=JobOutcome.STOPPED, reason=answer.reason)
            return None
        report(job, "plan", answer.summary, problems=", ".join(answer.problems), cost_usd=cost)
        refusal = ledger.find_plan_refusal(answer, problem_list.ids)
        if refusal:
            report(job, "plan", f"refused: {refusal}")
            raise RefactorError(f"the plan was refused: {refusal}")

        # record job_started for each of the plan's problems that the job has not started, under
        # the job's id, and save the ledger
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        job_id = job.data.setdefault("job_id", f"J{ledger.next_job_number():02d}")
        for problem_id in answer.problems:
            if problem_id in job.data["started"]:
                continue
            job.data["started"].append(problem_id)
            problem = ledger.problems.setdefault(problem_id, ProblemRecord())
            problem.events.append(ProblemEvent(when=now, kind="job_started", by=job_id,
                                               job=job_id, note=answer.summary))
        save_ledger(ledger, ledger_path)
        return "super-plan"

    @graph.step("super-plan", next=("plan", "write-tests", "implement", "undo"))
    def super_plan(job: Job) -> str:
        pathways = describe(build_map(project.root), Plan.model_validate(job.data["plan"]).functions)
        prompt = _format_super_planner_prompt(job.data["plan"], pathways, project.rules)
        reply = ask(super_planner, prompt, planning_folder(job))
        answer, cost = reply.data, reply.cost_usd
        (planning_folder(job) / "super-plan.json").write_text(
            answer.model_dump_json(indent=2), encoding="utf-8")
        if answer.verdict == SuperVerdict.REVISE:
            job.data["revisions"] += 1
            job.data["revised"] = {"plan": job.data["plan"], "summary": answer.summary,
                                   "revisions": answer.revisions}
            after = "plan"
            if job.data["revisions"] > caps.revisions:
                notes = "\n".join(f"- {revision}" for revision in answer.revisions)
                after = end(job, JobOutcome.ABANDONED,
                            f"# The plan was sent back too often\n\n{answer.summary}\n\n{notes}")
            report(job, "super-plan", f"revise {where_next(job, after)}", cost_usd=cost,
                   detail=answer.summary)
            return after
        wanted = answer.tests_to_write
        job.data["wanted"] = [test.model_dump(mode="json") for test in wanted]
        to_write = f"{count_of(len(wanted), 'test')} to write" if wanted else "no tests to write"
        report(job, "super-plan", f"approve, {to_write}", cost_usd=cost)
        return "write-tests" if wanted else "implement"

    @graph.step("write-tests", next=("write-tests", "implement", "undo"))
    def write_tests(job: Job) -> str:
        wanted = [WantedTest.model_validate(test) for test in job.data["wanted"]]
        folder = writing_folder(job)
        try:
            reply = ask(test_writer, _format_writer_prompt(wanted, job.data["write_report"]),
                        folder)
        except NoAnswerError as error:
            # the cut-off turn's files are dropped, so that a resumed job starts this turn clean
            repo.undo()
            report(job, "write-tests", "gave no answer", detail=str(error))
            raise
        cost = reply.cost_usd
        faults, paths = find_write_faults(project, repo, [test.file for test in wanted])
        if faults:
            report_text = ("# The tests were refused\n\n"
                           + "\n".join(f"- {fault}" for fault in faults))
        else:
            run = run_pytest([*project.config.test_command, *paths], project.root, folder,
                             project.config.test_timeout_s)
            # test_command may name a test folder, so the run can hold tests that were not written
            written = outcomes_of_files(run.outcomes, paths)
            failed = {test_id: outcome for test_id, outcome in written.items()
                      if outcome.kind != OutcomeKind.PASSED}
            if written and not failed:
                save_written(project, job_folder(job), paths)
                job.data["written"] = paths
                made = f"{count_of(len(paths), 'file')}, {count_of(len(written), 'test')} passed"
                report(job, "write-tests", made, cost_usd=cost)
                return "implement"
            if failed:
                faults = [f"{len(failed)} failing on the code as it is: {name_tests(list(failed))}"]
            elif run.outcomes:
                faults = ["none of the written tests were collected"]
            else:
                faults = ["the test process crashed"]
            report_text = format_failure_report(failed, run, folder / "pytest.log")

        # the refused tests are undone, and the test writer sees them with the report
        report_text += "\n\n# What you wrote\n\n```diff\n" + repo.diff() + "\n```\n"
        repo.undo()
        (folder / "report.md").write_text(report_text, encoding="utf-8")
        job.data["write_report"] = report_text
        job.data["rewrites"] += 1
        after = "write-tests"
        if job.data["rewrites"] > caps.test_rewrites:
            after = end(job, JobOutcome.ABANDONED, report_text)
        report(job, "write-tests", f"refused: {'; '.join(faults)} {where_next(job, after)}",
               cost_usd=cost)
        return after

    @graph.step("implement", next=("check-map", "undo"))
    def implement(job: Job) -> str:
        # record the violations of the committed code before the job's first attempt
        if job.data["attempt"] == 0:
            job.data["violations_before"] = find_violations(project)
        job.data["attempt"] += 1
        # the tests written for this job are frozen for the implementer, and for it alone
        implementer.frozen = implementer_frozen + tuple(job.data["written"])
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
        return "check-map"

    @graph.step("check-map", next=("check-rules", "review-map"))
    def check_map(job: Job) -> str:
        pathway_map = build_map(project.root)
        known = {*Plan.model_validate(job.data["plan"]).functions, *job.data["approved"]}
        # a change to module-level code is not a change to a function
        job.data["outside"] = [name for name in functions_of_diff(pathway_map, repo.diff())
                               if name not in known and not name.endswith(f".{MODULE_PART}")]
        if not job.data["outside"]:
            return "check-rules"
        report(job, "check-map", f"{count_of(len(job.data['outside']), 'function')} outside the "
               "plan -> review-map", detail=", ".join(job.data["outside"]))
        return "review-map"

    @graph.step("review-map", next=("check-rules", "implement", "undo"))
    def review_map(job: Job) -> str:
        outside = job.data["outside"]
        diff = repo.diff()
        pathways = describe(build_map(project.root), outside)
        prompt = _format_map_review_prompt(job.data["plan"], outside, pathways, diff, project.rules)
        folder = attempt_folder(job)
        # the map review answers in its own shape, which has no tests to write
        super_planner.schema = MapReview
        try:
            reply = ask(super_planner, prompt, folder)
        finally:
            super_planner.schema = SuperPlan
        answer, cost = reply.data, reply.cost_usd
        (folder / "map-review.json").write_text(answer.model_dump_json(indent=2), encoding="utf-8")
        if answer.verdict == SuperVerdict.APPROVE:
            job.data["approved"] += outside
            report(job, "review-map", f"approve {count_of(len(outside), 'function')}",
                   cost_usd=cost, detail=answer.summary)
            return "check-rules"
        notes = "\n".join(f"- {revision}" for revision in answer.revisions)
        functions = "\n".join(f"- `{name}`" for name in outside)
        after = send_back(job, "# The super-planner refused the functions outside the plan\n\n"
                          f"{answer.summary}\n\n## The functions\n\n{functions}\n\n"
                          f"## The revisions\n\n{notes}\n")
        report(job, "review-map", f"refuse {where_next(job, after)}", cost_usd=cost,
               detail=answer.summary)
        return after

    @graph.step("check-rules", next=("test", "implement", "undo"))
    def check_rules_step(job: Job) -> str:
        answer = ImplementerAnswer.model_validate(job.data["answer"])
        written = read_written(job_folder(job), job.data["written"])
        broken = check_rules(project, repo, answer, ratchet_files,
                             job.data.get("violations_before"), written)
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
                         project.config.test_timeout_s, coverage=coverage)
        worse = find_regressions(baseline, run.outcomes) if run.outcomes else {}
        untested = {}
        if not worse and run.outcomes:
            # An empty report means nothing was measured, such as when the project's own pytest
            # options turn coverage off, so no line can be called untested.
            if coverage and run.coverage:
                untested = find_untested_lines(repo.diff(), run.coverage, project.root, run.omit)
            if not untested:
                report(job, "test", f"passed ({count_of(len(run.outcomes), 'test')})")
                return "review"
            failure = format_untested_report(untested, folder / "pytest.log")
            what = (f"{count_of(sum(map(len, untested.values())), 'line')} not run by any test: "
                    f"{_name_lines(untested)}")
        else:
            failure = format_failure_report(worse, run, folder / "pytest.log")
            what = (f"{len(worse)} newly failing: {name_tests(list(worse))}" if worse
                    else "the test process crashed")
        (folder / "report.md").write_text(failure, encoding="utf-8")
        job.data["report"] = failure
        job.data["rejections"] = 0
        job.data["test_failures"] += 1
        after = "implement"
        if job.data["test_failures"] >= caps.test_failures:
            after = end(job, JobOutcome.ABANDONED, failure)
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
        failure = apply_comment_fixes(project, verdict, job.data["written"])
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
