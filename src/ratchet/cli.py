"""Reads the ratchet command line and runs the command it names."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rooster import AgentError, GraphError

from ratchet.agent_calls import NoAnswerError
from ratchet.git import GitError
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
