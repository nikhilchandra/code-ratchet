"""Checks whole refactor runs with scripted agents on a scratch project: one job after another,
the ledger carried between them, and the checks before a run starts."""

from __future__ import annotations

import re

import pytest

from ratchet.git import Repo
from ratchet.project import Project
from ratchet.refactor.agent_answers import ImplementerAnswer, Plan, Review
from ratchet.refactor.job_graph import JobOutcome, RefactorError
from ratchet.refactor.job_loop import run_refactor
from ratchet.refactor.ledger import ProblemStatus, load_ledger
from scripted_agents import GOOD, ScriptedAgent, Turn, edit, plan, review

LIST = "problems-20261026-09h00m"


@pytest.fixture
def run(scratch: tuple[Project, Repo], monkeypatch: pytest.MonkeyPatch):
    """Returns a function that runs ratchet with scripted turns and returns the run and agents."""
    project, _ = scratch
    monkeypatch.chdir(project.root)
    # an edit that keeps a file's size within the same second would otherwise run stale bytecode
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")

    def run_with(planner: list[Turn], implementer: list[Turn] = (), reviewer: list[Turn] = (),
                 **options):
        agents = {"planner": ScriptedAgent("planner", Plan, planner),
                  "implementer": ScriptedAgent("implementer", ImplementerAnswer, implementer),
                  "reviewer": ScriptedAgent("reviewer", Review, reviewer)}
        lines: list[str] = []
        result = run_refactor(project, agent_maker=lambda *args: agents, say=lines.append,
                              **options)
        return result, agents, lines

    return run_with


def ledger_of(project: Project, name: str = LIST):
    """Returns the saved ledger of a problem list."""
    return load_ledger(project.state_dir / name / "ledger.json", name)


def test_jobs_run_until_the_planner_finds_nothing_left(scratch, run) -> None:
    """Proves a run records each job in the ledger, shows the next planner the result, and ends
    when the planner stops."""
    project, repo = scratch

    result, agents, lines = run(
        [plan(), plan(stop=True, reason="all done")], [edit(*GOOD)], [review("accept")],
        max_jobs=5)

    assert result.end == "the planner found nothing left: all done"
    assert [record.outcome for record in result.jobs] == [JobOutcome.COMMITTED]
    assert result.cost_usd == 2.0
    assert ledger_of(project).status_of("P1") == ProblemStatus.DONE
    second = agents["planner"].prompts[1]
    assert "- P1: done" in second and '"rename the comment in add"' in second
    # the step's column starts after the time (7), the job (9) and the problems (7)
    steps = [line[23:36].strip() for line in lines if line[7:].startswith("job-")]
    assert steps == ["plan", "implement", "test", "review", "commit", "plan"]
    commit = next(line for line in lines if " commit " in line)
    assert re.fullmatch(
        r"\d\d:\d\d  job-001 +commit +[0-9a-f]{7} \| J01 \| job \$1\.50 \| run \$1\.50", commit)
    assert (project.state_dir / LIST / "run.log").read_text(encoding="utf-8").count("\n") == len(lines)


def test_a_run_stops_at_its_most_jobs_and_the_focus_goes_to_the_first_only(scratch, run) -> None:
    """Proves --max-jobs ends the run, and --focus is asked of the first planner call only."""
    project, _ = scratch

    result, agents, _ = run(
        [plan(), plan(problems=["P2"], completes=["P2"])], [edit(*GOOD), edit("a + b", "b + a")],
        [review("accept"), review("accept")], max_jobs=2, focus="P1")

    assert result.end == "the run reached its most jobs, 2"
    assert len(result.jobs) == 2
    assert "Plan a step for P1 next" in agents["planner"].prompts[0]
    assert "Plan a step for P1 next" not in agents["planner"].prompts[1]


def test_the_next_run_on_the_same_list_carries_on(scratch, run) -> None:
    """Proves a second run on the same list keeps the ledger and numbers its jobs after it."""
    project, _ = scratch
    run([plan()], [edit(*GOOD)], [review("accept")])

    result, agents, _ = run([plan(problems=["P2"], completes=["P2"])], [edit("a + b", "b + a")],
                            [review("accept")])

    assert result.jobs[0].id == "J02"
    assert "Jobs run on it so far: 1." in agents["planner"].prompts[0]


def test_a_new_list_starts_a_new_ledger(scratch, run) -> None:
    """Proves a newer problem list gets an empty ledger, while the old list's ledger stays."""
    project, _ = scratch
    run([plan()], [edit(*GOOD)], [review("accept")])
    newer = "problems-20261027-10h00m"
    (project.folder / f"{newer}.md").write_text("## P1. A new first problem\n", encoding="utf-8")

    result, agents, _ = run([plan()], [edit("a + b", "b + a")], [review("accept")])

    assert "None yet. Every problem on the list is open." in agents["planner"].prompts[0]
    assert f".ratchet/{newer}.md" in agents["planner"].prompts[0]
    assert ledger_of(project, newer).jobs[0].id == "J01"
    assert len(ledger_of(project).jobs) == 1


def test_a_plan_for_a_closed_problem_halts_the_run(scratch, run) -> None:
    """Proves a planner that picks a problem already done is refused before any change."""
    project, repo = scratch
    run([plan()], [edit(*GOOD)], [review("accept")])

    with pytest.raises(RefactorError, match="already closed: P1"):
        run([plan()])

    assert repo.changed_paths() == []


def test_a_job_that_halts_part_way_is_undone(scratch, run) -> None:
    """Proves a job cut off after the implementer changed code leaves the tree clean, and its
    problem open."""
    project, repo = scratch

    with pytest.raises(RefactorError, match="the reviewer gave no answer"):
        run([plan()], [edit(*GOOD)], [lambda prompt: None])

    assert repo.changed_paths() == []
    assert ledger_of(project).status_of("P1") == ProblemStatus.OPEN


def test_a_dirty_tree_is_refused(scratch, run) -> None:
    """Proves a run does not start while the tree has changes, since a commit would take them."""
    project, _ = scratch
    (project.root / "src" / "extra.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(RefactorError, match="the working tree has changes.*src/extra.py"):
        run([plan()])


def test_a_failing_suite_is_refused(scratch, run) -> None:
    """Proves a run does not start when the suite already fails, since nothing could be judged."""
    project, repo = scratch
    (project.root / "src" / "app.py").write_text("def add(a, b):\n    return 0\n", encoding="utf-8")
    repo.commit("break add")

    with pytest.raises(RefactorError, match="the suite does not pass before the run starts"):
        run([plan()])
