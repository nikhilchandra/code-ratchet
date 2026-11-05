"""Checks the functions that run an audit from the command line, with scripted agents."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from ratchet.architecture import check_architecture
from ratchet.audit.agent_answers import (
    AuditorAnswer,
    FinalJudgeAnswer,
    ListReview,
    DuplicateJudgeAnswer,
    OldListCheckerAnswer,
)
from ratchet.audit.audit_run import (
    APPROVE,
    DISCARD,
    AuditRun,
    _end_audit,
    _new_slug,
    settle_audit,
    start_audit,
)
from ratchet.audit.audit_graph import AuditError, build_audit_graph
from ratchet.audit.settings import AuditSettings
from ratchet.graph_lock import AUDIT, read_lock, take_lock
from ratchet.project import Config, Project
from ratchet.refactor.problem_list import find_latest_problem_list, find_problem_lists
from lock_files import FAKE_PID, lock_path, read_job_file
from scripted_agents import ScriptedAgent
from test_audit_graph import (  # noqa: F401
    NOT_BLIND,
    age_list,
    audit_project,
    auditor_turn,
    build_project,
    final_judge_turn,
    commit_ratchet,
    continuing_final_judge,
    default_turns,
    blind_auditor_turn,
    git_output,
    duplicate_judge_turn,
    old_list_checker_turn,
    short_hash,
)

ANSWERS = {"auditor": AuditorAnswer, "blind_auditor": AuditorAnswer, "rules_reviewer": ListReview,
           "standard_reviewer": ListReview, "old_list_checker": OldListCheckerAnswer,
           "duplicate_judge": DuplicateJudgeAnswer, "final_judge": FinalJudgeAnswer}


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

    from ratchet.graph_lock import LockError

    with pytest.raises(LockError, match="Finish or discard it first"):
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
                                      "final_judge": [final_judge_turn()]}
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
    """Proves HEAD is checked before the final judge is called."""
    agents = cut_off_at_questions(audit_project)
    (audit_project.root / "later.txt").write_text("later\n", encoding="utf-8")
    for command in (["add", "-A"], ["commit", "-q", "-m", "later"]):
        subprocess.run(["git", *command], cwd=audit_project.root, check=True, capture_output=True)

    with pytest.raises(AuditError, match="HEAD has moved"):
        settle_audit(audit_project, APPROVE, agent_maker=lambda *_: agents)

    assert agents["final_judge"].prompts == []
    settle_audit(audit_project, DISCARD, agent_maker=lambda *_: agents)


def test_discarding_an_audit_cut_off_at_the_questions_calls_no_final_judge(
    audit_project: Project,
) -> None:
    """Proves discarding an audit left at the questions gate ends it with no final judge turn."""
    agents = cut_off_at_questions(audit_project)

    done = settle_audit(audit_project, DISCARD, agent_maker=lambda *_: agents)

    assert done.outcome == "discarded" and not find_problem_lists(audit_project)
    assert agents["final_judge"].prompts == []


def test_fresh_audits_as_a_first_audit_when_a_list_exists(audit_project: Project) -> None:
    """Proves --fresh skips the old list checker even though the project has a problem list."""
    waiting_run(audit_project)
    settle_audit(audit_project, APPROVE)
    agents, maker = scripted(audit_project)

    run = start_audit(audit_project, agent_maker=maker, fresh=True, interactive=False)

    assert run.outcome == "waiting"
    assert agents["old_list_checker"].prompts == [] and agents["auditor"].prompts


def test_an_audit_after_one_that_halted_in_prepare_still_starts(audit_project: Project) -> None:
    """Proves an audit that halted on a dirty tree does not stop the next audit from reaching its
    draft."""
    stray = audit_project.root / "stray.txt"
    stray.write_text("stray\n", encoding="utf-8")
    _, maker = scripted(audit_project)
    with pytest.raises(AuditError, match="working tree"):
        start_audit(audit_project, agent_maker=maker, interactive=False)
    stray.unlink()

    assert waiting_run(audit_project).outcome == "waiting"


def test_an_audit_leaves_no_temp_folder_behind(audit_project: Project) -> None:
    """Proves an audit that halts on a dirty tree, and an audit that waits and is then approved,
    leave no folder whose name starts with 'ratchet-audit-' in the system's temp folder."""
    prefix = "ratchet-audit-"
    temp = Path(tempfile.gettempdir())
    before = {path.name for path in temp.iterdir() if path.name.startswith(prefix)}
    # halt one audit on a dirty tree, then run one that waits and approve it
    stray = audit_project.root / "stray.txt"
    stray.write_text("stray\n", encoding="utf-8")
    _, maker = scripted(audit_project)
    with pytest.raises(AuditError, match="working tree"):
        start_audit(audit_project, agent_maker=maker, interactive=False)
    stray.unlink()
    waiting_run(audit_project)
    settle_audit(audit_project, APPROVE)

    after = {path.name for path in temp.iterdir() if path.name.startswith(prefix)}
    assert after - before == set()


