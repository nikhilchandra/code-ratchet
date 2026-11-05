"""Walks every arrow of the refactor job graph with scripted agents, on a scratch project whose
tests really run."""

from __future__ import annotations

import contextlib
import json
import re

import pytest

from ratchet.agent_calls import NoAnswerError
from ratchet.git import Repo
from ratchet.pathways import build_map, describe
from ratchet.project import Project
from ratchet.pytest_runner import run_pytest
from ratchet.refactor import agent_answers
from ratchet.refactor.agent_answers import (
    ImplementerAnswer,
    Plan,
    Review,
    SuperPlan,
    WriterAnswer,
)
from ratchet.refactor.job_graph import JobOutcome, RefactorError, build_job_graph, make_agents
from ratchet.refactor.ledger import (
    Ledger,
    ProblemEvent,
    ProblemRecord,
    ProblemStatus,
    load_ledger,
    record_job,
    save_ledger,
)
from ratchet.refactor.problem_list import find_latest_problem_list
from ratchet.refactor.settings import Caps, RefactorSettings
from rooster import Agent, JobStatus
from conftest import APP
from scripted_agents import (
    BREAK,
    GOOD,
    SHOP_CLI,
    ScriptedAgent,
    Turn,
    add_app_sub,
    add_shop_sub,
    approve,
    commit_shop,
    done,
    edit,
    map_approve,
    map_revise,
    plan,
    review,
    revise,
    wanted,
    write_sub_test,
    write_test,
)

# A test that the test writer writes, which passes on the scratch project's code, and one that
# fails on it.
PASSING = "from app import add\n\n\ndef test_more():\n    # two and two\n    assert add(2, 2) == 4\n"
FAILING = "from app import add\n\n\ndef test_more():\n    assert add(2, 2) == 5\n"


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

    problem_list = find_latest_problem_list(project)

    def run(planner: list[Turn], implementer: list[Turn] = (), reviewer: list[Turn] = (),
            super_planner: list[Turn] | None = None, test_writer: list[Turn] = (),
            ledger: Ledger | None = None, focus: str = "", reports: list | None = None,
            coverage: bool = False, seen: dict | None = None):
        # a super-planner left out approves every plan and asks for no tests
        if super_planner is None:
            super_planner = [approve()] * len(planner)
        agents = {"planner": ScriptedAgent("planner", Plan, planner),
                  "super_planner": ScriptedAgent("super_planner", SuperPlan, super_planner),
                  "test_writer": ScriptedAgent("test_writer", WriterAnswer, test_writer),
                  "implementer": ScriptedAgent("implementer", ImplementerAnswer, implementer),
                  "reviewer": ScriptedAgent("reviewer", Review, reviewer)}
        # a dict given as 'seen' gets the agents, so a test can read them after the job raises
        if seen is not None:
            seen.update(agents)

        def report(job, step: str, text: str, **extras) -> None:
            if reports is not None:
                reports.append((step, text, extras))

        graph = build_job_graph(project, RefactorSettings(), agents, baseline,
                                project.state_dir / "list",
                                ledger or Ledger(problem_list=problem_list.name),
                                project.state_dir / "list" / "ledger.json", problem_list, report,
                                coverage=coverage)
        return graph.start(focus, slug="job"), agents, graph

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
    assert job.cost_usd == 2.0
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

    with pytest.raises(NoAnswerError, match="the implementer gave no answer: timed out"):
        run_job([plan()], [cut_off])

    assert repo.changed_paths() == []


def test_a_refused_plan_halts_the_job_before_any_change(scratch, run_job) -> None:
    """Proves a plan that the ledger refuses halts the job without calling the implementer."""
    match = "the plan was refused: it names problems that the list does not hold: P9"
    with pytest.raises(RefactorError, match=match):
        run_job([plan(problems=["P9"])])


def test_the_planner_sees_the_statuses_and_only_the_last_ten_jobs(scratch, run_job) -> None:
    """Proves the planner's prompt lists each touched problem's status and the latest ten jobs,
    and leaves out the older ones."""
    project, _ = scratch
    ledger = Ledger(problem_list=find_latest_problem_list(project).name)
    for number in range(12):
        data = {"plan": plan(problems=[f"P{number}"], step_title=f"step {number}"),
                "outcome": JobOutcome.BLOCKED, "reason": "it was blocked", "attempt": 1}
        record_job(ledger, data, f"job-{number}", 1.0, Caps())

    _, agents, _ = run_job([plan(stop=True)], ledger=ledger)

    prompt = agents["planner"].prompts[0]
    assert "The problem list is `.ratchet/problems-20261026-09h00m.md`." in prompt
    assert "Jobs run on it so far: 12." in prompt
    assert "- P0: open, sent back 1 time\n" in prompt
    assert "## The last 10 jobs" in prompt
    assert '"step 2"' in prompt and '"step 1"' not in prompt
    assert "cost_usd" not in prompt and "A request" not in prompt


def test_a_halted_job_leaves_job_started_in_the_saved_ledger(scratch, run_job) -> None:
    """Proves the plan step records job_started for each problem and saves the ledger, so a job
    that halts afterward still leaves a trace."""
    project, _ = scratch
    ledger = Ledger(problem_list=find_latest_problem_list(project).name)

    with pytest.raises(NoAnswerError):
        run_job([plan()], [lambda prompt: None], ledger=ledger)

    saved = load_ledger(project.state_dir / "list" / "ledger.json", ledger.problem_list)
    event = saved.problems["P1"].events[0]
    assert (event.kind, event.by, event.job) == ("job_started", "J01", "J01")
    assert event.note == "rename the comment in add"
    assert saved.status_of("P1") == "open"


