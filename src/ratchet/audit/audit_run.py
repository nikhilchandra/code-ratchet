"""The module contains the functions that run an audit from the command line: they refuse an audit
that may not start, start the graph's job, ask the owner the questions and the approval in a
terminal when there is one, and settle a waiting audit by approving or discarding its draft.

At most one graph runs at a time, through the lock of ratchet.graph_lock. An audit that waits at a
gate keeps the lock. An audit that is waiting for approval is settled by
settle_audit(), which a later `ratchet audit --approve` or `--discard` calls.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rooster import AgentGraph, Job, JobStatus

from ratchet.audit.agent_answers import Citation
from ratchet.audit.agent_profiles import make_agents
from ratchet.audit.audit_graph import (
    NOT_BLIND_NOTE,
    AgentMaker,
    AuditError,
    build_audit_graph,
    done_ids,
)
from ratchet.audit.list_markdown import (
    Draft,
    DraftDropped,
    DraftProblem,
    DraftQuestion,
)
from ratchet.audit.settings import load_settings
from ratchet.git import Repo
from ratchet.graph_lock import (
    AUDIT,
    DRAFT_GATE,
    QUESTIONS_GATE,
    RUNNING,
    free_job_slug,
    halt_job_file,
    hold_at_gate,
    read_lock,
    release_lock,
    take_lock,
)
from ratchet.owner_terminal import (
    ask_questions,
    is_interactive,
    open_editor,
    review_draft,
)
from ratchet.project import Project
from ratchet.records import has_records
from ratchet.target_design import is_template

# The step after the questions gate, and the step after the draft gate.
AFTER_QUESTIONS = "check"
AFTER_DRAFT = "publish"

# The decisions that settle a waiting audit.
APPROVE = "approve"
DISCARD = "discard"


@dataclass
class AuditRun:
    """Holds what one call to start_audit() or settle_audit() did."""

    # The audit's name, which also names its folder under .ratchet/state/audits.
    slug: str
    # "published", "discarded" or "waiting" when the draft still needs the owner's decision.
    outcome: str
    # The name of the list that was published, or "".
    list_name: str = ""
    # What the audit has spent so far, in dollars.
    cost_usd: float = 0.0
    # The audit's draft file, or "" when the audit has not drafted yet.
    draft_path: str = ""


def _graph(project: Project, agent_maker: AgentMaker, force_first: bool = False) -> AgentGraph:
    """Returns the audit graph of a project, with the settings from its config.toml."""
    return build_audit_graph(project, load_settings(project), agent_maker, force_first)


def waiting_audit(graph: AgentGraph) -> Job | None:
    """Finds the audit that is paused at a gate.

    Args:
        graph: The audit graph.

    Returns:
        The waiting job, or None when no audit waits.
    """
    return next((job for job in graph.jobs() if job.status == JobStatus.WAITING), None)


def _load_draft(path: Path) -> Draft:
    """Reads the draft that the draft step saved.

    Args:
        path: The audit's draft.json.

    Returns:
        The draft, for the owner's summary.
    """
    data = json.loads(path.read_text(encoding="utf-8"))["draft"]
    problems = []
    for row in data["problems"]:
        evidence = [Citation.model_validate(place) for place in row.pop("evidence")]
        tests = [tuple(pair) for pair in row.pop("tests")]
        problems.append(DraftProblem(**row, evidence=evidence, tests=tests))
    return Draft(
        problems=problems,
        questions=[DraftQuestion(**row) for row in data["questions"]],
        dropped=[DraftDropped(**row) for row in data["dropped"]],
        bugs=data["bugs"],
        notes=data["notes"],
        not_blind=data.get("not_blind", ""),
    )


def _finish(job: Job) -> AuditRun:
    """Describes where an audit stands after the graph stopped advancing it.

    Args:
        job: The job as the graph returned it.

    Returns:
        The run. A job waiting at the draft gate is "waiting".

    Raises:
        AuditError: The job halted.
    """
    if job.status == JobStatus.HALTED:
        raise AuditError(f"{job.slug} halted: {job.halt_reason}")
    outcome = job.data.get("outcome", "waiting")
    return AuditRun(job.slug, outcome, job.data.get("list", ""), job.cost_usd)


def _head(project: Project) -> str:
    """Returns the commit that the project's HEAD is at."""
    return Repo(project.root).run("rev-parse", "HEAD").strip()


def _hold_at_gate(project: Project, job: Job) -> None:
    """Marks the graph lock as waiting at the gate where a job stopped.

    Args:
        project: The project.
        job: The job as the graph returned it. A job that is not waiting leaves the lock alone.
    """
    if job.status == JobStatus.WAITING:
        gate = QUESTIONS_GATE if job.next_step == AFTER_QUESTIONS else DRAFT_GATE
        hold_at_gate(project, gate)


