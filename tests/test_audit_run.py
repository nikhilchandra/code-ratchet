"""Checks the functions that run an audit from the command line, with scripted agents."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from rooster import Job, JobStatus

from ratchet.audit.agent_answers import (
    AuditorAnswer,
    CheckAnswer,
    ListReview,
    PairAnswer,
    RecheckAnswer,
)
from ratchet.audit.audit_run import (
    APPROVE,
    DISCARD,
    AuditRun,
    _clean_up,
    _drop_copy,
    _new_slug,
    settle_audit,
    start_audit,
)
from ratchet.audit.audit_graph import AuditError, build_audit_graph
from ratchet.audit.code_copy import remove_copy
from ratchet.audit.settings import AuditSettings
from ratchet.project import Project
from ratchet.refactor.problem_list import find_latest_problem_list, find_problem_lists
from scripted_agents import ScriptedAgent
from test_audit_graph import (  # noqa: F401
    audit_project,
    auditor_turn,
    build_project,
    checker_turn,
    default_turns,
)

ANSWERS = {"auditor": AuditorAnswer, "fresh": AuditorAnswer, "list_reviewer_a": ListReview,
           "list_reviewer_b": ListReview, "rechecker": RecheckAnswer, "pair_judge": PairAnswer,
           "checker": CheckAnswer}


def scripted(project: Project) -> tuple[dict[str, ScriptedAgent], object]:
    """Returns scripted agents for a first audit and the agent maker that hands them out."""
    turns = default_turns(project)
    agents = {name: ScriptedAgent(name, schema, turns.get(name, []))
              for name, schema in ANSWERS.items()}
    return agents, lambda *_: agents


def waiting_run(project: Project) -> AuditRun:
    """Runs an audit with no terminal, which waits at the draft."""
    _, maker = scripted(project)
    return start_audit(project, "look at rounding", agent_maker=maker, interactive=False)


def test_with_no_terminal_the_audit_waits_and_can_be_approved(audit_project: Project) -> None:
    """Proves an audit with no terminal stops at the draft, and that approving it later
    publishes the list."""
    run = waiting_run(audit_project)

    assert run.outcome == "waiting" and Path(run.draft_path).is_file()
    assert not find_problem_lists(audit_project)

    done = settle_audit(audit_project, APPROVE)

    assert done.outcome == "published" and done.slug == run.slug
    assert find_latest_problem_list(audit_project).name == done.list_name


def test_discarding_a_waiting_audit_writes_no_list(audit_project: Project) -> None:
    """Proves discarding the waiting audit ends it without a list."""
    waiting_run(audit_project)

    done = settle_audit(audit_project, DISCARD)

    assert done.outcome == "discarded" and not find_problem_lists(audit_project)


def test_an_audit_is_refused_while_another_waits(audit_project: Project) -> None:
    """Proves a second audit does not start while the first waits for a decision."""
    waiting_run(audit_project)
    _, maker = scripted(audit_project)

    with pytest.raises(AuditError, match="--approve"):
        start_audit(audit_project, agent_maker=maker, interactive=False)


def test_settling_with_no_waiting_audit_is_refused(audit_project: Project) -> None:
    """Proves approving or discarding needs an audit that waits."""
    with pytest.raises(AuditError, match="no audit is waiting"):
        settle_audit(audit_project, APPROVE)


def test_approving_is_refused_when_head_has_moved(audit_project: Project) -> None:
    """Proves a draft is not published after the code moved on, and can still be discarded."""
    waiting_run(audit_project)
    (audit_project.root / "later.txt").write_text("later\n", encoding="utf-8")
    for command in (["add", "-A"], ["commit", "-q", "-m", "later"]):
        subprocess.run(["git", *command], cwd=audit_project.root, check=True, capture_output=True)

    with pytest.raises(AuditError, match="HEAD has moved"):
        settle_audit(audit_project, APPROVE)

    assert settle_audit(audit_project, DISCARD).outcome == "discarded"


def test_the_template_rules_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves an audit does not start while rules.md still has the template marker."""
    monkeypatch.chdir(tmp_path)
    project = build_project(tmp_path / "project", template=True)

    with pytest.raises(AuditError, match="template marker"):
        start_audit(project, interactive=False)