def test_a_refused_plan_records_no_job_started(scratch, run_job) -> None:
    """Proves a plan that the ledger refuses leaves no event and no saved ledger."""
    project, _ = scratch

    with pytest.raises(RefactorError):
        run_job([plan(problems=["P9"])])

    assert not (project.state_dir / "list" / "ledger.json").exists()


def test_the_planner_sees_a_reopen_and_only_the_last_three_job_events(scratch, run_job) -> None:
    """Proves the prompt shows a problem's reopened event and its last three job events, and
    leaves out older job events."""
    project, _ = scratch
    ledger = Ledger(problem_list=find_latest_problem_list(project).name)
    events = [ProblemEvent(when="2026-11-01 09:00", kind="reopened", by="owner",
                           note="strip repeats the vote")]
    for number in range(1, 5):
        events.append(ProblemEvent(when=f"2026-11-02 1{number}:00", kind="job_started",
                                   by=f"J0{number}", job=f"J0{number}", note=f"try {number}"))
    ledger.problems["P1"] = ProblemRecord(events=events)
    save_ledger(ledger, project.state_dir / ledger.problem_list / "ledger.json")

    _, agents, _ = run_job([plan(stop=True)], ledger=ledger)

    prompt = agents["planner"].prompts[0]
    assert "reopened by owner: strip repeats the vote" in prompt
    assert "job_started by J04: try 4" in prompt and "try 2" in prompt
    assert "try 1" not in prompt


def test_an_empty_ledger_says_everything_is_open_and_carries_the_focus(run_job) -> None:
    """Proves the first job's planner prompt says no problem is touched yet, and passes on the
    focus."""
    _, agents, _ = run_job([plan(stop=True)], focus="P2")

    prompt = agents["planner"].prompts[0]
    assert "None yet. Every problem on the list is open." in prompt
    assert "Plan a step for P2 next, if the rules for ordering allow it." in prompt


def test_each_step_reports_what_happened(scratch, run_job) -> None:
    """Proves a smooth job reports its plan's summary and problems, each attempt, the tests and
    the verdict, with each agent call's cost."""
    reports: list = []

    run_job([plan()], [edit(*GOOD)], [review("accept")], reports=reports)

    assert [(step, text) for step, text, _ in reports] == [
        ("plan", "rename the comment in add"), ("super-plan", "approve, no tests to write"),
        ("implement", "attempt 1"), ("test", "passed (1 test)"), ("review", "accept")]
    assert reports[0][2] == {"problems": "P1", "cost_usd": 0.5}
    assert reports[1][2] == {"cost_usd": 0.5}
    assert reports[3][2] == {}


def test_each_failure_reports_its_culprit_and_where_the_job_goes(scratch, run_job) -> None:
    """Proves a test failure names the failing test, a broken rule and a rejection say why, and
    comment fixes are counted, each with the attempt that follows."""
    reports: list = []
    fix = {"file": "src/app.py", "old": "# adds a to b", "new": "# adds a and b"}

    run_job([plan()],
            [edit(*BREAK), edit(BREAK[1], BREAK[0]), edit(*GOOD), done()],
            [review("reject", failing=("scope",), check_passes=False),
             review("accept_after_comment_fixes", failing=("comments",), fixes=[fix])],
            reports=reports)

    lines = [(step, text) for step, text, _ in reports]
    assert ("test", "1 newly failing: test_app::test_add -> attempt 2") in lines
    assert ("check-rules", "broke 1 rule -> attempt 3") in lines
    assert ("review", "reject: scope; check 1 unmet -> attempt 4") in lines
    assert ("review", "accept after 1 comment fix") in lines
    assert lines[-1] == ("fix-comments", "1 fix made")
    details = {step: extras.get("detail") for step, _, extras in reports if extras.get("detail")}
    assert details == {"check-rules": "the implementer says that it is done, but nothing changed",
                       "review": "the verdict is reject"}


def test_an_overruled_accept_and_a_cap_say_so(scratch, run_job) -> None:
    """Proves an accept with an unmet check is reported as overruled, and the last failure
    names the cap that undoes the step."""
    reports: list = []

    run_job([plan()], [edit(*GOOD), done(), done()],
            [review("accept", check_passes=False), review("reject", failing=("scope",)),
             review("reject", failing=("scope",))], reports=reports)

    texts = [text for step, text, _ in reports if step == "review"]
    assert texts[0] == "reject: check 1 unmet; overruling the reviewer's accept -> attempt 2"
    assert texts[-1] == "reject: scope -> undo, at the cap of 3 rejections"


def test_a_blocked_attempt_reports_its_reason(scratch, run_job) -> None:
    """Proves a blocked answer is reported with the implementer's reason beneath it."""
    reports: list = []

    run_job([plan()], [done(status="blocked", blocked_reason="needs a new class")], reports=reports)

    assert reports[-1][:2] == ("implement", "attempt 1 blocked -> undo")
    assert reports[-1][2]["detail"] == "needs a new class"


