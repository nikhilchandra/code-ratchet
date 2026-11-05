"""Reads the ratchet command line and runs the command it names."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rooster import AgentError, GraphError

from ratchet.agent_calls import NoAnswerError
from ratchet.audit.audit_graph import AuditError
from ratchet.audit.audit_run import APPROVE, DISCARD, settle_audit, start_audit
from ratchet.audit.code_copy import CopyError
from ratchet.audit.settings import COMMAND as AUDIT
from ratchet.git import GitError
from ratchet.init_project import InitError, init_project
from ratchet.problems_view import show_history, show_lists, show_problem
from ratchet.project import ProjectError, load_project
from ratchet.refactor.job_graph import RefactorError
from ratchet.refactor.job_loop import run_refactor
from ratchet.refactor.progress_lines import count_of
from ratchet.refactor.settings import COMMAND as REFACTOR
from ratchet.reopen import reopen_problem
from ratchet.target_design import TargetDesignError

# Every command, which are also the sections that config.toml may have.
COMMANDS = (AUDIT, REFACTOR)


def build_parser() -> argparse.ArgumentParser:
    """Returns the parser for ratchet's command line."""
    parser = argparse.ArgumentParser(
        prog="ratchet",
        description="Hands a coding goal to a team of agents that work in small, tested, "
                    "committed steps.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser(
        "init",
        help="set up the .ratchet folder and its records repository",
        description="Writes config.toml and rules.md from templates, makes .ratchet its own "
                    "repository, and stops the project's git from tracking it. Each step runs "
                    "only when it is still needed.",
    )
    init.add_argument("--yes", action="store_true",
                      help="agree to stop tracking .ratchet without asking")
    init.add_argument("--project", type=Path, default=Path.cwd(),
                      help="the project's folder (default: here)")
    audit = commands.add_parser(
        AUDIT,
        help="audit the code against the target design and the standard, and write a problem list",
        description="Has a team of agents audit the code and draft a problem list. "
                    "In a terminal you answer their questions and approve the draft. With no "
                    "terminal, or when you choose later, the audit waits for --approve or "
                    "--discard. A project that has a list gets a re-audit of it.",
    )
    audit.add_argument("-p", "--prompt", default="",
                       help="what to look at in this audit, in your own words")
    audit.add_argument("--fresh", action="store_true",
                       help="audit as a first audit even when a problem list exists")
    decision = audit.add_mutually_exclusive_group()
    decision.add_argument("--approve", action="store_true",
                          help="publish the draft of the audit that is waiting")
    decision.add_argument("--discard", action="store_true",
                          help="discard the draft of the audit that is waiting")
    audit.add_argument("--project", type=Path, default=Path.cwd(),
                       help="the project's folder, which holds .ratchet (default: here)")
    problems = commands.add_parser(
        "problems",
        help="show the problem lists, one problem, or a problem's history",
        description="Shows the newest problem list with each problem's status and grade. Name a "
                    "problem to see its full entry, and add --history to see what happened to "
                    "it across the lists. It costs nothing and calls no agent.",
    )
    problems.add_argument("problem", nargs="?", default="",
                          help="a problem to show in full, such as P7")
    problems.add_argument("--all", action="store_true", dest="every_list",
                          help="show every list, oldest first")
    problems.add_argument("--history", action="store_true",
                          help="show the events of the named problem, oldest first")
    problems.add_argument("--project", type=Path, default=Path.cwd(),
                          help="the project's folder (default: here)")
    reopen = commands.add_parser(
        "reopen",
        help="mark a problem on the newest list as open again",
        description="Sets a problem on the newest list back to open, records why in its history "
                    "and commits that in the records repository. It costs nothing and calls no "
                    "agent.",
    )
    reopen.add_argument("problem", help="the problem to reopen, such as P7")
    reopen.add_argument("reason", help="why it is being reopened")
    reopen.add_argument("--project", type=Path, default=Path.cwd(),
                        help="the project's folder (default: here)")
    refactor = commands.add_parser(
        REFACTOR,
        help="improve the code's structure, one tested and reviewed commit at a time",
        description="Works through the newest problem list in .ratchet, one job per step: the "
                    "planner plans it, the implementer makes it, the tests and the reviewer "
                    "judge it, and it is committed or undone.",
    )
    refactor.add_argument("--max-jobs", type=int, default=1,
                          help="the most jobs to run before stopping (default 1)")
    refactor.add_argument("--focus", default="",
                          help="a problem to ask the planner to take first, such as P19")
    refactor.add_argument("--project", type=Path, default=Path.cwd(),
                          help="the project's folder, which holds .ratchet (default: here)")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Runs the command named on the command line.

    Args:
        argv: The arguments after "ratchet". None reads them from the command line.

    Returns:
        0 when the command finished, and 1 when it stopped on an error, which is printed.
    """
    args = build_parser().parse_args(argv)
    if args.command == "init":
        return run_init(args)
    if args.command == AUDIT:
        return run_audit(args)
    if args.command == "problems":
        return run_problems(args)
    if args.command == "reopen":
        return run_reopen(args)
    try:
        project = load_project(args.project, commands=COMMANDS)
        run = run_refactor(project, max_jobs=args.max_jobs, focus=args.focus)
    except (ProjectError, RefactorError, NoAnswerError, GitError, AgentError, GraphError) as error:
        print(f"ratchet: {error}", file=sys.stderr)
        return 1
    committed = sum(1 for record in run.jobs if record.commit)
    print(f"{count_of(len(run.jobs), 'job')}, {committed} committed, "
          f"${run.cost_usd:.2f} at list price. The run ended because {run.end}.")
    return 0


def ask_owner(question: str) -> bool:
    """Asks the owner a yes or no question in the terminal.

    Args:
        question: The question to show.

    Returns:
        True when the owner answers y or yes.
    """
    return input(f"{question} Continue? [y/N] ").strip().lower() in ("y", "yes")


def run_init(args: argparse.Namespace) -> int:
    """Runs `ratchet init` and prints what it changed.

    Args:
        args: The parsed command line.

    Returns:
        0 when init finished, and 1 when it stopped on an error, which is printed.
    """
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        actions = init_project(
            args.project.resolve(), assume_yes=args.yes, confirm=ask_owner if interactive else None
        )
    except (InitError, GitError) as error:
        print(f"ratchet: {error}", file=sys.stderr)
        return 1
    for action in actions:
        print(f"ratchet: {action}")
    if not actions:
        print("ratchet: nothing to do, this project is already set up")
    return 0


def run_audit(args: argparse.Namespace) -> int:
    """Runs `ratchet audit` and prints how it ended.

    Args:
        args: The parsed command line.

    Returns:
        0 when the audit published, discarded or is waiting for the owner, and 1 when it stopped on
        an error, which is printed.
    """
    try:
        deciding = args.approve or args.discard
        if deciding and (args.prompt or args.fresh):
            raise ProjectError("--approve and --discard cannot be combined with -p or --fresh")
        project = load_project(args.project, commands=COMMANDS)
        if deciding:
            run = settle_audit(project, APPROVE if args.approve else DISCARD)
        else:
            run = start_audit(project, args.prompt, fresh=args.fresh)
    except (
        ProjectError, AuditError, CopyError, TargetDesignError, NoAnswerError, GitError,
        AgentError, GraphError,
    ) as error:
        print(f"ratchet: {error}", file=sys.stderr)
        return 1
    if run.outcome == "published":
        print(f"ratchet: {run.slug} published {run.list_name}, ${run.cost_usd:.2f} at list price")
    elif run.outcome == "discarded":
        print(f"ratchet: {run.slug} was discarded, ${run.cost_usd:.2f} at list price")
    else:
        print(f"ratchet: {run.slug} is waiting for you, ${run.cost_usd:.2f} at list price. Read "
              f"{run.draft_path}, then run `ratchet audit --approve` or `ratchet audit --discard`.")
    return 0


def run_reopen(args: argparse.Namespace) -> int:
    """Runs `ratchet reopen` and prints what it did.

    Args:
        args: The parsed command line.

    Returns:
        0 when the problem was reopened, and 1 when it stopped on an error, which is printed.
    """
    try:
        project = load_project(args.project, commands=COMMANDS)
        name = reopen_problem(project, args.problem, args.reason)
    except (ProjectError, GitError) as error:
        print(f"ratchet: {error}", file=sys.stderr)
        return 1
    print(f"ratchet: reopened {args.problem} in {name}")
    return 0


def run_problems(args: argparse.Namespace) -> int:
    """Runs `ratchet problems` and prints what it shows.

    Args:
        args: The parsed command line.

    Returns:
        0 when it showed something, and 1 when it stopped on an error, which is printed.
    """
    try:
        if args.history and not args.problem:
            raise ProjectError("--history needs a problem, such as `ratchet problems P7 --history`")
        if args.every_list and args.problem:
            raise ProjectError("--all cannot be combined with a problem")
        project = load_project(args.project, commands=COMMANDS)
        if args.problem and args.history:
            text = show_history(project, args.problem)
        elif args.problem:
            text = show_problem(project, args.problem)
        else:
            text = show_lists(project, args.every_list)
    except ProjectError as error:
        print(f"ratchet: {error}", file=sys.stderr)
        return 1
    print(text)
    return 0
