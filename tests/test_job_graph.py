"""Walks every arrow of the refactor job graph with scripted agents, on a scratch project whose
tests really run."""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from ratchet.git import Repo
from ratchet.project import Project
from ratchet.pytest_runner import run_pytest
from ratchet.refactor.agent_answers import ImplementerAnswer, Plan, Review
from ratchet.refactor.job_graph import JobOutcome, RefactorError, build_job_graph, make_agents
from ratchet.refactor.settings import RefactorSettings
from rooster import Agent, JobStatus
from scripted_agents import (
    BREAK,
    GOOD,
    ScriptedAgent,
    Turn,
    done,
    edit,
    plan,
    review,
)


@pytest.fixture
def run_job(scratch: tuple[Project, Repo], monkeypatch: pytest.MonkeyPatch):
    """Returns a function that runs one job with scripted turns and returns the job and agents."""
    project, repo = scratch
    monkeypatch.chdir(project.root)
    # an edit that keeps a file's size within the same second would otherwise run stale bytecode
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    baseline = run_pytest(project.config.test_command, project.root, project.state_dir / "base",
                          project.config.test_timeout_s).outcomes
    assert baseline, "the scratch project's tests must run"

    def run(planner: list[Turn], implementer: list[Turn] = (), reviewer: list[Turn] = (),
            check_plan: Callable[[Plan], str] = lambda plan: ""):
        agents = {"planner": ScriptedAgent("planner", Plan, planner),
                  "implementer": ScriptedAgent("implementer", ImplementerAnswer, implementer),
                  "reviewer": ScriptedAgent("reviewer", Review, reviewer)}
        graph = build_job_graph(project, RefactorSettings(), agents, baseline,
                                project.state_dir / "list", check_plan)
        return graph.start("plan the next step", slug="job"), agents, graph

    return run


def app_text(project: Project) -> str:
    """Returns the text of the scratch project's src/app.py."""
    return (project.root / "src" / "app.py").read_text(encoding="utf-8")


def test_an_accepted_step_is_committed(scratch, run_job) -> None:
    """Proves plan, implement, check-rules, test, review and commit, with the plan's title."""
    project, repo = scratch

    job, agents, graph = run_job([plan()], [edit(*GOOD)], [review("accept")])

    assert job.status == JobStatus.DONE
    assert job.data["outcome"] == JobOutcome.COMMITTED
    assert repo.run("log", "-1", "--format=%s").strip() == "rename the comment in add"
    assert job.data["commit"] == repo.last_commit()
    assert job.cost_usd == 1.5
    folder = graph.state_dir / "job" / "attempt-1"
    assert {path.name for path in folder.iterdir()} >= {
        "implementer-prompt.md", "reviewer-prompt.md", "diff.patch", "checks.md", "pytest.log"}


def test_a_stop_ends_the_job_without_touching_the_code(scratch, run_job) -> None:
    """Proves a planner that finds nothing left ends the job with its reason."""
    project, repo = scratch

    job, agents, _ = run_job([plan(stop=True, reason="all done")])

    assert job.data["outcome"] == JobOutcome.STOPPED and job.data["reason"] == "all done"
    assert agents["implementer"].prompts == []


def test_a_rejection_goes_back_with_the_report_and_the_diff(scratch, run_job) -> None:
    """Proves a rejected step returns to the implementer with the findings and the diff, and is
    committed once the reviewer accepts."""
    project, repo = scratch

    job, agents, _ = run_job([plan()], [edit(*GOOD), edit("# adds a to b", "# adds b to a")],
                             [review("reject", failing=("readable",)), review("accept")])

    second = agents["implementer"].prompts[1]
    assert job.data["outcome"] == JobOutcome.COMMITTED and job.data["attempt"] == 2
    assert "This is attempt 2" in second
    assert "`src/app.py:6` readable is wrong Fix: do better" in second
    assert "+    # adds a to b" in second


