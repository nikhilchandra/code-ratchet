"""Runs refactor jobs one after another on the newest problem list, keeping its ledger.

A run first checks the project: the working tree must be clean, and the whole suite must pass,
since that run becomes the baseline that every job's tests are compared with. It then starts one
job after another, recording each in the ledger, until the planner finds nothing left, the run
reaches its most jobs, or a job halts.

Everything a run makes goes in the problem list's own folder, .ratchet/state/<list name>/: the
ledger, the run's log, the baseline, the agents' profiles and logs, and every job.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from rooster import Agent, Job, JobStatus

from ratchet.git import GitError, Repo
from ratchet.problem_history import publish_time
from ratchet.project import Project
from ratchet.pytest_runner import OutcomeKind, has_pytest_cov, run_pytest
from ratchet.records import commit_records, has_records
from ratchet.refactor.job_graph import JobOutcome, RefactorError, build_job_graph, make_agents
from ratchet.refactor.ledger import JobRecord, load_ledger, record_job, save_ledger
from ratchet.refactor.problem_list import find_latest_problem_list
from ratchet.refactor.progress_lines import console_width, count_of, format_step_lines
from ratchet.refactor.settings import RefactorSettings, load_settings

# A function that makes the five agents, with the arguments of make_agents().
AgentMaker = Callable[[Project, RefactorSettings, str, Path], dict[str, Agent]]

# The width of the time that starts each console line, such as "14:25  ".
TIME_WIDTH = 7

# The number of jobs committed on a list from which a run suggests a re-audit.
REAUDIT_JOBS = 3


@dataclass
class RefactorRun:
    """Holds what one run did."""

    # The jobs that the run recorded in the ledger, in order.
    jobs: list[JobRecord] = field(default_factory=list)
    # Why the run ended, as one sentence.
    end: str = ""
    # What the run spent, in dollars, including a planner call that found nothing left.
    cost_usd: float = 0.0
    # The short hash of the records commit that took the run's ledger, or "" when there was none.
    records_commit: str = ""
    # The commits that landed in the project since the problem list was published.
    commits_since_list: int = 0
    # The jobs committed on the problem list, by this run and every earlier one.
    jobs_committed_on_list: int = 0


def run_refactor(
    project: Project,
    *,
    max_jobs: int = 1,
    focus: str = "",
    agent_maker: AgentMaker = make_agents,
    say: Callable[[str], None] = print,
    width: int | None = None,
) -> RefactorRun:
    """Runs jobs on the newest problem list until nothing is left or 'max_jobs' have run.

    Each step of a job shows one line, and each job ends with a line that says whether it was
    committed or undone, and what it and the run have spent.

    Args:
        project: The project, as read from its .ratchet folder.
        max_jobs: The most jobs to run.
        focus: A problem to ask the planner to take first, such as "P19", or "".
        agent_maker: The function that makes the five agents.
        say: The function that shows each line of progress. Every line also goes to the run's
            log.
        width: The console's width in characters, which the lines are fitted to. None measures
            the console.

    Returns:
        The jobs that ran, why the run ended, what it spent, the short hash of the records
        commit that took the ledger ("" when .ratchet is not its own repository), and the number
        of commits since the problem list was published and the number of jobs committed on it.

    Raises:
        ProjectError: The .ratchet folder or its problem list is wrong.
        GitError: The records commit failed, or git could not count the commits since the list.
        RefactorError: The working tree is not clean, the suite does not pass before the run,
            or a job halted.
        NoAnswerError: An agent gave no answer during a job. As with a halted job, the job's
            changes are undone first, so its problems stay open.
    """
    settings = load_settings(project)
    problem_list = find_latest_problem_list(project)
    folder = project.state_dir / problem_list.name
    folder.mkdir(parents=True, exist_ok=True)
    width = width or console_width()

    def log(*lines: str) -> None:
        """Shows lines of progress, the first after the time, and adds them to the run's log."""
        now = datetime.now()
        stamp = now.strftime("%Y-%m-%d %H:%M:%S  ")
        say(now.strftime("%H:%M  ") + lines[0])
        for line in lines[1:]:
            say(" " * TIME_WIDTH + line)
        with (folder / "run.log").open("a", encoding="utf-8") as file:
            file.write(stamp + lines[0] + "\n")
            for line in lines[1:]:
                file.write(" " * len(stamp) + line + "\n")

    def report(job: Job, step: str, text: str, **extras: object) -> None:
        """Shows what one step of a job did, as one aligned line and its detail."""
        log(*format_step_lines(job.slug, step, text, width=width, indent=TIME_WIDTH, **extras))

    def end_run(run: RefactorRun) -> RefactorRun:
        """Commits the ledger to the records repository, then says how old the list is.

        The last line suggests `ratchet audit` once the ledger holds REAUDIT_JOBS or more
        committed jobs. The ledger spans every run on the list, and abandoned jobs do not count.
        """
        run.records_commit = save_records(run)
        run.commits_since_list = commits_since(
            repo, publish_time(project, problem_list.name, problem_list.created))
        # count the committed jobs in the ledger, which holds earlier runs as well
        run.jobs_committed_on_list = sum(1 for job in ledger.jobs if job.outcome == JobOutcome.COMMITTED)
        text = f"{count_of(run.commits_since_list, 'commit')} since {problem_list.name} was published"
        if run.jobs_committed_on_list >= REAUDIT_JOBS:
            text += f", {count_of(run.jobs_committed_on_list, 'job')} committed on it, consider `ratchet audit`"
        log(text)
        return run

    def save_records(run: RefactorRun) -> str:
        """Commits the ledger and the other tracked records, when the records repository exists."""
        if not has_records(project.root):
            return ""
        return commit_records(project.root, f"refactor: {count_of(len(run.jobs), 'job')} on "
                                            f"{problem_list.name}") or ""

    repo = Repo(project.root)
    changed = repo.changed_paths()
    if changed:
        raise RefactorError(f"the working tree has changes, so a job's commit would take them: "
                            f"{', '.join(changed[:10])}")

    log(f"run started on {repo.last_commit()}, with {problem_list.relative_path}")
    if not has_records(project.root):
        log("records are not committed: run `ratchet init` to set up .ratchet as its own repository")
    baseline = run_pytest(project.config.test_command, project.root, folder / "baseline",
                          project.config.test_timeout_s)
    failing = [test_id for test_id, outcome in baseline.outcomes.items()
               if outcome.kind in (OutcomeKind.FAILED, OutcomeKind.ERROR)]
    if not baseline.outcomes or failing:
        raise RefactorError(f"the suite does not pass before the run starts (pytest exit code "
                            f"{baseline.code}), see {folder / 'baseline' / 'pytest.log'}")
    log(f"baseline: {count_of(len(baseline.outcomes), 'test')}")
    covered = has_pytest_cov(project.config.test_command, project.root, project.config.test_timeout_s)
    if not covered:
        log("coverage is not checked: pytest-cov does not load in the project's test environment")

    ledger_path = folder / "ledger.json"
    ledger = load_ledger(ledger_path, problem_list.name)
    agents = agent_maker(project, settings, problem_list.relative_path, folder)
    graph = build_job_graph(project, settings, agents, baseline.outcomes, folder, ledger,
                            ledger_path, problem_list, report=report, coverage=covered)

    run = RefactorRun()
    for number in range(max_jobs):
        slug = f"job-{ledger.next_job_number():03d}"
        try:
            job = graph.start(focus if number == 0 else "", slug=slug)
            if job.status == JobStatus.HALTED:
                raise RefactorError(f"{job.slug} halted: {job.halt_reason}")
        except Exception:
            # a job cut off part way leaves no half-made change for the next run
            if repo.changed_paths():
                repo.undo()
                log(*format_step_lines(slug, "undo", "halted, so its problems are still open",
                                       width=width, indent=TIME_WIDTH))
            # commit the records, ignoring a git error
            with contextlib.suppress(GitError):
                save_records(run)
            raise
        run.cost_usd += job.cost_usd

        if job.data["outcome"] == JobOutcome.STOPPED:
            run.end = f"the planner found nothing left: {job.data['reason']}"
            log(run.end)
            return end_run(run)
        record = record_job(ledger, job.data, job.slug, job.cost_usd, settings.caps)
        save_ledger(ledger, ledger_path)
        run.jobs.append(record)
        spent = f"job ${record.cost_usd:.2f} | run ${run.cost_usd:.2f}"
        if record.commit:
            log(*format_step_lines(job.slug, "commit", f"{record.commit} | {record.id} | {spent}",
                                   width=width, indent=TIME_WIDTH))
        else:
            log(*format_step_lines(job.slug, "undo", f"{record.outcome} | {record.id} | {spent}",
                                   width=width, indent=TIME_WIDTH))

    run.end = f"the run reached its most jobs, {max_jobs}"
    log(run.end)
    return end_run(run)


def commits_since(repo: Repo, when: str) -> int:
    """Counts the commits that landed in a project after a time.

    Args:
        repo: The project's git helper.
        when: The time, such as "2026-11-02 14:05".

    Returns:
        The number of commits reachable from HEAD that are newer than 'when'.

    Raises:
        GitError: Git could not count the commits.
    """
    return int(repo.run("rev-list", "--count", f"--since={when}", "HEAD").strip() or 0)