def test_the_diagram_draws_every_arrow_of_the_table(scratch, run_job) -> None:
    """Proves the graph's arrows are the ones the plan's table lists."""
    _, _, graph = run_job([plan(stop=True)])
    text = graph.diagram()
    names = dict(re.findall(r'(s\d+)\["([^"]+)"\]', text))
    names["finish((finish))"] = "finish"
    arrows = {(names[a], names[b]) for a, b in re.findall(r"(s\d+) --> (s\d+|finish\(\(finish\)\))", text)}

    assert arrows == {
        ("plan", "super-plan"), ("plan", "undo"), ("plan", "finish"),
        ("super-plan", "plan"), ("super-plan", "write-tests"), ("super-plan", "implement"),
        ("super-plan", "undo"),
        ("write-tests", "write-tests"), ("write-tests", "implement"), ("write-tests", "undo"),
        ("implement", "check-map"), ("implement", "undo"),
        ("check-map", "check-rules"), ("check-map", "review-map"),
        ("review-map", "check-rules"), ("review-map", "implement"), ("review-map", "undo"),
        ("check-rules", "test"), ("check-rules", "implement"), ("check-rules", "undo"),
        ("test", "review"), ("test", "implement"), ("test", "undo"),
        ("review", "commit"), ("review", "fix-comments"), ("review", "implement"), ("review", "undo"),
        ("fix-comments", "commit"), ("fix-comments", "implement"), ("fix-comments", "undo"),
        ("commit", "finish"), ("undo", "finish"),
    }


def test_the_real_agents_are_set_up_from_the_settings(scratch) -> None:
    """Proves the five agents get their model, budget, answer shape and permissions, without
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
    super_planner, test_writer = agents["super_planner"], agents["test_writer"]
    assert set(agents) == {"planner", "super_planner", "test_writer", "implementer", "reviewer"}
    assert (super_planner.model, super_planner.max_work_usd, super_planner.schema) == (
        "opus", 3.0, SuperPlan)
    assert super_planner.editable == ()
    assert "Keep it small." in super_planner.profile.read_text(encoding="utf-8")
    assert (test_writer.model, test_writer.max_work_usd, test_writer.schema) == (
        "sonnet", 5.0, WriterAnswer)
    assert test_writer.editable == ("src/**", "tests/**")
    assert test_writer.frozen == ("**/*.feature", ".ratchet/**")


def lines_of(reports: list, step: str) -> list[str]:
    """Returns what each report of one step said, in order."""
    return [text for name, text, _ in reports if name == step]


def new_ledger(project: Project) -> Ledger:
    """Returns an empty ledger of the scratch project's problem list."""
    return Ledger(problem_list=find_latest_problem_list(project).name)


def test_the_super_planner_sees_the_plan_the_pathway_map_and_the_rules(scratch, run_job) -> None:
    """Proves code builds the pathway map of the plan's functions and gives it to the
    super-planner with the plan and rules.md, and that an approval with no tests to write goes
    straight to the implementer."""
    project, repo = scratch
    package = project.root / "src" / "shop"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(
        '"""Totals a basket."""\n\n\ndef total(prices):\n    """Returns the sum."""\n'
        '    return sum(prices)\n\n\ndef main():\n    """Shows the total."""\n'
        '    print(total([1, 2]))\n', encoding="utf-8", newline="\n")
    repo.commit("add a shop")
    expected = json.dumps(describe(build_map(project.root), ["shop.cli.total"]), indent=2)

    job, agents, _ = run_job([plan(functions=["shop.cli.total"])], [edit(*GOOD)],
                             [review("accept")])

    prompt = agents["super_planner"].prompts[0]
    assert '"shop.cli.total"' in expected and expected in prompt
    assert json.dumps(job.data["plan"], indent=2) in prompt
    assert "Keep it small." in prompt
    assert job.data["outcome"] == JobOutcome.COMMITTED
    assert agents["test_writer"].prompts == []


@pytest.mark.parametrize("functions", [["nowhere.at_all"], []])
def test_a_super_plan_for_an_unknown_function_still_runs(scratch, run_job, functions) -> None:
    """Proves a plan whose functions the pathway map does not hold, or that names none, still
    reaches the super-planner and the commit."""
    job, agents, _ = run_job([plan(functions=functions)], [edit(*GOOD)], [review("accept")])

    prompts = agents["super_planner"].prompts
    assert len(prompts) == 1 and all(name in prompts[0] for name in functions)
    assert job.data["outcome"] == JobOutcome.COMMITTED


def test_a_revise_sends_the_job_back_to_plan_with_the_notes(scratch, run_job) -> None:
    """Proves a revise asks the planner again with the earlier plan and the revisions, and that
    the planning rounds share one job id and one job_started event."""
    project, _ = scratch
    ledger = new_ledger(project)

    job, agents, _ = run_job([plan(step_title="first try at the comment"), plan()],
                             [edit(*GOOD)], [review("accept")],
                             super_planner=[revise("name what add returns"), approve()],
                             ledger=ledger)

    second = agents["planner"].prompts[1]
    assert '"first try at the comment"' in second
    assert "name what add returns" in second
    assert job.data["outcome"] == JobOutcome.COMMITTED
    assert record_job(ledger, job.data, job.slug, job.cost_usd, Caps()).id == "J01"
    kinds = [event.kind for event in ledger.problems["P1"].events]
    assert kinds.count("job_started") == 1