def test_an_accept_with_an_unmet_check_becomes_a_reject(scratch, run_job) -> None:
    """Proves code overrules an accept when the reviewer did not confirm every check."""
    project, repo = scratch

    job, agents, _ = run_job([plan()], [edit(*GOOD), done()],
                             [review("accept", check_passes=False), review("accept")])

    assert "did not confirm checks 1" in agents["implementer"].prompts[1]
    assert job.data["outcome"] == JobOutcome.COMMITTED


def test_a_test_failure_goes_back_and_resets_the_rejections(scratch, run_job) -> None:
    """Proves broken tests return the step with the failure report, without calling the
    reviewer, and that a test failure clears the count of rejections in a row."""
    project, repo = scratch

    job, agents, _ = run_job(
        [plan()],
        [edit(*GOOD), edit(*BREAK), edit(BREAK[1], BREAK[0])],
        [review("reject", failing=("scope",)), review("accept")])

    assert "# The tests failed (1 newly failing" in agents["implementer"].prompts[2]
    assert len(agents["reviewer"].prompts) == 2
    assert job.data["test_failures"] == 1 and job.data["rejections"] == 0
    assert job.data["outcome"] == JobOutcome.COMMITTED


def test_three_test_failures_undo_the_step(scratch, run_job) -> None:
    """Proves the step is abandoned and the code put back at the test-failure cap."""
    project, repo = scratch
    start = repo.last_commit()

    job, agents, _ = run_job([plan()], [edit(*BREAK), done(), done()])

    assert job.data["outcome"] == JobOutcome.ABANDONED
    assert job.data["reason"].startswith("# The tests failed")
    assert repo.last_commit() == start and repo.changed_paths() == []


def test_three_rejections_undo_the_step(scratch, run_job) -> None:
    """Proves the step is abandoned and the code put back at the rejection cap."""
    project, repo = scratch

    job, agents, _ = run_job([plan()], [edit(*GOOD), done(), done()],
                             [review("reject", failing=("scope",))] * 3)

    assert job.data["outcome"] == JobOutcome.ABANDONED
    assert job.data["reason"].startswith("# The step was rejected")
    assert repo.changed_paths() == []


def test_a_broken_rule_goes_back_before_the_tests(scratch, run_job) -> None:
    """Proves a change to a file outside 'editable' is rejected by code, without a test run or a
    review, and the next attempt is told why."""
    project, repo = scratch

    def outside(prompt: str) -> dict:
        (project.root / "setup.cfg").write_text("[x]\n", encoding="utf-8")
        return done()

    def tidy(prompt: str) -> dict:
        (project.root / "setup.cfg").unlink()
        return edit(*GOOD)(prompt)

    job, agents, _ = run_job([plan()], [outside, tidy], [review("accept")])

    assert "setup.cfg is not among the files that a step may change" in agents["implementer"].prompts[1]
    assert not (project.root / "setup.cfg").exists()
    assert len(agents["reviewer"].prompts) == 1
    assert job.data["outcome"] == JobOutcome.COMMITTED


def test_a_blocked_step_is_undone_with_its_reason(scratch, run_job) -> None:
    """Proves a blocked answer ends the job, puts the code back and keeps the reason."""
    project, repo = scratch

    def blocked(prompt: str) -> dict:
        edit(*GOOD)(prompt)
        return done(status="blocked", blocked_reason="needs a new class")

    job, agents, _ = run_job([plan()], [blocked])

    assert job.data["outcome"] == JobOutcome.BLOCKED and job.data["reason"] == "needs a new class"
    assert repo.changed_paths() == []


def test_a_replan_is_undone_with_the_review(scratch, run_job) -> None:
    """Proves a replan verdict ends the job and keeps the review as the reason."""
    project, repo = scratch

    job, agents, _ = run_job([plan()], [edit(*GOOD)], [review("replan", failing=("necessary",))])

    assert job.data["outcome"] == JobOutcome.REPLAN
    assert "necessary is wrong" in job.data["reason"]
    assert repo.changed_paths() == []


