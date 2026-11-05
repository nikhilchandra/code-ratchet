"""Checks whole refactor runs with scripted agents on a scratch project: one job after another,
the ledger carried between them, and the checks before a run starts."""

from __future__ import annotations

import re

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
from scripted_agents import (
    GOOD,
    ScriptedAgent,
    Turn,
    approve,
    edit,
    plan,
    review,
    revise,
    wanted,
    write_test,
)

LIST = "problems-20261026-09h00m"

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
    below the set number."""
    result, _, lines = run([plan()], [edit(*GOOD)], [review("accept")])

    assert result.commits_since_list == 2
    assert lines[-1].endswith(f"2 commits since {LIST} was published")


def test_a_run_suggests_a_re_audit_at_the_set_number_of_commits(
        scratch, run, monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the suggestion appears when the count equals the set number, and not when the set
    number is one higher."""
    monkeypatch.setattr(job_loop, "REAUDIT_COMMITS", 3)
    _, _, lines = run([plan()], [edit(*GOOD)], [review("accept")])
    assert lines[-1].endswith(f"2 commits since {LIST} was published")

    _, _, lines = run([plan(problems=["P2"], completes=["P2"])], [edit("a + b", "b + a")],
                      [review("accept")])

    assert lines[-1].endswith(f"3 commits since {LIST} was published, consider `ratchet audit`")


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