def _resume_job(
    project: Project, graph: AgentGraph, job: Job, data: dict | None = None,
    say: Callable[[str], None] = print,
) -> Job:
    """Takes the graph lock back for a waiting job, resumes it, and holds the lock again if the
    job waits at the next gate.

    Args:
        project: The project.
        graph: The audit graph that holds the job.
        job: The job that waits.
        data: The decisions made at the gate, or None for none.
        say: The function that shows a line saying that an earlier job was rolled back.

    Returns:
        The job after it has been resumed.

    Raises:
        LockError: Another graph holds the lock.
    """
    take_lock(project, graph=AUDIT, job=job.slug, job_file=graph.state_dir / f"{job.slug}.json",
              resuming=True, say=say)
    resumed = graph.resume(job.slug, data)
    _hold_at_gate(project, resumed)
    return resumed


def _answer_questions(
    project: Project, graph: AgentGraph, job: Job, interactive: bool, ask: Callable[[str], str],
    show: Callable[[str], None],
) -> Job:
    """Resumes a job that waits at the questions gate.

    The owner's answers are passed along when there is a terminal and none when there is not.

    Args:
        project: The project that the job audits.
        graph: The audit graph that holds the job.
        job: The job that waits at the questions gate.
        interactive: Whether a terminal is available to ask the owner.
        ask: The function that reads one line of input from the owner.
        show: The function that prints a line to the owner.

    Returns:
        The job after it has been resumed.
    """
    if not interactive:
        return _resume_job(project, graph, job, say=show)
    questions = [DraftQuestion(row["id"], row["title"], row["text"])
                 for row in job.data.get("questions", [])]
    answers = ask_questions(questions, ask, show)
    return _resume_job(project, graph, job, {"answers": answers}, show)


def _approve_draft(
    project: Project, graph: AgentGraph, job: Job, interactive: bool, ask: Callable[[str], str],
    show: Callable[[str], None], edit: Callable[[Path], None],
) -> Job:
    """Asks the owner about a job that waits at the draft gate, and resumes it when they decide.

    A job stays waiting when there is no terminal or when the owner chooses later.

    Args:
        project: The project that the job audits.
        graph: The audit graph that holds the job.
        job: The job that waits at the draft gate.
        interactive: Whether a terminal is available to ask the owner.
        ask: The function that reads one line of input from the owner.
        show: The function that prints a line to the owner.
        edit: The function that opens the draft file for the owner to edit.

    Returns:
        The job after it has been resumed, or the same job when it stays waiting.
    """
    if not interactive:
        return job
    folder = graph.state_dir / job.slug
    decision = review_draft(
        _load_draft(folder / "draft.json"), folder / "draft.md", done_ids(project),
        lambda: _head(project) != job.data["head"], ask, show, edit,
    )
    return _resume_job(project, graph, job, decision, show) if decision else job


def _new_slug(graph: AgentGraph, base: str) -> str:
    """Returns the name that graph.start() gives a job started with the name 'base'.

    The name is known before the job starts, so the clean-up finds the job even when start raises.

    Args:
        graph: The audit graph.
        base: The name asked for. A counter is added when a job of that name exists.
    """
    return free_job_slug(graph.state_dir, base)


def _end_audit(project: Project, graph: AgentGraph, slug: str) -> None:
    """Closes an audit run: halts a job that was cut off, and releases the graph lock unless
    the job waits.

    A job cut off in a step, such as by an error or Ctrl-C, is saved as running, or as waiting
    when it was resumed from a gate, in which case this run's lock still says running. Ctrl-C at
    the owner's prompt leaves the job waiting and the lock waiting, so both are kept.

    Args:
        project: The project.
        graph: The audit graph.
        slug: The job's name. A job that was never saved holds nothing.
    """
    job_file = graph.state_dir / f"{slug}.json"
    held = read_lock(project)
    cut_off = held is not None and (held.job, held.state) == (slug, RUNNING)
    status = json.loads(job_file.read_text(encoding="utf-8"))["status"] if job_file.is_file() else None
    if status == JobStatus.RUNNING or (status == JobStatus.WAITING and cut_off):
        halt_job_file(job_file)
        status = JobStatus.HALTED
    if status != JobStatus.WAITING:
        release_lock(project)