def test_an_audit_before_init_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves an audit does not start in a project that has not run init."""
    monkeypatch.chdir(tmp_path)
    project = build_project(tmp_path / "project", initialized=False)

    with pytest.raises(AuditError, match="ratchet init"):
        start_audit(project, interactive=False)


def test_the_owner_approves_in_the_terminal(audit_project: Project) -> None:
    """Proves an owner at a terminal sees the summary and approving publishes the list."""
    _, maker = scripted(audit_project)
    shown: list[str] = []

    run = start_audit(audit_project, agent_maker=maker, interactive=True,
                      ask=lambda prompt: "a", show=shown.append)

    assert run.outcome == "published"
    assert any("problems" in line for line in shown)
    assert find_latest_problem_list(audit_project).name == run.list_name


def test_later_leaves_the_audit_waiting(audit_project: Project) -> None:
    """Proves choosing later in the terminal leaves the audit waiting."""
    _, maker = scripted(audit_project)

    run = start_audit(audit_project, agent_maker=maker, interactive=True,
                      ask=lambda prompt: "l", show=lambda line: None)

    assert run.outcome == "waiting"


def cut_off_at_questions(project: Project) -> dict[str, ScriptedAgent]:
    """Runs an audit that raises a question and stops when the owner is asked, and returns its
    agents."""
    question = {"kind": "rules", "title": "Is rounding twice deliberate",
                "evidence": [], "why_it_may_be_deliberate": "Money is rounded at each step.",
                "what_would_settle": "A word from the owner."}
    turns = default_turns(project) | {"auditor": [auditor_turn(project, questions=[question])],
                                      "checker": [checker_turn()]}
    agents = {name: ScriptedAgent(name, schema, turns.get(name, []))
              for name, schema in ANSWERS.items()}

    def interrupt(prompt: str) -> str:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        start_audit(project, agent_maker=lambda *_: agents, interactive=True, ask=interrupt,
                    show=lambda line: None)
    return agents


def test_approving_an_audit_cut_off_at_the_questions_stops_at_the_draft(
    audit_project: Project,
) -> None:
    """Proves approving an audit left at the questions gate does not publish, and leaves a draft
    for the owner to read."""
    agents = cut_off_at_questions(audit_project)

    run = settle_audit(audit_project, APPROVE, agent_maker=lambda *_: agents)

    assert run.outcome == "waiting" and Path(run.draft_path).is_file()
    assert not find_problem_lists(audit_project)

    done = settle_audit(audit_project, APPROVE, agent_maker=lambda *_: agents)

    assert done.outcome == "published"


def test_approving_an_audit_cut_off_at_the_questions_is_refused_when_head_has_moved(
    audit_project: Project,
) -> None:
    """Proves HEAD is checked before the checker is called."""
    agents = cut_off_at_questions(audit_project)
    (audit_project.root / "later.txt").write_text("later\n", encoding="utf-8")
    for command in (["add", "-A"], ["commit", "-q", "-m", "later"]):
        subprocess.run(["git", *command], cwd=audit_project.root, check=True, capture_output=True)

    with pytest.raises(AuditError, match="HEAD has moved"):
        settle_audit(audit_project, APPROVE, agent_maker=lambda *_: agents)

    assert agents["checker"].prompts == []


def test_discarding_an_audit_cut_off_at_the_questions_calls_no_checker(
    audit_project: Project,
) -> None:
    """Proves discarding an audit left at the questions gate ends it with no checker turn."""
    agents = cut_off_at_questions(audit_project)
    copy = Path(next(iter(build_audit_graph(audit_project, AuditSettings()).jobs())).data["copy"])

    done = settle_audit(audit_project, DISCARD, agent_maker=lambda *_: agents)

    assert done.outcome == "discarded" and not find_problem_lists(audit_project)
    assert agents["checker"].prompts == []
    assert not copy.exists()


def test_approving_an_audit_cut_off_at_the_questions_halts_when_the_copy_is_gone(
    audit_project: Project,
) -> None:
    """Proves approving an audit left at the questions gate, whose copy has since been deleted,
    halts before the checker is called and no longer counts as a waiting audit."""
    agents = cut_off_at_questions(audit_project)
    copy = Path(next(iter(build_audit_graph(audit_project, AuditSettings()).jobs())).data["copy"])
    remove_copy(copy)

    with pytest.raises(AuditError, match="copy"):
        settle_audit(audit_project, APPROVE, agent_maker=lambda *_: agents)

    assert agents["checker"].prompts == []
    with pytest.raises(AuditError, match="no audit is waiting"):
        settle_audit(audit_project, APPROVE, agent_maker=lambda *_: agents)


def test_fresh_audits_as_a_first_audit_when_a_list_exists(audit_project: Project) -> None:
    """Proves fresh skips the rechecker even though the project has a problem list."""
    waiting_run(audit_project)
    settle_audit(audit_project, APPROVE)
    agents, maker = scripted(audit_project)

    run = start_audit(audit_project, agent_maker=maker, fresh=True, interactive=False)

    assert run.outcome == "waiting"
    assert agents["rechecker"].prompts == [] and agents["auditor"].prompts


def only_job(project: Project) -> Job:
    """Returns the one audit job that the project has."""
    [job] = build_audit_graph(project, AuditSettings()).jobs()
    return job


def test_a_halted_audit_deletes_its_copy(
    audit_project: Project, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves an audit that halts at the cost cap after its copy was made leaves no copy behind."""
    monkeypatch.setattr("ratchet.audit.audit_run.load_settings",
                        lambda _: AuditSettings(max_cost_usd=12.0))
    _, maker = scripted(audit_project)

    with pytest.raises(AuditError, match="cap"):
        start_audit(audit_project, agent_maker=maker, interactive=False)

    job = only_job(audit_project)
    assert job.status == JobStatus.HALTED and "copy" in job.data
    copy = Path(job.data["copy"])
    try:
        assert not copy.exists()
    finally:
        remove_copy(copy)


