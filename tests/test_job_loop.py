"""Checks whole refactor runs with scripted agents on a scratch project: one job after another,
the ledger carried between them, the checks before a run starts, and the coverage and map checks
of each job."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from ratchet.agent_calls import NoAnswerError
from ratchet.git import Repo
from ratchet.project import Project
from ratchet.records import commit_records, init_records, records_log, records_repo
from ratchet.refactor.agent_answers import (
    ImplementerAnswer,
    Plan,
    Review,
    SuperPlan,
    WriterAnswer,
)
from ratchet.refactor.job_graph import JobOutcome, RefactorError
from ratchet.refactor import job_loop
from ratchet.refactor.job_loop import run_refactor
from ratchet.refactor.ledger import ProblemStatus, load_ledger
from lock_files import FAKE_PID, LIST, lock_path, read_job_file, write_job_file, write_lock
from scripted_agents import (
    GOOD,
    ScriptedAgent,
    Turn,
    add_app_sub,
    add_shop_sub,
    approve,
    commit_shop,
    commit_shop_basket,
    done,
    edit,
    edit_shop_basket,
    map_approve,
    plan,
    review,
    revise,
    wanted,
    write_sub_test,
    write_test,
)


# A test that the test writer writes, which passes on the scratch project's code.
PASSING = "from app import add\n\n\ndef test_more():\n    assert add(2, 2) == 4\n"


@pytest.fixture
def run(scratch: tuple[Project, Repo], monkeypatch: pytest.MonkeyPatch):
    """Returns a function that runs ratchet with scripted turns and returns the run and agents."""
    project, _ = scratch
    monkeypatch.chdir(project.root)
    # an edit that keeps a file's size within the same second would otherwise run stale bytecode
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")

    def run_with(planner: list[Turn], implementer: list[Turn] = (), reviewer: list[Turn] = (),
                 super_planner: list[Turn] | None = None, test_writer: list[Turn] = (),
                 **options):
        # a super-planner left out approves every plan and asks for no tests
        if super_planner is None:
            super_planner = [approve()] * len(planner)
        agents = {"planner": ScriptedAgent("planner", Plan, planner),
                  "super_planner": ScriptedAgent("super_planner", SuperPlan, super_planner),
                  "test_writer": ScriptedAgent("test_writer", WriterAnswer, test_writer),
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
    assert result.cost_usd == 2.5
    assert ledger_of(project).status_of("P1") == ProblemStatus.DONE
    second = agents["planner"].prompts[1]
    assert "- P1: done" in second and '"rename the comment in add"' in second
    # the step's column starts after the time (7), the job (9) and the problems (7)
    steps = [line[23:36].strip() for line in lines if line[7:].startswith("job-")]
    assert steps == ["plan", "super-plan", "implement", "test", "review", "commit", "plan"]
    commit = next(line for line in lines if " commit " in line)
    assert re.fullmatch(
        r"\d\d:\d\d  job-001 +commit +[0-9a-f]{7} \| J01 \| job \$2\.00 \| run \$2\.00", commit)
    assert (project.state_dir / LIST / "run.log").read_text(encoding="utf-8").count("\n") == len(lines)


def test_a_job_with_written_tests_commits_them(scratch, run) -> None:
    """Proves the test writer's test goes into the job's commit, and the job's problem is done."""
    project, repo = scratch

    result, _, _ = run([plan()], [edit(*GOOD)], [review("accept")],
                       super_planner=[approve(wanted())],
                       test_writer=[write_test("tests/test_more.py", PASSING)])

    assert [record.outcome for record in result.jobs] == [JobOutcome.COMMITTED]
    committed = repo.run("show", "--name-only", "--format=", "HEAD").split()
    assert "tests/test_more.py" in committed and "src/app.py" in committed
    assert ledger_of(project).status_of("P1") == ProblemStatus.DONE