def start_audit(
    project: Project,
    prompt: str = "",
    *,
    fresh: bool = False,
    agent_maker: AgentMaker = make_agents,
    interactive: bool | None = None,
    ask: Callable[[str], str] = input,
    show: Callable[[str], None] = print,
    edit: Callable[[Path], None] = open_editor,
) -> AuditRun:
    """Starts an audit and takes it as far as the owner allows.

    In a terminal the owner answers the questions and then decides on the draft. With no terminal
    the questions go to the draft unanswered, and the audit waits at the draft.

    Args:
        project: The project, as read from its .ratchet folder.
        prompt: The owner's request for the audit, or "".
        fresh: True to audit as a first audit even when the project has a problem list.
        agent_maker: The function that makes the audit's agents.
        interactive: Whether the owner can be asked. None checks the terminal.
        ask: Reads one line from the owner, given the prompt to show.
        show: Writes one line to the owner.
        edit: Opens a file for the owner to change.

    When a re-audit could not be blind, because the project's git history holds .ratchet, the note is shown.

    Returns:
        The run. Its outcome is "waiting" when the owner has yet to decide on the draft.

    Raises:
        LockError: Another graph is running, or an audit waits at a gate and holds the lock.
        AuditError: The audit may not start or it halted. An audit may not start while another
            waits, while rules.md is the template, or before `ratchet init`.
        ProjectError: The [audit] section of config.toml is wrong.
        TargetDesignError: The toml block of rules.md is malformed.
        GitError: A git command failed.
        NoAnswerError: An agent gave no answer in a step that needs it.
    """
    if interactive is None:
        interactive = is_interactive()
    settings = load_settings(project)
    graph = build_audit_graph(project, settings, agent_maker, fresh)
    slug = _new_slug(graph, datetime.now().strftime("audit-%Y%m%d-%Hh%Mm"))
    take_lock(project, graph=AUDIT, job=slug, job_file=graph.state_dir / f"{slug}.json", say=show)
    try:
        waiting = waiting_audit(graph)
        if waiting:
            raise AuditError(f"{waiting.slug} is waiting for a decision. Run `ratchet audit "
                             "--approve` or `ratchet audit --discard` first.")
        if is_template(project.rules):
            raise AuditError("rules.md still has the template marker on its first line. Answer "
                             "the essential questions, then delete that line.")
        if not has_records(project.root):
            raise AuditError("this project has not been set up yet. Run `ratchet init` first.")

        job = graph.start(prompt, slug=slug)
        _hold_at_gate(project, job)
        if job.data.get("ratchet_commit", ""):
            show(NOT_BLIND_NOTE.format(commit=job.data["ratchet_commit"]))
        if job.status == JobStatus.WAITING and job.next_step == AFTER_QUESTIONS:
            job = _answer_questions(project, graph, job, interactive, ask, show)
        if job.status == JobStatus.WAITING and job.next_step == AFTER_DRAFT:
            job = _approve_draft(project, graph, job, interactive, ask, show, edit)
        run = _finish(job)
        if run.outcome == "waiting":
            run.draft_path = str(graph.state_dir / job.slug / "draft.md")
        return run
    finally:
        # halt a job cut off mid-step, and release the lock unless it waits
        _end_audit(project, graph, slug)


def settle_audit(
    project: Project, decision: str, agent_maker: AgentMaker = make_agents
) -> AuditRun:
    """Approves or discards the draft of the audit that is waiting.

    Args:
        project: The project, as read from its .ratchet folder.
        decision: APPROVE or DISCARD.
        agent_maker: The function that makes the audit's agents.

    Returns:
        The run, published or discarded. An audit that waited at the questions gate and is
        approved goes on with no answers and stops at its draft, so the run is "waiting" with
        draft_path set. The owner reads the draft and approves again.

    Raises:
        LockError: The waiting audit cannot take its lock back, because another graph holds it.
        AuditError: No audit waits, the draft cannot be published, HEAD has moved since the audit
            started so that the list would be out of date, or the records repository is missing
            or damaged.
        ProjectError: The [audit] section of config.toml is wrong.
        GitError: A git command failed.
    """
    graph = _graph(project, agent_maker)
    job = waiting_audit(graph)
    if job is None:
        raise AuditError("no audit is waiting for a decision. Start one with `ratchet audit`.")
    if decision == APPROVE and _head(project) != job.data["head"]:
        raise AuditError("HEAD has moved since the audit started, so the list is out of date. "
                         "Discard this audit and start another.")
    if decision == APPROVE and not has_records(project.root):
        raise AuditError("this project has not been set up yet. Run `ratchet init` first.")
    try:
        if job.next_step == AFTER_QUESTIONS and decision == APPROVE:
            # an audit cut off at the questions gate goes on with no answers and stops at the draft
            run = _finish(_resume_job(project, graph, job))
            run.draft_path = str(graph.state_dir / job.slug / "draft.md")
            return run
        return _finish(_resume_job(project, graph, job, {"decision": decision}))
    finally:
        # halt a job cut off mid-step, and release the lock unless it waits
        _end_audit(project, graph, job.slug)