def test_audit_run_has_no_except_clause_that_only_passes() -> None:
    """Ratchet's own code checker finds no silent except clause in audit_run.py."""
    config = Config(test_command=["pytest"], editable=["src/ratchet/audit/audit_run.py"])
    project = Project(root=Path(__file__).parents[1], config=config, rules="rules")

    findings = check_architecture(project, None)

    assert [item.evidence for item in findings if item.kind == "silent_except"] == []


def test_ending_an_audit_whose_job_was_never_saved_releases_the_lock(
    audit_project: Project,
) -> None:
    """Proves ending an audit that took the lock but never saved its job raises nothing and
    leaves the project with no lock."""
    graph = build_audit_graph(audit_project, AuditSettings())
    slug = "audit-never-started"
    take_lock(audit_project, graph=AUDIT, job=slug, job_file=graph.state_dir / f"{slug}.json")

    _end_audit(audit_project, graph, slug)

    assert read_lock(audit_project) is None


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
    'blind', which is "blind_auditor" or "auditor", gives the audit that runs beside the old list checker."""
    turns = {blind: [blind_auditor_turn(project)],
             "old_list_checker": [old_list_checker_turn(entries)],
             "duplicate_judge": [duplicate_judge_turn],
             "final_judge": [continuing_final_judge()]}
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
    assert agents["blind_auditor"].prompts == [] and len(agents["auditor"].prompts) == 1

    done = settle_audit(audit_project, APPROVE, agent_maker=lambda *_: agents)

    assert done.outcome == "published"
    published = find_latest_problem_list(audit_project)
    assert published.name == done.list_name
    assert f"Not blind: {note}" in published.text.splitlines()


def test_a_reaudit_with_a_clean_history_runs_the_blind_audit(audit_project: Project) -> None:
    """Proves a re-audit of a project whose history never held .ratchet asks the blind audit and
    not the auditor, shows no note, and publishes a list with no "Not blind" line."""
    entries = published_old_list(audit_project)
    agents = reaudit_agents(audit_project, entries, "blind_auditor")
    shown: list[str] = []

    run = start_audit(audit_project, agent_maker=lambda *_: agents, interactive=False,
                      show=shown.append)

    assert run.outcome == "waiting"
    assert len(agents["blind_auditor"].prompts) == 1 and agents["auditor"].prompts == []
    assert not any("blind audit was skipped" in line for line in shown)

    done = settle_audit(audit_project, APPROVE, agent_maker=lambda *_: agents)

    assert done.outcome == "published"
    assert "Not blind" not in find_latest_problem_list(audit_project).text


def audit_job_file(project: Project) -> Path:
    """Returns the job file of the only audit that the project has started."""
    (path,) = (project.state_dir / "audits").glob("*.json")
    return path


def lock_of(project: Project):
    """Returns the project's graph lock, as read_lock gives it."""
    from ratchet.graph_lock import read_lock

    return read_lock(project)


def test_a_waiting_audit_holds_the_lock_at_its_draft(audit_project: Project) -> None:
    """Proves an audit that waits for the owner's decision on its draft keeps the lock, which
    names the audit and the draft gate."""
    run = waiting_run(audit_project)

    lock = lock_of(audit_project)
    assert (lock.graph, lock.job, lock.state, lock.gate) == ("audit", run.slug, "waiting", "draft")


def test_approving_or_discarding_releases_the_lock(audit_project: Project) -> None:
    """Proves settling a waiting audit either way removes the lock."""
    waiting_run(audit_project)
    settle_audit(audit_project, DISCARD)

    assert not lock_path(audit_project).exists()

    waiting_run(audit_project)
    settle_audit(audit_project, APPROVE)

    assert not lock_path(audit_project).exists()


def test_ctrl_c_while_asking_the_questions_keeps_the_audit_waiting(audit_project: Project) -> None:
    """Proves Ctrl-C at the owner's prompt leaves the audit waiting at the questions gate, with
    the lock kept for it."""
    cut_off_at_questions(audit_project)

    lock = lock_of(audit_project)
    assert (lock.graph, lock.state, lock.gate) == ("audit", "waiting", "questions")
    job = read_job_file(audit_job_file(audit_project))
    assert job["status"] == "waiting" and lock.job == job["slug"]


