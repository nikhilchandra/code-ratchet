"""Checks the functions that run an audit from the command line, with scripted agents."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ratchet.architecture import check_architecture
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
from ratchet.audit.settings import AuditSettings
from ratchet.project import Config, Project
from ratchet.refactor.problem_list import find_latest_problem_list, find_problem_lists
from scripted_agents import ScriptedAgent
from test_audit_graph import (  # noqa: F401
    NOT_BLIND,
    age_list,
    audit_project,
    auditor_turn,
    build_project,
    checker_turn,
    commit_ratchet,
    continuing_checker,
    default_turns,
    fresh_turn,
    git_output,
    judge_turn,
    recheck_turn,
    short_hash,
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


def test_approving_is_refused_when_the_records_repository_is_damaged(
    audit_project: Project,
) -> None:
    """Proves a draft is not published when .ratchet/.git is damaged, so nothing is written or
    committed to the project's own repository, and the audit can still be discarded."""
    waiting_run(audit_project)
    root = audit_project.root
    records_git = root / ".ratchet" / ".git"
    records_git.rename(root.parent / "records-git")
    records_git.mkdir()
    (root / "src" / "app" / "core" / "model.py").write_text("changed\n", encoding="utf-8")
    rules = (root / ".ratchet" / "rules.md").read_bytes()

    def git(*command: str) -> str:
        """Runs git in the project's own folder and returns its output."""
        return subprocess.run(["git", *command], cwd=root, check=True, capture_output=True,
                              text=True).stdout.strip()

    head = git("rev-parse", "HEAD")

    with pytest.raises(AuditError, match="ratchet init"):
        settle_audit(audit_project, APPROVE)

    assert not find_problem_lists(audit_project)
    assert (root / ".ratchet" / "rules.md").read_bytes() == rules
    assert not list((root / ".ratchet" / "state").glob("*/ledger.json"))
    assert git("rev-parse", "HEAD") == head
    assert git("diff", "--cached", "--name-only") == ""

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
    settle_audit(audit_project, DISCARD, agent_maker=lambda *_: agents)


def test_discarding_an_audit_cut_off_at_the_questions_calls_no_checker(
    audit_project: Project,
) -> None:
    """Proves discarding an audit left at the questions gate ends it with no checker turn."""
    agents = cut_off_at_questions(audit_project)

    done = settle_audit(audit_project, DISCARD, agent_maker=lambda *_: agents)

    assert done.outcome == "discarded" and not find_problem_lists(audit_project)
    assert agents["checker"].prompts == []


def test_fresh_audits_as_a_first_audit_when_a_list_exists(audit_project: Project) -> None:
    """Proves fresh skips the rechecker even though the project has a problem list."""
    waiting_run(audit_project)
    settle_audit(audit_project, APPROVE)
    agents, maker = scripted(audit_project)

    run = start_audit(audit_project, agent_maker=maker, fresh=True, interactive=False)

    assert run.outcome == "waiting"
    assert agents["rechecker"].prompts == [] and agents["auditor"].prompts


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


def test_dropping_a_copy_ignores_a_path_that_is_not_a_copy(tmp_path: Path) -> None:
    """A stored path that is not a ratchet copy is left alone and raises nothing."""
    folder = tmp_path / "project"
    folder.mkdir()

    _drop_copy(folder)

    assert folder.is_dir()


def test_dropping_a_copy_ignores_a_copy_that_cannot_be_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A copy whose files are locked raises nothing, so the audit goes on."""
    def locked(copy: Path) -> None:
        raise OSError(f"{copy} is locked")

    monkeypatch.setattr("ratchet.audit.audit_run.remove_copy", locked)

    _drop_copy(tmp_path / "copy")


def test_audit_run_has_no_except_clause_that_only_passes() -> None:
    """Ratchet's own code checker finds no silent except clause in audit_run.py."""
    config = Config(test_command=["pytest"], editable=["src/ratchet/audit/audit_run.py"])
    project = Project(root=Path(__file__).parents[1], config=config, rules="rules")

    findings = check_architecture(project, None)

    assert [item.evidence for item in findings if item.kind == "silent_except"] == []


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


def published_old_list(project: Project) -> list:
    """Publishes a first audit with no terminal, ages its list so that a second list can be
    published in the same minute, and returns the list's problem entries."""
    waiting_run(project)
    settle_audit(project, APPROVE)
    entries = [entry for entry in find_latest_problem_list(project).entries if entry.is_problem]
    age_list(project)
    return entries


def reaudit_agents(project: Project, entries: list, blind: str) -> dict[str, ScriptedAgent]:
    """Returns scripted agents for a re-audit in which every old problem is open. The agent named
    'blind', which is "fresh" or "auditor", gives the audit that runs beside the rechecker."""
    turns = {blind: [fresh_turn(project)], "rechecker": [recheck_turn(entries)],
             "pair_judge": [judge_turn], "checker": [continuing_checker()]}
    return {name: ScriptedAgent(name, schema, turns.get(name, []))
            for name, schema in ANSWERS.items()}


def test_a_reaudit_whose_history_holds_ratchet_shows_the_note_and_publishes(
    audit_project: Project,
) -> None:
    """Proves a re-audit of a project with .ratchet committed on another branch shows the owner
    the note that names the commit, skips the blind audit, and publishes a list that says the
    audit was not blind."""
    entries = published_old_list(audit_project)
    root = audit_project.root
    commit = commit_ratchet(root)
    git_output(root, "update-ref", "refs/heads/other", commit)
    note = NOT_BLIND.format(commit=short_hash(root, commit))
    agents = reaudit_agents(audit_project, entries, "auditor")
    shown: list[str] = []

    run = start_audit(audit_project, agent_maker=lambda *_: agents, interactive=False,
                      show=shown.append)

    assert run.outcome == "waiting"
    assert note in shown
    assert agents["fresh"].prompts == [] and len(agents["auditor"].prompts) == 1

    done = settle_audit(audit_project, APPROVE, agent_maker=lambda *_: agents)

    assert done.outcome == "published"
    published = find_latest_problem_list(audit_project)
    assert published.name == done.list_name
    assert f"Not blind: {note}" in published.text.splitlines()


def test_a_reaudit_with_a_clean_history_runs_the_blind_audit(audit_project: Project) -> None:
    """Proves a re-audit of a project whose history never held .ratchet asks the blind audit and
    not the auditor, shows no note, and publishes a list with no "Not blind" line."""
    entries = published_old_list(audit_project)
    agents = reaudit_agents(audit_project, entries, "fresh")
    shown: list[str] = []

    run = start_audit(audit_project, agent_maker=lambda *_: agents, interactive=False,
                      show=shown.append)

    assert run.outcome == "waiting"
    assert len(agents["fresh"].prompts) == 1 and agents["auditor"].prompts == []
    assert not any("blind audit was skipped" in line for line in shown)

    done = settle_audit(audit_project, APPROVE, agent_maker=lambda *_: agents)

    assert done.outcome == "published"
    assert "Not blind" not in find_latest_problem_list(audit_project).text