def test_an_audit_sweeps_the_copies_of_earlier_halted_audits(audit_project: Project) -> None:
    """Proves an audit that starts deletes the copy that an earlier halted audit left behind, and
    still reaches its draft."""
    agents, _ = scripted(audit_project)
    graph = build_audit_graph(audit_project, AuditSettings(max_cost_usd=12.0), lambda *_: agents)
    with pytest.raises(AuditError, match="cap"):
        graph.start("", slug="capped")
    capped = Path(graph.load("capped").data["copy"])
    assert capped.is_dir()

    try:
        run = waiting_run(audit_project)

        assert run.outcome == "waiting"
        assert not capped.exists()
    finally:
        remove_copy(capped)


def test_an_audit_after_one_that_halted_in_prepare_still_starts(audit_project: Project) -> None:
    """Proves an audit that halted on a dirty tree, before it made a copy, does not stop the next
    audit from reaching its draft."""
    stray = audit_project.root / "stray.txt"
    stray.write_text("stray\n", encoding="utf-8")
    _, maker = scripted(audit_project)
    with pytest.raises(AuditError, match="working tree"):
        start_audit(audit_project, agent_maker=maker, interactive=False)
    stray.unlink()

    assert waiting_run(audit_project).outcome == "waiting"


def test_a_waiting_audit_keeps_its_copy(audit_project: Project) -> None:
    """Proves the copy of an audit that waits at the questions gate survives both the interrupt
    and a second audit that is refused."""
    agents = cut_off_at_questions(audit_project)
    job = only_job(audit_project)
    copy = Path(job.data["copy"])

    with pytest.raises(AuditError, match="--approve"):
        start_audit(audit_project, agent_maker=lambda *_: agents, interactive=False)

    assert only_job(audit_project).status == JobStatus.WAITING and copy.is_dir()
    settle_audit(audit_project, DISCARD, agent_maker=lambda *_: agents)


def test_dropping_a_copy_ignores_a_path_that_is_not_a_copy(tmp_path: Path) -> None:
    """A stored path that is not a ratchet copy is left alone and raises nothing."""
    folder = tmp_path / "project"
    folder.mkdir()

    _drop_copy(folder)

    assert folder.is_dir()


def test_cleaning_up_a_job_that_was_never_saved_does_nothing(audit_project: Project) -> None:
    """A slug with no saved job is skipped."""
    graph = build_audit_graph(audit_project, AuditSettings())

    _clean_up(graph, "audit-never-started")


def test_a_new_slug_counts_past_every_name_taken(audit_project: Project) -> None:
    """The counter skips each saved job of the same name, not only the first."""
    graph = build_audit_graph(audit_project, AuditSettings())
    graph.state_dir.mkdir(parents=True, exist_ok=True)
    assert _new_slug(graph, "audit-x") == "audit-x"
    (graph.state_dir / "audit-x.json").write_text("{}")
    assert _new_slug(graph, "audit-x") == "audit-x-2"
    (graph.state_dir / "audit-x-2.json").write_text("{}")

    assert _new_slug(graph, "audit-x") == "audit-x-3"


def test_discarding_with_another_temp_folder_leaves_the_copy_and_still_ends(
    audit_project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves a discard still ends the audit when the temp folder is not the one the copy was made
    in, and that remove_copy's refusal leaves the copy alone."""
    agents = cut_off_at_questions(audit_project)
    copy = Path(next(iter(build_audit_graph(audit_project, AuditSettings()).jobs())).data["copy"])
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "othertemp"))

    done = settle_audit(audit_project, DISCARD, agent_maker=lambda *_: agents)

    assert done.outcome == "discarded"
    assert copy.is_dir()
    monkeypatch.undo()
    remove_copy(copy)


def test_approving_with_another_temp_folder_still_reaches_the_draft(
    audit_project: Project, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves the checker's work is kept when remove_copy refuses the copy after it."""
    agents = cut_off_at_questions(audit_project)
    copy = Path(next(iter(build_audit_graph(audit_project, AuditSettings()).jobs())).data["copy"])
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path / "othertemp"))

    run = settle_audit(audit_project, APPROVE, agent_maker=lambda *_: agents)

    assert run.outcome == "waiting" and Path(run.draft_path).is_file()
    monkeypatch.undo()
    remove_copy(copy)