def test_ctrl_c_inside_a_resumed_step_halts_the_audit(audit_project: Project) -> None:
    """Proves Ctrl-C in a step after the owner answered the questions halts the audit, which
    rooster last saved as waiting, and removes the lock."""
    question = {"kind": "rules", "title": "Is rounding twice deliberate",
                "evidence": [], "why_it_may_be_deliberate": "Money is rounded at each step.",
                "what_would_settle": "A word from the owner."}

    def interrupt(prompt: str) -> dict:
        raise KeyboardInterrupt

    turns = default_turns(audit_project) | {
        "auditor": [auditor_turn(audit_project, questions=[question])], "final_judge": [interrupt]}
    agents = {name: ScriptedAgent(name, schema, turns.get(name, []))
              for name, schema in ANSWERS.items()}

    with pytest.raises(KeyboardInterrupt):
        start_audit(audit_project, agent_maker=lambda *_: agents, interactive=True,
                    ask=lambda prompt: "yes, it is deliberate", show=lambda line: None)

    assert len(agents["final_judge"].prompts) == 1
    assert read_job_file(audit_job_file(audit_project))["status"] == "halted"
    assert not lock_path(audit_project).exists()


def test_a_refused_settle_keeps_the_waiting_lock(audit_project: Project) -> None:
    """Proves approving after HEAD moved is refused with the lock left waiting, and discarding
    afterwards still releases it."""
    waiting_run(audit_project)
    lock_before = lock_path(audit_project).read_bytes()
    (audit_project.root / "later.txt").write_text("later\n", encoding="utf-8")
    for command in (["add", "-A"], ["commit", "-q", "-m", "later"]):
        subprocess.run(["git", *command], cwd=audit_project.root, check=True, capture_output=True)

    with pytest.raises(AuditError, match="HEAD has moved"):
        settle_audit(audit_project, APPROVE)

    assert lock_path(audit_project).read_bytes() == lock_before
    assert settle_audit(audit_project, DISCARD).outcome == "discarded"
    assert not lock_path(audit_project).exists()


def test_an_audit_killed_mid_publish_does_not_lock_the_project(audit_project: Project) -> None:
    """Proves an audit whose process died while it was settled, leaving the lock waiting and the
    job running, is halted by the next audit, which says so and reaches its own draft."""
    old = waiting_run(audit_project)
    job_file = audit_job_file(audit_project)
    job = read_job_file(job_file)
    job_file.write_text(json.dumps(job | {"status": "running"}), encoding="utf-8")
    lock = json.loads(lock_path(audit_project).read_text(encoding="utf-8"))
    lock_path(audit_project).write_text(json.dumps(lock | {"pid": FAKE_PID}), encoding="utf-8")
    _, maker = scripted(audit_project)
    shown: list[str] = []

    run = start_audit(audit_project, agent_maker=maker, interactive=False, show=shown.append)

    assert (f"The last audit ({old.slug}) stopped without finishing. It was halted."
            in shown)
    assert read_job_file(job_file)["status"] == "halted"
    assert run.outcome == "waiting" and run.slug != old.slug
    assert lock_of(audit_project).job == run.slug


def test_with_no_terminal_an_audit_goes_through_the_questions_unanswered(
    audit_project: Project,
) -> None:
    """Proves an audit that raises a question and has no terminal to ask resumes past the
    questions gate with no answers, and waits at its draft."""
    question = {"kind": "rules", "title": "Is rounding twice deliberate",
                "evidence": [], "why_it_may_be_deliberate": "Money is rounded at each step.",
                "what_would_settle": "A word from the owner."}
    turns = default_turns(audit_project) | {
        "auditor": [auditor_turn(audit_project, questions=[question])],
        "final_judge": [final_judge_turn()]}
    agents = {name: ScriptedAgent(name, schema, turns.get(name, []))
              for name, schema in ANSWERS.items()}

    run = start_audit(audit_project, agent_maker=lambda *_: agents, interactive=False)

    assert run.outcome == "waiting" and lock_of(audit_project).gate == "draft"


def test_an_audit_job_waiting_with_no_lock_refuses_a_new_audit(audit_project: Project) -> None:
    """Proves a waiting audit job from before the lock existed still refuses a new audit."""
    from ratchet.audit.audit_run import AFTER_DRAFT
    from lock_files import write_job_file

    write_job_file(audit_project.state_dir / "audits" / "audit-old.json", "waiting", AFTER_DRAFT)
    _, maker = scripted(audit_project)

    with pytest.raises(AuditError, match="audit-old is waiting for a decision"):
        start_audit(audit_project, agent_maker=maker, interactive=False)

    assert not lock_path(audit_project).exists()