def test_the_third_revise_abandons_the_job(scratch, run_job) -> None:
    """Proves the revise after the cap of two abandons the job before the implementer is asked,
    and says so."""
    project, repo = scratch
    ledger = new_ledger(project)
    reports: list = []

    job, agents, _ = run_job([plan()] * 3, super_planner=[revise("rename it")] * 3,
                             ledger=ledger, reports=reports)

    assert job.data["outcome"] == JobOutcome.ABANDONED
    assert len(agents["super_planner"].prompts) == 3
    assert agents["implementer"].prompts == []
    assert lines_of(reports, "super-plan")[-1] == "revise -> undo, at the cap of 2 revisions"
    assert repo.changed_paths() == []
    record_job(ledger, job.data, job.slug, job.cost_usd, Caps())
    assert ledger.problems["P1"].events[-1].kind == "abandoned"


def test_a_stop_after_a_revise_keeps_the_last_plan(scratch, run_job) -> None:
    """Proves a planner that stops after a revise abandons the job, and that the job keeps the
    plan the super-planner last saw, so the ledger records its title and problems."""
    project, _ = scratch
    ledger = new_ledger(project)
    stop = plan(stop=True, reason="nothing", step_title="", summary="", problems=[],
                completes=[], functions=[])

    job, agents, _ = run_job([plan(), stop], super_planner=[revise("rename it")], ledger=ledger)

    assert job.data["outcome"] == JobOutcome.ABANDONED
    assert job.data["plan"]["step_title"] == "rename the comment in add"
    record = record_job(ledger, job.data, job.slug, job.cost_usd, Caps())
    assert (record.title, record.problems) == ("rename the comment in add", ["P1"])
    assert ledger.problems["P1"].events[-1].kind == "abandoned"


def test_a_revised_plan_that_drops_a_problem_sends_it_back(scratch, run_job) -> None:
    """Proves a problem that an earlier planning round started, and the revised plan dropped,
    gets a sent_back event and stays open."""
    project, _ = scratch
    ledger = new_ledger(project)

    job, _, _ = run_job([plan(problems=["P1", "P2"], completes=["P1"]), plan()],
                        [edit(*GOOD)], [review("accept")],
                        super_planner=[revise("leave P2 for later"), approve()], ledger=ledger)
    record_job(ledger, job.data, job.slug, job.cost_usd, Caps())

    assert job.data["outcome"] == JobOutcome.COMMITTED
    assert ledger.status_of("P1") == ProblemStatus.DONE
    dropped = ledger.problems["P2"]
    assert dropped.events[-1].kind == "sent_back" and dropped.sendbacks == 1
    assert ledger.status_of("P2") == ProblemStatus.OPEN
    kinds = [event.kind for event in ledger.problems["P1"].events]
    assert kinds.count("job_started") == 1


def written_job(run_job, implementer: list[Turn], reviewer: list[Turn], **options):
    """Runs one job whose super-planner asks for tests/test_more.py and whose test writer
    writes a passing test there."""
    return run_job([plan()], implementer, reviewer, super_planner=[approve(wanted())],
                   test_writer=[write_test("tests/test_more.py", PASSING)], **options)


def test_written_tests_pass_freeze_and_are_committed(scratch, run_job) -> None:
    """Proves the test writer gets the tests asked for, its passing test is frozen for the
    implementer and named in its prompt, and the test is committed with the step."""
    project, repo = scratch
    reports: list = []

    job, agents, _ = written_job(run_job, [edit(*GOOD)], [review("accept")], reports=reports)

    implementer = agents["implementer"]
    assert "add(2, 2) is 4" in agents["test_writer"].prompts[0]
    assert implementer.frozen_seen == [("tests/test_more.py",)]
    assert "tests/test_more.py" in implementer.prompts[0]
    assert job.data["outcome"] == JobOutcome.COMMITTED
    assert "tests/test_more.py" in repo.run("ls-files").splitlines()
    assert lines_of(reports, "write-tests") == ["1 file, 1 test passed"]


@pytest.fixture
def folder_command(scratch: tuple[Project, Repo]) -> None:
    """Makes the test command name the test folder, which holds one committed skipped test.

    It is set up before the run_job fixture, so that the baseline holds the skipped test."""
    project, repo = scratch
    (project.root / "tests" / "test_old.py").write_text(
        "import pytest\n\n\n@pytest.mark.skip\ndef test_old():\n    pass\n", encoding="utf-8")
    repo.run("add", "-A")
    repo.commit("add a skipped test")
    project.config.test_command = [*project.config.test_command, "tests"]


def test_a_skipped_old_test_is_not_counted_against_the_written_tests(
        folder_command, run_job) -> None:
    """Proves that, when the test command names the test folder, only the written tests count:
    an old test that is skipped does not refuse them, and they are counted alone."""
    reports: list = []

    job, _, _ = written_job(run_job, [edit(*GOOD)], [review("accept")], reports=reports)

    assert job.data["outcome"] == JobOutcome.COMMITTED
    assert lines_of(reports, "write-tests") == ["1 file, 1 test passed"]