def test_comment_fixes_are_made_and_committed(scratch, run_job) -> None:
    """Proves the reviewer's comment fixes are made by code and committed without another review."""
    project, repo = scratch
    fix = {"file": "src/app.py", "old": "# adds a to b", "new": "# adds a and b"}

    job, agents, _ = run_job([plan()], [edit(*GOOD)],
                             [review("accept_after_comment_fixes", failing=("comments",), fixes=[fix])])

    assert job.data["outcome"] == JobOutcome.COMMITTED and job.data["comment_fixes"] == 1
    assert "# adds a and b" in app_text(project)
    assert repo.changed_paths() == []


def test_comment_fixes_that_change_code_go_back_to_the_implementer(scratch, run_job) -> None:
    """Proves fixes that change code are refused, and the implementer is asked to make them."""
    project, repo = scratch
    fix = {"file": "src/app.py", "old": "return a + b", "new": "return b + a"}

    job, agents, _ = run_job(
        [plan()], [edit(*GOOD), done()],
        [review("accept_after_comment_fixes", failing=("comments",), fixes=[fix]), review("accept")])

    second = agents["implementer"].prompts[1]
    assert "could not make the comment fixes, because a fix to src/app.py changes code" in second
    assert "return a + b" in app_text(project)
    assert job.data["outcome"] == JobOutcome.COMMITTED and job.data["rejections"] == 1


def test_an_implementer_with_no_answer_halts_the_job_with_the_tree_as_before(
    scratch, run_job
) -> None:
    """Proves a failed turn halts the job, and drops the edits the turn made, so the attempt can
    run again from where it began."""
    project, repo = scratch

    def cut_off(prompt: str) -> None:
        edit(*GOOD)(prompt)
        return None

    with pytest.raises(RefactorError, match="the implementer gave no answer: timed out"):
        run_job([plan()], [cut_off])

    assert repo.changed_paths() == []


def test_a_refused_plan_halts_the_job_before_any_change(scratch, run_job) -> None:
    """Proves a plan that check_plan refuses halts the job without calling the implementer."""
    with pytest.raises(RefactorError, match="the plan was refused: P9 is not on the list"):
        run_job([plan()], check_plan=lambda plan: "P9 is not on the list")


def test_the_diagram_draws_every_arrow_of_the_table(scratch, run_job) -> None:
    """Proves the graph's arrows are the ones the plan's table lists."""
    _, _, graph = run_job([plan(stop=True)])
    text = graph.diagram()
    names = dict(re.findall(r'(s\d+)\["([^"]+)"\]', text))
    names["finish((finish))"] = "finish"
    arrows = {(names[a], names[b]) for a, b in re.findall(r"(s\d+) --> (s\d+|finish\(\(finish\)\))", text)}

    assert arrows == {
        ("plan", "implement"), ("plan", "finish"),
        ("implement", "check-rules"), ("implement", "undo"),
        ("check-rules", "test"), ("check-rules", "implement"), ("check-rules", "undo"),
        ("test", "review"), ("test", "implement"), ("test", "undo"),
        ("review", "commit"), ("review", "fix-comments"), ("review", "implement"), ("review", "undo"),
        ("fix-comments", "commit"), ("fix-comments", "implement"), ("fix-comments", "undo"),
        ("commit", "finish"), ("undo", "finish"),
    }


def test_the_real_agents_are_set_up_from_the_settings(scratch) -> None:
    """Proves the three agents get their model, budget, answer shape and permissions, without
    running them."""
    project, _ = scratch

    agents = make_agents(project, RefactorSettings(), ".ratchet/problems-20261026-09h00m.md",
                         project.state_dir / "list")

    planner, implementer, reviewer = agents["planner"], agents["implementer"], agents["reviewer"]
    assert isinstance(implementer, Agent)
    assert (planner.model, planner.max_work_usd, planner.schema) == ("opus", 3.0, Plan)
    assert (implementer.model, implementer.max_work_usd, implementer.schema) == (
        "sonnet", 8.0, ImplementerAnswer)
    assert implementer.editable == ("src/**", "tests/**")
    assert implementer.frozen == ("**/*.feature", ".ratchet/**")
    assert reviewer.editable == () and reviewer.schema is Review
    assert planner.profile == project.state_dir / "list" / "profiles" / "planner.md"
    assert ".ratchet/problems-20261026-09h00m.md" in planner.profile.read_text(encoding="utf-8")