def test_a_job_abandoned_at_the_revise_cap_lets_the_run_go_on(scratch, run) -> None:
    """Proves a job abandoned after too many revises is recorded, and the next job runs."""
    project, _ = scratch

    result, _, _ = run(
        [plan()] * 3 + [plan(problems=["P2"], completes=["P2"])], [edit("a + b", "b + a")],
        [review("accept")], super_planner=[revise("rename it")] * 3 + [approve()], max_jobs=2)

    assert [record.outcome for record in result.jobs] == [JobOutcome.ABANDONED,
                                                          JobOutcome.COMMITTED]
    assert ledger_of(project).status_of("P1") == ProblemStatus.ABANDONED
    assert ledger_of(project).status_of("P2") == ProblemStatus.DONE


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

    with pytest.raises(NoAnswerError, match="the reviewer gave no answer"):
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


def test_a_halted_job_leaves_job_started_in_the_ledger_file(scratch, run) -> None:
    """Proves the ledger on disk holds a job_started event for a job that halted."""
    project, _ = scratch

    with pytest.raises(NoAnswerError, match="the reviewer gave no answer"):
        run([plan()], [edit(*GOOD)], [lambda prompt: None])

    events = ledger_of(project).problems["P1"].events
    assert [event.kind for event in events] == ["job_started"]


def test_a_halted_jobs_id_is_not_reused(scratch, run) -> None:
    """Proves the job after a halted one takes the next id and the next slug, so the history
    never shows two jobs with one id."""
    project, _ = scratch
    with pytest.raises(NoAnswerError, match="the reviewer gave no answer"):
        run([plan()], [edit(*GOOD)], [lambda prompt: None])

    result, _, lines = run([plan()], [edit(*GOOD)], [review("accept")])

    assert result.jobs[0].id == "J02"
    events = ledger_of(project).problems["P1"].events
    assert [event.job for event in events if event.kind == "job_started"] == ["J01", "J02"]
    assert any("job-002" in line for line in lines)


def test_a_run_commits_its_ledger_to_the_records_repository(scratch, run) -> None:
    """Proves a run's ledger goes into one records commit when .ratchet is its own repository."""
    project, _ = scratch
    init_records(project.root)
    commit_records(project.root, "first records")

    result, _, _ = run([plan()], [edit(*GOOD)], [review("accept")])

    assert result.records_commit == records_log(project.root)[0].split()[0]
    assert records_log(project.root)[0].endswith(f"refactor: 1 job on {LIST}")


def test_a_halted_job_commits_the_records(scratch, run) -> None:
    """Proves a job that halts still has its ledger committed to the records repository."""
    project, _ = scratch
    init_records(project.root)
    commit_records(project.root, "first records")

    with pytest.raises(NoAnswerError, match="the reviewer gave no answer"):
        run([plan()], [edit(*GOOD)], [lambda prompt: None])

    assert records_log(project.root)[0].endswith(f"refactor: 0 jobs on {LIST}")
    committed = records_repo(project.root).run("show", f"HEAD:state/{LIST}/ledger.json")
    assert "job_started" in committed


def test_a_run_with_no_records_repository_makes_no_records_commit(scratch, run) -> None:
    """Proves refactor only skips the records commit when init has not run."""
    result, _, lines = run([plan()], [edit(*GOOD)], [review("accept")])

    assert result.records_commit == ""
    assert any("records are not committed: run `ratchet init`" in line for line in lines)


def test_a_run_counts_the_commits_since_the_list_was_published(scratch, run) -> None:
    """Proves the last line counts the commits since the list was published, with no suggestion
    below the set number of committed jobs."""
    result, _, lines = run([plan()], [edit(*GOOD)], [review("accept")])

    assert result.commits_since_list == 2
    assert result.jobs_committed_on_list == 1
    assert lines[-1].endswith(f"2 commits since {LIST} was published")
    assert "consider" not in lines[-1]