@pytest.fixture
def tests_ini(scratch: tuple[Project, Repo]) -> None:
    """Commits a tests/pytest.ini, so that pytest takes tests/ as its rootdir when a run names a
    file in it.

    The ini's pythonpath is read from the ini's own folder, so that 'from app import add' still
    imports. It is set up before the run_job fixture, so that the baseline is run with it."""
    project, repo = scratch
    (project.root / "tests" / "pytest.ini").write_text("[pytest]\npythonpath = ../src\n",
                                                       encoding="utf-8")
    repo.run("add", "-A")
    repo.commit("add a pytest.ini to the test folder")


def test_written_tests_under_their_own_pytest_ini_are_collected(tests_ini, run_job) -> None:
    """Proves written tests in a folder with its own pytest.ini are found in the run of their
    file, and are committed with the step."""
    reports: list = []

    job, _, _ = run_job([plan()], [edit(*GOOD)], [review("accept")],
                        super_planner=[approve(wanted())],
                        test_writer=[write_test("tests/test_more.py", PASSING)] * 3,
                        reports=reports)

    assert lines_of(reports, "write-tests") == ["1 file, 1 test passed"]
    assert job.data["outcome"] == JobOutcome.COMMITTED


def test_a_written_file_with_no_test_in_it_is_refused(folder_command, run_job) -> None:
    """Proves a written file that holds no test is refused, rather than passing on the tests
    that the run found elsewhere."""
    reports: list = []

    run_job([plan()], [], [], super_planner=[approve(wanted())],
            test_writer=[write_test("tests/test_more.py", "VALUE = 1\n")] * 3, reports=reports)

    assert "none of the written tests were collected" in lines_of(reports, "write-tests")[0]


def test_a_written_test_that_kills_the_test_process_is_refused(run_job) -> None:
    """Proves a written test that ends the test process, so that no outcome is read, is refused
    as a crash."""
    reports: list = []

    run_job([plan()], [], [], super_planner=[approve(wanted())],
            test_writer=[write_test("tests/test_more.py",
                                    "import os\n\nos._exit(3)\n")] * 3, reports=reports)

    assert "the test process crashed" in lines_of(reports, "write-tests")[0]


UNIMPORTABLE = "import nonexistent_mod\n\n\ndef test_a():\n    pass\n"


def test_a_written_test_that_cannot_be_imported_goes_back_with_the_error(scratch, run_job) -> None:
    """Proves a written file that fails to import is refused as failing, and the test writer's
    next prompt holds the import error."""
    def second(prompt: str) -> dict:
        assert "nonexistent_mod" in prompt
        return write_test("tests/test_more.py", PASSING)(prompt)

    reports: list = []
    job, _, _ = run_job(
        [plan()], [edit(*GOOD)], [review("accept")], super_planner=[approve(wanted())],
        test_writer=[write_test("tests/test_more.py", UNIMPORTABLE), second], reports=reports)

    assert "1 failing on the code as it is" in lines_of(reports, "write-tests")[0]
    assert job.data["outcome"] == JobOutcome.COMMITTED


def test_the_next_job_gets_the_implementers_own_frozen_paths_back(scratch, run_job) -> None:
    """Proves the written tests are frozen only for their own job, so a later job's
    implementer sees its own frozen paths again."""
    project, _ = scratch

    job, agents, graph = run_job(
        [plan(), plan(problems=["P2"], completes=["P2"])],
        [edit(*GOOD), edit("a + b", "b + a")], [review("accept"), review("accept")],
        super_planner=[approve(wanted()), approve()],
        test_writer=[write_test("tests/test_more.py", PASSING)])
    second = graph.start("", slug="job-2")

    assert job.data["outcome"] == second.data["outcome"] == JobOutcome.COMMITTED
    assert agents["implementer"].frozen_seen == [("tests/test_more.py",), ()]


def test_the_reviewer_is_told_which_files_the_test_writer_wrote(scratch, run_job) -> None:
    """Proves the reviewer's prompt names the written tests as the test writer's, frozen for
    the step, apart from the diff that holds them."""
    job, agents, _ = written_job(run_job, [edit(*GOOD)], [review("accept")])

    prompt = agents["reviewer"].prompts[0]
    named = [line for line in prompt.splitlines() if "tests/test_more.py" in line
             and not line.startswith(("diff --git", "+++", "---"))]
    assert named
    assert "test writer" in prompt.lower() and "frozen" in prompt.lower()


def test_a_comment_fix_in_a_written_test_is_refused(scratch, run_job) -> None:
    """Proves a reviewer's comment fix to a written test is not made, and the step goes back to
    the implementer, so the test is committed as the test writer left it."""
    project, repo = scratch
    fix = {"file": "tests/test_more.py", "old": "# two and two", "new": "# two plus two"}

    job, agents, _ = written_job(
        run_job, [edit(*GOOD), done()],
        [review("accept_after_comment_fixes", failing=("comments",), fixes=[fix]),
         review("accept")])

    assert job.data["outcome"] == JobOutcome.COMMITTED
    assert len(agents["implementer"].prompts) == 2
    assert repo.run("show", "HEAD:tests/test_more.py") == PASSING


def test_a_written_test_that_fails_on_the_current_code_goes_back(scratch, run_job) -> None:
    """Proves a written test that fails on the unchanged code is undone and goes back to the
    test writer with the failure and the earlier diff."""
    project, _ = scratch

    def second(prompt: str) -> dict:
        assert app_text(project) == APP
        assert not (project.root / "tests" / "test_more.py").exists()
        assert "test_more" in prompt and "assert 4 == 5" in prompt
        assert "+    assert add(2, 2) == 5" in prompt
        return write_test("tests/test_more.py", PASSING)(prompt)

    job, agents, _ = run_job([plan()], [edit(*GOOD)], [review("accept")],
                             super_planner=[approve(wanted())],
                             test_writer=[write_test("tests/test_more.py", FAILING), second])

    assert len(agents["test_writer"].prompts) == 2
    assert job.data["outcome"] == JobOutcome.COMMITTED


