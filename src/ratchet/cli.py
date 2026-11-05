"""Reads the ratchet command line and runs the command it names."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rooster import AgentError, GraphError

from ratchet.agent_calls import NoAnswerError
from ratchet.git import GitError
from ratchet.init_project import InitError, init_project
from ratchet.project import ProjectError, load_project
from ratchet.refactor.job_graph import RefactorError
from ratchet.refactor.job_loop import run_refactor
from ratchet.refactor.progress_lines import count_of
from ratchet.refactor.settings import COMMAND as REFACTOR

# Every command, which are also the sections that config.toml may have.
COMMANDS = (REFACTOR,)


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