def test_a_run_suggests_an_audit_at_the_set_number_of_committed_jobs(
        scratch, run, monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the suggestion appears once the list's ledger holds the set number of committed
    jobs, counting the jobs of earlier runs, and not before."""
    monkeypatch.setattr(job_loop, "REAUDIT_JOBS", 2)
    _, _, lines = run([plan()], [edit(*GOOD)], [review("accept")])
    assert lines[-1].endswith(f"2 commits since {LIST} was published")
    assert "consider" not in lines[-1]

    result, _, lines = run([plan(problems=["P2"], completes=["P2"])], [edit("a + b", "b + a")],
                           [review("accept")])

    assert lines[-1].endswith(f"3 commits since {LIST} was published, 2 jobs committed on it, "
                              "consider `ratchet audit`")
    assert result.jobs_committed_on_list == 2


def test_an_abandoned_job_does_not_count_toward_the_audit_suggestion(
        scratch, run, monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves only committed jobs count toward the suggestion, so an abandoned job leaves it out."""
    monkeypatch.setattr(job_loop, "REAUDIT_JOBS", 2)

    result, _, lines = run(
        [plan()] * 3 + [plan(problems=["P2"], completes=["P2"])], [edit("a + b", "b + a")],
        [review("accept")], super_planner=[revise("rename it")] * 3 + [approve()], max_jobs=2)

    assert [record.outcome for record in result.jobs] == [JobOutcome.ABANDONED,
                                                          JobOutcome.COMMITTED]
    assert result.jobs_committed_on_list == 1
    assert "consider" not in lines[-1]


def test_the_audit_suggestion_waits_for_three_committed_jobs() -> None:
    """Proves a run suggests a re-audit from three committed jobs on the list."""
    assert job_loop.REAUDIT_JOBS == 3


def test_commits_since_counts_only_the_commits_after_the_time(
        tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves commits_since leaves out the commits dated before the time it is given."""
    repo = Repo(tmp_path)
    repo.run("init", "-q", "-b", "main")
    repo.run("config", "user.email", "ratchet@example.com")
    repo.run("config", "user.name", "ratchet")
    dates = ["2020-01-01T12:00:00", "2020-01-02T12:00:00", "2020-03-01T12:00:00",
             "2020-03-02T12:00:00", "2020-03-03T12:00:00"]
    for number, date in enumerate(dates):
        monkeypatch.setenv("GIT_AUTHOR_DATE", date)
        monkeypatch.setenv("GIT_COMMITTER_DATE", date)
        (tmp_path / f"file{number}.txt").write_text("x\n", encoding="utf-8")
        repo.commit(f"commit {number}")

    assert job_loop.commits_since(repo, "2020-02-01 00:00") == 3
    assert job_loop.commits_since(repo, "2020-03-02 00:00") == 2
    assert job_loop.commits_since(repo, "2021-01-01 00:00") == 0


def test_an_untested_line_goes_back_from_run_refactor(scratch, run) -> None:
    """Proves a run turns coverage on when pytest-cov loads, so that an added line that no test
    runs goes back to the implementer before the commit."""
    project, repo = scratch

    result, _, lines = run([plan()], [add_app_sub, write_sub_test("app")], [review("accept")],
                           width=200)

    assert [record.outcome for record in result.jobs] == [JobOutcome.COMMITTED]
    assert any("not run by any test: src/app.py:" in line for line in lines)
    assert not any("coverage is not checked" in line for line in lines)
    committed = repo.run("show", "--name-only", "--format=", "HEAD").split()
    assert "src/app.py" in committed and "tests/test_sub.py" in committed


def test_an_added_line_in_an_omitted_file_is_not_sent_back_from_run_refactor(scratch, run) -> None:
    """Proves a run does not send back an added line in a src file that the project's coverage
    config omits, since no test could make coverage measure it."""
    project, repo = scratch
    # omit src/app.py, and add src/extra.py so that the coverage report still holds a file
    (project.root / ".coveragerc").write_text("[run]\nomit = */app.py\n", encoding="utf-8")
    (project.root / "src" / "extra.py").write_text("X = 1\n", encoding="utf-8", newline="\n")
    repo.run("add", "-A")
    repo.run("commit", "-q", "-m", "omit app")

    result, agents, lines = run([plan()], [add_app_sub], [review("accept")], width=200)

    assert [record.outcome for record in result.jobs] == [JobOutcome.COMMITTED]
    assert len(agents["implementer"].prompts) == 1
    assert not any("not run by any test" in line for line in lines)


def test_a_run_without_pytest_cov_says_once_that_coverage_is_not_checked(scratch, run) -> None:
    """Proves a run whose test command does not load pytest-cov says once that coverage is not
    checked, and commits an added line that no test runs."""
    project, _ = scratch
    project.config.test_command.extend(["-p", "no:pytest_cov"])

    result, agents, lines = run([plan(), plan(stop=True)], [add_app_sub], [review("accept")],
                                max_jobs=2, width=200)

    assert sum("coverage is not checked" in line for line in lines) == 1
    assert [record.outcome for record in result.jobs] == [JobOutcome.COMMITTED]
    assert len(agents["implementer"].prompts) == 1


def test_a_function_outside_the_plan_is_reviewed_from_run_refactor(scratch, run) -> None:
    """Proves a run checks the map after the implementer and has the super-planner review a
    function outside the plan, before the job commits."""
    project, repo = scratch
    commit_shop(project, repo)

    def add_tested_sub(prompt: str) -> dict:
        add_shop_sub(prompt)
        return write_sub_test("shop.cli")(prompt)

    result, _, lines = run([plan(functions=["shop.cli.total"])], [add_tested_sub],
                           [review("accept")], super_planner=[approve(), map_approve()])

    # the step's column starts after the time (7), the job (9) and the problems (7)
    steps = [line[23:36].strip() for line in lines if line[7:].startswith("job-")]
    assert "check-map" in steps and "review-map" in steps
    assert steps.index("check-map") < steps.index("review-map")
    assert [record.outcome for record in result.jobs] == [JobOutcome.COMMITTED]


def test_a_class_field_outside_the_plan_is_reviewed_from_run_refactor(scratch, run) -> None:
    """Proves a run checks the map after the implementer and has the super-planner review a
    field added to a class that the plan does not name, before the job commits."""
    project, repo = scratch
    commit_shop(project, repo)
    commit_shop_basket(project, repo)

    def add_tested_field(prompt: str) -> dict:
        edit_shop_basket(("    prices: list = []\n",
                          "    prices: list = []\n    names: list = []\n"))(prompt)
        (project.root / "tests" / "test_basket.py").write_text(
            "from shop.basket import Basket\n\n\ndef test_names():\n"
            "    assert Basket.names == []\n", encoding="utf-8", newline="\n")
        return done()

    result, agents, lines = run([plan(functions=["shop.cli.total"])], [add_tested_field],
                                [review("accept")], super_planner=[approve(), map_approve()])

    # the step's column starts after the time (7), the job (9) and the problems (7)
    steps = [line[23:36].strip() for line in lines if line[7:].startswith("job-")]
    assert "check-map" in steps and "review-map" in steps
    assert steps.index("check-map") < steps.index("review-map")
    assert "shop.basket.Basket" in agents["super_planner"].prompts[1]
    assert [record.outcome for record in result.jobs] == [JobOutcome.COMMITTED]


def test_a_function_the_plan_adds_is_not_reviewed_from_run_refactor(scratch, run) -> None:
    """Proves a run commits a function that the plan lists in 'added' without a map review."""
    project, repo = scratch
    commit_shop(project, repo)
    added = {"kind": "function", "name": "shop.cli.sub", "file": "src/shop/cli.py",
             "requirement": "a basket can take one price from another",
             "why_not_simpler": "no function subtracts yet"}

    def add_tested_sub(prompt: str) -> dict:
        add_shop_sub(prompt)
        return write_sub_test("shop.cli")(prompt)

    result, agents, lines = run([plan(functions=["shop.cli.total"], added=[added])],
                                [add_tested_sub], [review("accept")], super_planner=[approve()])

    # the step's column starts after the time (7), the job (9) and the problems (7)
    steps = [line[23:36].strip() for line in lines if line[7:].startswith("job-")]
    assert "review-map" not in steps
    assert len(agents["super_planner"].prompts) == 1
    assert [record.outcome for record in result.jobs] == [JobOutcome.COMMITTED]


def lock_is_gone(project: Project) -> bool:
    """Returns whether the project has no graph lock."""
    return not lock_path(project).exists()


def run_log(project: Project) -> list[str]:
    """Returns the lines of the run's log on the scratch project's problem list."""
    return (project.state_dir / LIST / "run.log").read_text(encoding="utf-8").splitlines()


def interrupt(prompt: str) -> dict:
    """Stands in for a turn that the owner cuts off with Ctrl-C."""
    raise KeyboardInterrupt


def test_a_run_releases_the_lock(scratch, run) -> None:
    """Proves a run that committed its job leaves no lock behind."""
    project, _ = scratch

    result, _, _ = run([plan()], [edit(*GOOD)], [review("accept")])

    assert [record.outcome for record in result.jobs] == [JobOutcome.COMMITTED]
    assert lock_is_gone(project)


def test_a_halted_job_releases_the_lock(scratch, run) -> None:
    """Proves a job cut off by an agent that gave no answer is halted and leaves no lock."""
    project, _ = scratch

    with pytest.raises(NoAnswerError, match="the reviewer gave no answer"):
        run([plan()], [edit(*GOOD)], [lambda prompt: None])

    assert read_job_file(project.state_dir / LIST / "refactor" / "job-001.json")["status"] == "halted"
    assert lock_is_gone(project)


def test_ctrl_c_in_a_job_undoes_it_marks_it_halted_and_releases_the_lock(scratch, run) -> None:
    """Proves Ctrl-C in a step, which is not an Exception, still undoes the job's changes, halts
    its job file and removes the lock, and its problem stays open."""
    project, repo = scratch

    with pytest.raises(KeyboardInterrupt):
        run([plan()], [edit(*GOOD)], [interrupt])

    assert repo.changed_paths() == []
    assert read_job_file(project.state_dir / LIST / "refactor" / "job-001.json")["status"] == "halted"
    assert any(" undo " in line and "job-001" in line for line in run_log(project))
    assert ledger_of(project).status_of("P1") == ProblemStatus.OPEN
    assert lock_is_gone(project)


def test_a_dirty_tree_refusal_releases_the_lock(scratch, run) -> None:
    """Proves a run refused on a dirty tree leaves no lock behind."""
    project, _ = scratch
    (project.root / "src" / "extra.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(RefactorError, match="the working tree has changes"):
        run([plan()])

    assert lock_is_gone(project)


def test_a_run_is_refused_while_an_audit_waits(scratch, run) -> None:
    """Proves a run does not start while an audit waits for the owner's answers, and asks the
    planner nothing."""
    from ratchet.graph_lock import LockError

    project, _ = scratch
    write_job_file(project.state_dir / "audits" / "audit-x.json", "waiting", "check")
    write_lock(project, graph="audit", job="audit-x", job_file="audits/audit-x.json",
               pid=FAKE_PID, state="waiting", gate="questions")
    asked: list[str] = []

    with pytest.raises(LockError) as refused:
        run([lambda prompt: asked.append(prompt) or plan()])

    assert str(refused.value) == ("Audit audit-x is waiting for your answers. Finish or discard "
                                  "it first.")
    assert asked == []
    assert lock_path(project).exists()


def test_a_stale_refactor_lock_is_rolled_back_by_the_next_run(scratch, run) -> None:
    """Proves a run after a refactor whose process is gone undoes that job's changes, halts it,
    says so, and goes on to commit its own job."""
    project, repo = scratch
    app = project.root / "src" / "app.py"
    app.write_text(app.read_text(encoding="utf-8") + "\nX = 1\n", encoding="utf-8", newline="\n")
    old = write_job_file(project.state_dir / LIST / "refactor" / "job-001.json", "running", "review")
    write_lock(project, job="job-001", job_file=f"{LIST}/refactor/job-001.json", pid=FAKE_PID)

    result, _, lines = run([plan()], [edit(*GOOD)], [review("accept")])

    assert [record.id for record in result.jobs] == ["J01"]
    assert [record.outcome for record in result.jobs] == [JobOutcome.COMMITTED]
    undone = "The last refactor (job-001) stopped without finishing. Its changes were undone."
    assert sum(undone in line for line in lines) == 1
    assert read_job_file(old)["status"] == "halted"
    assert "X = 1" not in repo.run("show", "HEAD:src/app.py")
    assert lock_is_gone(project)


def test_a_taken_job_name_gets_a_counter_in_the_lock_and_the_halt(scratch, run) -> None:
    """Proves a job whose name is taken by an earlier job file is locked, halted and undone under
    the name that rooster gives it, and the earlier file is left alone."""
    from ratchet.graph_lock import read_lock

    project, repo = scratch
    folder = project.state_dir / LIST / "refactor"
    taken = write_job_file(folder / "job-001.json", "done", None)
    taken_before = taken.read_bytes()
    locked: list[str] = []

    def implement(prompt: str) -> dict:
        locked.append(read_lock(project).job)
        return edit(*GOOD)(prompt)

    with pytest.raises(KeyboardInterrupt):
        run([plan()], [implement], [interrupt])

    assert locked == ["job-001-2"]
    assert read_job_file(folder / "job-001-2.json")["status"] == "halted"
    assert taken.read_bytes() == taken_before
    assert any(" undo " in line and "job-001-2" in line for line in run_log(project))
    assert repo.changed_paths() == []


def test_the_first_slug_given_to_the_lock_counts_past_a_taken_name(
        scratch, run, monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the lock that a run takes before its first job names the job file that rooster will
    write, when an earlier job file already holds the plain name."""
    project, _ = scratch
    write_job_file(project.state_dir / LIST / "refactor" / "job-001.json", "done", None)
    real_take = job_loop.take_lock
    asked_for: list[str] = []

    def recording_take(*args, **kwargs):
        # keep the job file of each call, then take the lock as the run would
        asked_for.append(Path(inspect.signature(real_take).bind(*args, **kwargs)
                              .arguments["job_file"]).stem)
        return real_take(*args, **kwargs)

    monkeypatch.setattr(job_loop, "take_lock", recording_take)

    run([plan()], [edit(*GOOD)], [review("accept")])

    assert asked_for[0] == "job-001-2"


def test_a_job_that_halts_without_raising_is_undone_and_refused(
        scratch, run, monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves a job that rooster halts without an exception, here by a step cap, is undone and
    ends the run with its halt reason, and the lock is released."""
    project, repo = scratch
    real_build = job_loop.build_job_graph

    def capped_build(*args, **kwargs):
        # a graph that may take one step only halts after the planner
        graph = real_build(*args, **kwargs)
        graph.max_steps = 1
        return graph

    monkeypatch.setattr(job_loop, "build_job_graph", capped_build)

    with pytest.raises(RefactorError, match="job-001 halted: stopped after 1 steps"):
        run([plan()], [edit(*GOOD)], [review("accept")])

    assert repo.changed_paths() == []
    assert read_job_file(project.state_dir / LIST / "refactor" / "job-001.json")["status"] == "halted"
    assert lock_is_gone(project)