def test_three_refused_test_writes_abandon_the_job(scratch, run_job) -> None:
    """Proves the refusal after the cap of two rewrites abandons the job before the implementer
    is asked, and leaves the tree clean."""
    project, repo = scratch
    reports: list = []

    job, agents, _ = run_job([plan()], super_planner=[approve(wanted())],
                             test_writer=[write_test("tests/test_more.py", FAILING)] * 3,
                             reports=reports)

    assert job.data["outcome"] == JobOutcome.ABANDONED
    assert lines_of(reports, "write-tests")[-1].endswith("-> undo, at the cap of 2 rewrites")
    assert agents["implementer"].prompts == []
    assert repo.changed_paths() == []


def test_a_test_writer_that_changes_code_or_an_existing_test_is_refused(scratch, run_job) -> None:
    """Proves a write that changes source code or adds to an existing test file is refused and
    sent back with both faults."""
    project, _ = scratch

    def meddle(prompt: str) -> dict:
        edit(*GOOD)(prompt)
        test_app = project.root / "tests" / "test_app.py"
        test_app.write_text(test_app.read_text(encoding="utf-8")
                            + "\n\ndef test_more():\n    assert add(2, 2) == 4\n",
                            encoding="utf-8", newline="\n")
        return {"summary": "wrote test_more",
                "tests": [{"file": "tests/test_app.py", "name": "test_more", "covers": "app.add"}]}

    job, agents, _ = run_job([plan()], [edit(*GOOD)], [review("accept")],
                             super_planner=[approve(wanted())],
                             test_writer=[meddle, write_test("tests/test_more.py", PASSING)])

    second = agents["test_writer"].prompts[1]
    assert "src/app.py is not a new test file" in second
    assert "tests/test_app.py already exists at the last commit" in second
    assert job.data["outcome"] == JobOutcome.COMMITTED


def test_a_skip_word_in_a_written_test_is_refused(scratch, run_job) -> None:
    """Proves a written test that holds the word skip, even in a comment, is refused."""
    skipping = PASSING.replace("# two and two", "# skip this later")

    job, agents, _ = run_job([plan()], [edit(*GOOD)], [review("accept")],
                             super_planner=[approve(wanted())],
                             test_writer=[write_test("tests/test_more.py", skipping),
                                          write_test("tests/test_more.py", PASSING)])

    assert "a skip or an expected failure" in agents["test_writer"].prompts[1]
    assert job.data["outcome"] == JobOutcome.COMMITTED


def test_a_test_writer_with_no_answer_halts_the_job_with_the_tree_clean(scratch, run_job) -> None:
    """Proves a cut-off test writer halts the job and drops the files its turn wrote."""
    project, repo = scratch

    def cut_off(prompt: str) -> None:
        write_test("tests/test_more.py", PASSING)(prompt)
        return None

    with pytest.raises(NoAnswerError, match="the test_writer gave no answer: timed out"):
        run_job([plan()], super_planner=[approve(wanted())], test_writer=[cut_off])

    assert repo.changed_paths() == []


def test_an_implementer_that_deletes_a_written_test_is_sent_back(scratch, run_job) -> None:
    """Proves a written test that the implementer deletes is put back, named in the next
    prompt, and committed as the test writer left it."""
    project, repo = scratch

    def delete_test(prompt: str) -> dict:
        (project.root / "tests" / "test_more.py").unlink()
        return edit(*GOOD)(prompt)

    job, agents, _ = written_job(run_job, [delete_test, done()],
                                 [review("accept")])

    assert "tests/test_more.py was written by the test writer" in agents["implementer"].prompts[1]
    assert job.data["outcome"] == JobOutcome.COMMITTED
    assert repo.run("show", "HEAD:tests/test_more.py") == PASSING


def shop_job(run_job, implementer: list[Turn], super_planner: list[Turn],
             reviewer: list[Turn] = (), **options):
    """Runs one job whose plan names shop.cli.total, as its only function."""
    return run_job([plan(functions=["shop.cli.total"])], implementer, reviewer,
                   super_planner=super_planner, **options)


def test_a_function_outside_the_plan_goes_to_the_super_planner_and_its_approval_goes_on(
        scratch, run_job) -> None:
    """Proves a changed function that the plan does not name goes to the super-planner with the
    diff, without the module-level code, and that its approval is kept and the job commits."""
    project, repo = scratch
    commit_shop(project, repo)
    reports: list = []

    job, agents, _ = shop_job(run_job, [add_shop_sub], [approve(), map_approve()],
                              [review("accept")], reports=reports)

    prompts = agents["super_planner"].prompts
    assert job.data["outcome"] == JobOutcome.COMMITTED
    assert len(prompts) == 2
    assert "shop.cli.sub" in prompts[1] and "+def sub(a, b):" in prompts[1]
    assert "shop.cli.<module>" not in prompts[1]
    assert job.data["approved"] == ["shop.cli.sub"]
    assert lines_of(reports, "check-map") == ["1 function outside the plan -> review-map"]
    assert lines_of(reports, "review-map") == ["approve 1 function"]


def test_a_refused_function_goes_back_to_implement_with_the_notes(scratch, run_job) -> None:
    """Proves a refused function sends the job back to the implementer with the super-planner's
    notes, counted as one rejection, and that an attempt that changes only the plan's function
    goes on to the commit."""
    project, repo = scratch
    commit_shop(project, repo)
    reports: list = []

    def only_total(prompt: str) -> dict:
        text = SHOP_CLI.replace('"""Returns the sum."""', '"""Returns the sum of the prices."""')
        (project.root / "src" / "shop" / "cli.py").write_text(text, encoding="utf-8",
                                                              newline="\n")
        return done()

    job, agents, _ = shop_job(run_job, [add_shop_sub, only_total],
                              [approve(), map_revise("drop sub")], [review("accept")],
                              reports=reports)

    second = agents["implementer"].prompts[1]
    assert "drop sub" in second and "shop.cli.sub" in second
    assert job.data["rejections"] == 1
    assert job.data["outcome"] == JobOutcome.COMMITTED
    assert lines_of(reports, "review-map") == ["refuse -> attempt 2"]


def test_three_refused_map_reviews_undo_the_job(scratch, run_job) -> None:
    """Proves the third refusal of a function outside the plan abandons the job at the rejection
    cap, and leaves the tree clean."""
    project, repo = scratch
    commit_shop(project, repo)
    reports: list = []

    job, _, _ = shop_job(run_job, [add_shop_sub, done(), done()],
                         [approve()] + [map_revise("drop sub")] * 3, reports=reports)

    assert job.data["outcome"] == JobOutcome.ABANDONED
    assert job.data["reason"].startswith("# The super-planner refused the functions outside the plan")
    assert repo.changed_paths() == []
    assert lines_of(reports, "review-map")[-1] == "refuse -> undo, at the cap of 3 rejections"


def test_an_approved_function_is_not_reviewed_again(scratch, run_job) -> None:
    """Proves a function that the super-planner approved does not go to it again on the attempt
    after a reviewer's reject."""
    project, repo = scratch
    commit_shop(project, repo)

    job, agents, _ = shop_job(run_job, [add_shop_sub, done()], [approve(), map_approve()],
                              [review("reject", failing=("scope",)), review("accept")])

    assert len(agents["super_planner"].prompts) == 2
    assert job.data["outcome"] == JobOutcome.COMMITTED


def test_the_map_review_answers_without_tests_to_write(scratch, run_job) -> None:
    """Proves the super-planner answers the map review in the shape of
    ratchet.refactor.agent_answers.MapReview, which holds no tests to write, that its prompt
    does not ask about them, and that the agent answers a plan in the shape of SuperPlan again
    after the job."""
    project, repo = scratch
    commit_shop(project, repo)

    job, agents, graph = shop_job(run_job, [add_shop_sub], [approve(), map_approve()],
                                  [review("accept")])

    super_planner = agents["super_planner"]
    assert super_planner.schemas_seen == [SuperPlan, agent_answers.MapReview]
    assert super_planner.schema is SuperPlan
    assert "tests_to_write" not in super_planner.prompts[1]
    saved = json.loads((graph.state_dir / "job" / "attempt-1" / "map-review.json")
                       .read_text(encoding="utf-8"))
    assert set(saved) == {"verdict", "summary", "revisions"}
    assert job.data["outcome"] == JobOutcome.COMMITTED


def test_a_map_review_with_no_answer_puts_the_plan_shape_back(scratch, run_job) -> None:
    """Proves that when the super-planner gives no answer to the map review, it still answers a
    plan in the shape of SuperPlan afterwards, whatever the job does with the failed turn."""
    project, repo = scratch
    commit_shop(project, repo)
    agents: dict = {}

    with contextlib.suppress(NoAnswerError):
        shop_job(run_job, [add_shop_sub], [approve(), None], [review("accept")], seen=agents)

    super_planner = agents["super_planner"]
    assert super_planner.schemas_seen == [SuperPlan, agent_answers.MapReview]
    assert super_planner.schema is SuperPlan


def test_a_module_level_change_does_not_start_a_map_review(scratch, run_job) -> None:
    """Proves a change to module-level code alone is not a function outside the plan, so the
    super-planner is not asked again."""
    project, repo = scratch
    commit_shop(project, repo)
    reports: list = []

    def add_import(prompt: str) -> dict:
        text = SHOP_CLI.replace('"""Totals a basket."""\n\n\n',
                                '"""Totals a basket."""\n\nimport os\n\n\n')
        (project.root / "src" / "shop" / "cli.py").write_text(text, encoding="utf-8",
                                                              newline="\n")
        return done()

    job, agents, _ = shop_job(run_job, [add_import], [approve()], [review("accept")],
                              reports=reports)

    assert len(agents["super_planner"].prompts) == 1
    assert lines_of(reports, "check-map") == []
    assert job.data["outcome"] == JobOutcome.COMMITTED


def test_deleting_an_unplanned_function_goes_to_the_super_planner_and_its_neighbour_does_not(
        scratch, run_job) -> None:
    """Proves that deleting a function the plan does not name, from between two others, sends that
    function to the super-planner, and not the unchanged function after it."""
    project, repo = scratch
    commit_shop(project, repo)
    cli = project.root / "src" / "shop" / "cli.py"
    helper = '\n\ndef helper():\n    """Returns one."""\n    return 1\n'
    marker = "    return sum(prices)\n"
    cli.write_text(SHOP_CLI.replace(marker, marker + helper), encoding="utf-8", newline="\n")
    repo.commit("add a helper")
    reports: list = []

    def drop_helper(prompt: str) -> dict:
        cli.write_text(SHOP_CLI, encoding="utf-8", newline="\n")
        return done()

    job, agents, _ = shop_job(run_job, [drop_helper], [approve(), map_approve()],
                              [review("accept")], reports=reports)

    prompts = agents["super_planner"].prompts
    assert len(prompts) == 2
    assert "shop.cli.helper" in prompts[1]
    assert "shop.cli.main" not in prompts[1]
    assert job.data["approved"] == ["shop.cli.helper"]
    assert lines_of(reports, "check-map") == ["1 function outside the plan -> review-map"]
    assert job.data["outcome"] == JobOutcome.COMMITTED


def test_a_line_no_test_runs_goes_back_to_implement(scratch, run_job) -> None:
    """Proves that, with coverage on, an added line that no test runs sends the job back to the
    implementer as a test failure that names the line, and that a test for it lets the job
    commit."""
    project, repo = scratch
    reports: list = []
    lines: list[int] = []

    def add_sub(prompt: str) -> dict:
        answer = add_app_sub(prompt)
        # the line of sub's return, which is not its docstring
        lines.append(app_text(project).splitlines().index("    return a - b") + 1)
        return answer

    job, agents, _ = run_job([plan()], [add_sub, write_sub_test("app")], [review("accept")],
                             reports=reports, coverage=True)

    line = lines[0]
    texts = [(step, text) for step, text, _ in reports]
    assert ("test", f"1 line not run by any test: src/app.py:{line} -> attempt 2") in texts
    second = agents["implementer"].prompts[1]
    assert "# Lines that no test runs" in second and f"lines {line}" in second
    assert job.data["test_failures"] == 1 and job.data["rejections"] == 0
    assert job.data["outcome"] == JobOutcome.COMMITTED


def test_a_run_that_measures_nothing_does_not_call_every_line_untested(scratch, run_job) -> None:
    """Proves that when the project's own pytest options turn coverage off, so no report is
    written, the added lines are not called untested and the job commits."""
    project, repo = scratch
    project.config.test_command.append("--no-cov")

    job, _, _ = run_job([plan()], [add_app_sub], [review("accept")], coverage=True)

    assert job.data["test_failures"] == 0
    assert job.data["outcome"] == JobOutcome.COMMITTED


def test_three_coverage_failures_undo_the_job(scratch, run_job) -> None:
    """Proves lines that no test runs count toward the test-failure cap, so the third time
    abandons the job and leaves the tree clean."""
    project, repo = scratch
    reports: list = []

    job, _, _ = run_job([plan()], [add_app_sub, done(), done()], reports=reports,
                        coverage=True)

    assert job.data["outcome"] == JobOutcome.ABANDONED
    assert job.data["reason"].startswith("# Lines that no test runs")
    assert lines_of(reports, "test")[-1].endswith("-> undo, at the cap of 3 test failures")
    assert repo.changed_paths() == []


def test_a_function_outside_the_plan_is_reviewed_when_the_owner_sets_diff_noprefix(
        scratch, run_job) -> None:
    """Proves a changed function that the plan does not name still goes to the super-planner when
    the repository's config sets diff.noprefix, which would otherwise drop the a/ and b/ prefixes
    from the diff that the map check reads."""
    project, repo = scratch
    commit_shop(project, repo)
    repo.run("config", "diff.noprefix", "true")
    reports: list = []

    job, agents, _ = shop_job(run_job, [add_shop_sub], [approve(), map_approve()],
                              [review("accept")], reports=reports)

    prompts = agents["super_planner"].prompts
    assert len(prompts) == 2
    assert "shop.cli.sub" in prompts[1]
    assert job.data["approved"] == ["shop.cli.sub"]
    assert lines_of(reports, "check-map") == ["1 function outside the plan -> review-map"]
    assert job.data["outcome"] == JobOutcome.COMMITTED


def test_a_line_no_test_runs_goes_back_to_implement_when_the_owner_sets_diff_noprefix(
        scratch, run_job) -> None:
    """Proves that, with coverage on, an added line that no test runs still sends the job back to
    the implementer when the repository's config sets diff.noprefix, which would otherwise drop
    the a/ and b/ prefixes from the diff that the coverage check reads."""
    project, repo = scratch
    repo.run("config", "diff.noprefix", "true")
    reports: list = []
    lines: list[int] = []

    def add_sub(prompt: str) -> dict:
        answer = add_app_sub(prompt)
        # the line of sub's return, which is not its docstring
        lines.append(app_text(project).splitlines().index("    return a - b") + 1)
        return answer

    job, agents, _ = run_job([plan()], [add_sub, write_sub_test("app")], [review("accept")],
                             reports=reports, coverage=True)

    line = lines[0]
    texts = [(step, text) for step, text, _ in reports]
    assert ("test", f"1 line not run by any test: src/app.py:{line} -> attempt 2") in texts
    assert job.data["test_failures"] == 1
    assert job.data["outcome"] == JobOutcome.COMMITTED
