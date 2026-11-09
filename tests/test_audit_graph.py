"""Walks the audit graph with scripted agents, on a scratch project that has run init."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest
from rooster import JobStatus

from ratchet.audit.agent_answers import (
    AuditorAnswer,
    FinalJudgeAnswer,
    ListReview,
    DuplicateJudgeAnswer,
    OldListCheckerAnswer,
)
from ratchet.audit.agent_profiles import make_agents
from ratchet.audit.audit_graph import AuditError, build_audit_graph
from ratchet.audit.code_facts import build_inventory
from ratchet.audit.evidence_hints import old_citations
from ratchet.audit.list_markdown import QUESTIONS_HEADING, DraftQuestion
from ratchet.audit.settings import COMMAND, AuditSettings
from ratchet.init_project import init_project
from ratchet.owner_terminal import ask_questions
from ratchet.project import Project, load_project
from ratchet.problems_view import show_history
from ratchet.records import commit_records, records_log
from ratchet.refactor.ledger import ProblemEvent, ProblemStatus, load_ledger, save_ledger
from ratchet.refactor.problem_list import find_latest_problem_list, find_problem_lists, split_entries
from ratchet.target_design import read_target_design
from scripted_agents import ScriptedAgent, Turn

RULES = """\
Keep the core free of the outside world.

```toml
[parts]
core = "app.core"
cli = "app.cli"

[may_use]
core = []
cli = ["core"]

[may_touch_outside_world]
parts = ["cli"]
```

## Decided in audits

Rules that audits settled with the owner are added below.
"""

FILES = {
    "src/app/__init__.py": "",
    "src/app/core/__init__.py": "",
    "src/app/core/model.py": (
        '"""Holds the order model."""\n\nfrom app.cli import main\n\n\n'
        "def total(a, b):\n    return round(a + b, 2)\n"
    ),
    "src/app/cli/__init__.py": "",
    "src/app/cli/main.py": (
        "from app.core import model\n\n\ndef run():\n    return round(model.total(1, 2), 2)\n"
    ),
}
MODEL = "src/app/core/model.py"
MAIN = "src/app/cli/main.py"
SLUG = "audit-1"


def git(root: Path, *arguments: str) -> None:
    """Runs git in the project and checks that it worked."""
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


def build_project(root: Path, *, initialized: bool = True, template: bool = False) -> Project:
    """Makes a committed project with a core and a cli part, and returns it read from .ratchet.

    The core imports the cli, which the rules forbid, so the code checker has a stated finding.
    """
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "ratchet@example.com")
    git(root, "config", "user.name", "ratchet")
    git(root, "config", "core.autocrlf", "false")
    for name, text in FILES.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "start")
    rules = ("<!-- ratchet: template. Delete this line once the essential questions are answered. "
             "-->\n" if template else "") + RULES
    if initialized:
        init_project(root, assume_yes=False, confirm=None)
        (root / ".ratchet" / "rules.md").write_text(rules, encoding="utf-8", newline="\n")
        commit_records(root, "set the rules")
    else:
        (root / ".ratchet").mkdir()
        (root / ".ratchet" / "config.toml").write_text(
            'test_command = ["pytest"]\neditable = ["src/**"]\n', encoding="utf-8")
        (root / ".ratchet" / "rules.md").write_text(rules, encoding="utf-8")
    return load_project(root, commands=[COMMAND])


@pytest.fixture
def audit_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Project:
    """Returns a project that has run init, with the process in a folder of its own so that the
    scripted agents' relative log paths stay out of the project."""
    here = tmp_path / "cwd"
    here.mkdir()
    monkeypatch.chdir(here)
    return build_project(tmp_path / "project")


def finding(subject: str, title: str, file: str, line: int, quote: str, **changes: object) -> dict:
    """Returns a standard-grade finding that cites one line."""
    return {
        "subject": subject, "title": title, "what_is_wrong": f"{title}.", "grade": "standard",
        "breaks": "S7", "inferred_from": [], "history": "",
        "evidence": [{"file": file, "line": line, "end_line": line, "quote": quote,
                      "shows": "the line"}],
        "simpler_option": "Do it once.", "done_when": ["it is done once"], "waits_on": [],
        "tests": [],
    } | changes


FIRST = finding("app.cli.main.run", "run rounds the total again", MAIN, 5,
                "return round(model.total(1, 2), 2)")
SECOND = finding("app.core.model.total", "total rounds the sum", MODEL, 7, "return round(a + b, 2)")
ADDED = finding("app.core.model", "the model is not described", MODEL, 1,
                '"""Holds the order model."""')


def auditor_turn(project: Project, **changes: object) -> dict:
    """Returns the auditor's answer: a verdict on every inventory item and two problems."""
    inventory = build_inventory(project, read_target_design(project.rules))
    units = [{"name": item.name, "verdict": "fine", "note": ""} for item in inventory]
    return {"drift": "a little", "units": units, "problems": [FIRST, SECOND], "questions": [],
            "bugs": []} | changes


def review_turn(reason: str = "", **changes: object) -> dict:
    """Returns a reviewer's answer that agrees with both entries, giving 'reason' for each."""
    reviews = [{"label": label, "verdict": "agree", "reason": reason, "finding": entry}
               for label, entry in (("L1", FIRST), ("L2", SECOND))]
    return {"reviews": reviews, "added": [], "questions": [], "bugs": []} | changes


def rows_of(prompt: str) -> list[dict]:
    """Reads the first JSON block of a prompt."""
    return json.loads(prompt.split("```json\n", 1)[1].split("\n```", 1)[0])


def duplicate_judge_turn(prompt: str) -> dict:
    """Answers every pair as different."""
    return {"pairs": [{"pair": row["pair"], "same": False, "reason": "different"}
                      for row in rows_of(prompt)]}


def final_judge_turn(seen: list[str] | None = None, decisions: list[dict] | None = None) -> Turn:
    """Returns a final judge turn that keeps every group it is given as it reads, and decides the
    owner's answers as given. The keys of the groups it saw are added to 'seen'."""
    def turn(prompt: str) -> dict:
        rows = rows_of(prompt)
        if seen is not None:
            seen.extend(row["key"] for row in rows)
        checks = [{
            "key": row["key"], "outcome": "keep", "merge_into": "", "reason": "it holds",
            "entries": [{"continues_old": False, "finding": {
                name: value for name, value in row["text"].items()
                if name not in ("verdicts", "old_verdict")}}],
        } for row in rows]
        return {"checks": checks, "decisions": decisions or []}
    return turn


def default_turns(project: Project) -> dict[str, list[Turn]]:
    """Returns the turns of a first audit in which every agent answers."""
    return {
        "auditor": [auditor_turn(project)],
        "rules_reviewer": [review_turn(added=[ADDED])],
        "standard_reviewer": [review_turn()],
        "duplicate_judge": [duplicate_judge_turn],
        "final_judge": [final_judge_turn()],
    }


@pytest.fixture
def start(audit_project: Project):
    """Returns a function that starts an audit with scripted turns and returns the job, the
    agents and the graph. An agent named in 'costs' spends that many dollars a turn, and every
    other agent spends 0.5 dollars."""
    def run(turns: dict[str, list[Turn]] | None = None, settings: AuditSettings | None = None,
            prompt: str = "", slug: str = SLUG, costs: dict[str, float] | None = None):
        answers = {"auditor": AuditorAnswer, "blind_auditor": AuditorAnswer, "rules_reviewer": ListReview,
                   "standard_reviewer": ListReview, "old_list_checker": OldListCheckerAnswer,
                   "duplicate_judge": DuplicateJudgeAnswer, "final_judge": FinalJudgeAnswer}
        given = turns or default_turns(audit_project)
        agents = {name: ScriptedAgent(name, schema, given.get(name, []),
                                      cost_usd=(costs or {}).get(name, 0.5))
                  for name, schema in answers.items()}
        graph = build_audit_graph(audit_project, settings or AuditSettings(),
                                  lambda *_: agents)
        return graph.start(prompt, slug=slug), agents, graph
    return run


def audit_folder(project: Project, slug: str = SLUG) -> Path:
    """Returns the folder of the audit that the tests start."""
    return project.state_dir / "audits" / slug


def test_a_full_walk_publishes_a_readable_list_and_commits(
    audit_project: Project, start
) -> None:
    """Proves a first audit walks every step, waits at the draft, and on approval writes a list
    whose problems have opened events, and commits the records."""
    seen: list[str] = []
    turns = default_turns(audit_project) | {"final_judge": [final_judge_turn(seen)]}
    job, agents, graph = start(turns, prompt="look at rounding")

    assert job.status == JobStatus.WAITING and job.next_step == "publish"
    folder = audit_folder(audit_project)
    for name in ("code-findings.json", "inventory.json", "auditor.json", "rules_reviewer.json",
                 "standard_reviewer.json", "groups.json", "pairs.json", "citations.md", "final_judge.json",
                 "verdicts.md", "draft.json", "draft.md"):
        assert (folder / name).is_file(), name

    job = graph.resume(SLUG, {"decision": "approve"})

    assert job.status == JobStatus.DONE and job.data["outcome"] == "published"
    published = find_latest_problem_list(audit_project)
    assert published.name == job.data["list"]
    assert published.ids == [f"P{number}" for number in range(1, len(published.ids) + 1)]
    assert len(published.ids) >= 3
    entries = {entry.id: entry for entry in published.entries}
    assert all(entry.fields["Status"] == "open" for entry in entries.values())
    ledger = load_ledger(audit_project.state_dir / published.name / "ledger.json", published.name)
    for problem_id in published.ids:
        events = ledger.problems[problem_id].events
        assert [(event.kind, event.by, event.note) for event in events] == [
            ("opened", "audit", SLUG)]
    assert records_log(audit_project.root)[0].endswith(f"audit: publish {published.name}")
    assert (folder / "published.txt").is_file()
    assert "Owner note" not in published.text


def test_the_auditor_is_asked_with_the_request(audit_project: Project, start) -> None:
    """Proves the owner's request reaches the auditor's prompt."""
    job, agents, graph = start(prompt="look at rounding")

    assert "look at rounding" in agents["auditor"].prompts[0]
    assert "app.cli.main" in agents["auditor"].prompts[0]
    assert "rules_reviewer" not in agents["auditor"].prompts[0]


def test_a_stated_code_finding_skips_the_final_judge_but_is_listed(
    audit_project: Project, start
) -> None:
    """Proves a group that is one stated code finding is not shown to the final judge, and still
    comes out as a problem graded stated."""
    seen: list[str] = []
    job, agents, graph = start(default_turns(audit_project) | {"final_judge": [final_judge_turn(seen)]})
    folder = audit_folder(audit_project)
    groups = json.loads((folder / "groups.json").read_text(encoding="utf-8"))["groups"]
    skipped = [group["key"] for group in groups
               if len(group["items"]) == 1 and group["items"][0]["source"] == "C"
               and group["text"]["grade"] == "stated"]

    assert skipped and not set(skipped) & set(seen)
    graph.resume(SLUG, {"decision": "approve"})
    stated = [entry for entry in find_latest_problem_list(audit_project).entries
              if entry.fields["Grade"].startswith("stated")]
    assert stated and stated[0].fields["Sources"].startswith("C")


def test_the_duplicate_judge_is_asked_and_every_pair_is_recorded(
    audit_project: Project, start
) -> None:
    """Proves pairs that the rules cannot settle go to the duplicate judge and into pairs.json."""
    job, agents, graph = start()

    assert len(agents["duplicate_judge"].prompts) == 1
    records = json.loads((audit_folder(audit_project) / "pairs.json").read_text(encoding="utf-8"))
    assert records and all(record["same"] is False for record in records)


def test_the_questions_gate_waits_and_the_owners_answers_reach_the_final_judge(
    audit_project: Project, start
) -> None:
    """Proves a question stops the job at the gate, and that an answer reaches the final judge's
    prompt and a rule decision is added to rules.md. The rules reviewer, the standard reviewer
    and the final judge write their answers under their own names, and the reviewers' reasons
    reach the final judge from those files."""
    question = {"kind": "rules", "title": "Is rounding twice deliberate",
                "evidence": [], "why_it_may_be_deliberate": "Money is rounded at each step.",
                "what_would_settle": "A word from the owner."}
    rule = {"label": "Q1", "decision": "rule", "reason": "settled", "finding": SECOND,
            "rule_text": "Amounts are rounded to cents in one place only.", "conflicts_with": []}
    turns = default_turns(audit_project) | {
        "auditor": [auditor_turn(audit_project, questions=[question])],
        "rules_reviewer": [review_turn("agreed against the rules")],
        "standard_reviewer": [review_turn("agreed against the standard")],
        "final_judge": [final_judge_turn(decisions=[rule])],
    }
    job, agents, graph = start(turns)
    folder = audit_folder(audit_project)
    for name in ("rules_reviewer.json", "standard_reviewer.json"):
        assert (folder / name).is_file(), name
    for name in ("review-a.json", "review-b.json"):
        assert not (folder / name).exists(), name

    assert job.status == JobStatus.WAITING and job.next_step == "check"
    assert job.data["questions"][0]["id"] == "Q1"
    assert "Is rounding twice deliberate" in (audit_folder(audit_project) / "questions.md").read_text(
        encoding="utf-8")
    assert agents["final_judge"].prompts == []

    job = graph.resume(SLUG, {"answers": {"Q1": "Round only in pricing.", "Q9": " "}})

    assert "Round only in pricing." in agents["final_judge"].prompts[0]
    assert "agreed against the rules" in agents["final_judge"].prompts[0]
    assert "agreed against the standard" in agents["final_judge"].prompts[0]
    assert (folder / "final_judge.json").is_file() and not (folder / "check.json").exists()
    assert job.status == JobStatus.WAITING and job.next_step == "publish"
    graph.resume(SLUG, {"decision": "approve"})
    rules = (audit_project.folder / "rules.md").read_text(encoding="utf-8")
    assert (f"- Amounts are rounded to cents in one place only. (decided in {SLUG})\n"
            in rules)
    assert rules.index("## Decided in audits") < rules.index("Amounts are rounded")
    assert [entry.id for entry in find_latest_problem_list(audit_project).entries
            if not entry.is_problem] == []


def wait_at_questions(project: Project, start, slug: str = SLUG):
    """Starts an audit whose auditor asks one question, so that the job waits at the questions
    gate, and returns the job, the agents and the graph."""
    question = {"kind": "rules", "title": "Is rounding twice deliberate",
                "evidence": [], "why_it_may_be_deliberate": "Money is rounded at each step.",
                "what_would_settle": "A word from the owner."}
    turns = default_turns(project) | {
        "auditor": [auditor_turn(project, questions=[question])],
        "final_judge": [final_judge_turn()],
    }
    job, agents, graph = start(turns, slug=slug)
    assert job.status == JobStatus.WAITING and job.next_step == "check"
    return job, agents, graph


def commit_later(project: Project) -> None:
    """Commits a new file later.py in the project, which moves HEAD."""
    (project.root / "later.py").write_text("x = 1\n", encoding="utf-8")
    git(project.root, "add", "-A")
    git(project.root, "commit", "-q", "-m", "later")


def test_a_moved_head_stops_the_audit_at_check(audit_project: Project, start) -> None:
    """Proves a commit made while the job waits at the questions gate halts the check before the
    final judge is asked, with a reason that asks for a new audit."""
    job, agents, graph = wait_at_questions(audit_project, start)
    commit_later(audit_project)

    with pytest.raises(AuditError) as caught:
        graph.resume(SLUG, {"answers": {"Q1": "yes"}})

    assert "HEAD has moved" in str(caught.value)
    assert re.search(r"(?i)run `ratchet audit` again", str(caught.value))
    assert graph.load(SLUG).status == JobStatus.HALTED
    assert agents["final_judge"].prompts == []


def test_a_dirty_tree_stops_the_audit_at_check(audit_project: Project, start) -> None:
    """Proves a file left uncommitted in the project while the job waits at the questions gate
    halts the check before the final judge is asked, with a reason that names the file and asks for
    a new audit."""
    job, agents, graph = wait_at_questions(audit_project, start)
    (audit_project.root / "stray.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(AuditError) as caught:
        graph.resume(SLUG, {"answers": {"Q1": "yes"}})

    assert "working tree has changes" in str(caught.value)
    assert "stray.py" in str(caught.value)
    assert re.search(r"(?i)run `ratchet audit` again", str(caught.value))
    assert graph.load(SLUG).status == JobStatus.HALTED
    assert agents["final_judge"].prompts == []


def test_a_discard_at_the_questions_gate_ignores_a_moved_head(
    audit_project: Project, start
) -> None:
    """Proves a discard decided at the questions gate ends the job as discarded, with no final judge
    turn, even after a commit has moved HEAD."""
    job, agents, graph = wait_at_questions(audit_project, start)
    commit_later(audit_project)

    job = graph.resume(SLUG, {"decision": "discard"})

    assert job.status == JobStatus.DONE and job.data["outcome"] == "discarded"
    assert agents["final_judge"].prompts == []


def test_an_answered_question_can_become_a_problem(audit_project: Project, start) -> None:
    """Proves a question the final judge turns into a problem is listed with a new id."""
    question = {"kind": "design", "title": "Is the tax in the report", "evidence": [],
                "why_it_may_be_deliberate": "x", "what_would_settle": "y"}
    problem = finding("app.core.model", "tax belongs in the model", MODEL, 7, "return round(a + b, 2)")
    decision = {"label": "Q1", "decision": "problem", "reason": "the owner agrees",
                "finding": problem, "rule_text": "", "conflicts_with": []}
    turns = default_turns(audit_project) | {
        "auditor": [auditor_turn(audit_project, questions=[question])],
        "final_judge": [final_judge_turn(decisions=[decision])],
    }
    job, agents, graph = start(turns)
    graph.resume(SLUG, {"answers": {"Q1": "yes, move it"}})
    graph.resume(SLUG, {"decision": "approve"})

    titles = [entry.title for entry in find_latest_problem_list(audit_project).entries]
    assert "tax belongs in the model" in titles
    entry = next(entry for entry in find_latest_problem_list(audit_project).entries
                 if entry.title == "tax belongs in the model")
    assert entry.fields["Status"] == "open"


def test_the_citations_of_a_question_made_problem_are_checked(
    audit_project: Project, start
) -> None:
    """Proves the final judge's finding for an answered question has its moved citation fixed and its
    unmatched citation removed."""
    question = {"kind": "design", "title": "Is the tax in the report", "evidence": [],
                "why_it_may_be_deliberate": "x", "what_would_settle": "y"}
    problem = finding("app.core.model", "tax belongs in the model", MODEL, 2, "return round(a + b, 2)")
    problem["evidence"].append({"file": MODEL, "line": 3, "end_line": 3,
                                "quote": "this text is not in the file", "shows": "nothing"})
    decision = {"label": "Q1", "decision": "problem", "reason": "the owner agrees",
                "finding": problem, "rule_text": "", "conflicts_with": []}
    turns = default_turns(audit_project) | {
        "auditor": [auditor_turn(audit_project, questions=[question])],
        "final_judge": [final_judge_turn(decisions=[decision])],
    }
    job, agents, graph = start(turns)
    graph.resume(SLUG, {"answers": {"Q1": "yes, move it"}})

    final = json.loads((audit_folder(audit_project) / "final.json").read_text(encoding="utf-8"))
    evidence = final["decisions"][0]["finding"]["evidence"]
    assert [(cited["line"], cited["quote"]) for cited in evidence] == [(7, "return round(a + b, 2)")]
    assert any("Q1" in note and "were removed" in note for note in final["notes"])


def test_a_question_nobody_answered_stays_in_the_list(audit_project: Project, start) -> None:
    """Proves a question left unanswered goes to the list's questions with an empty answer."""
    question = {"kind": "design", "title": "Is the tax in the report", "evidence": [],
                "why_it_may_be_deliberate": "x", "what_would_settle": "y"}
    turns = default_turns(audit_project) | {
        "auditor": [auditor_turn(audit_project, questions=[question])]}
    job, agents, graph = start(turns)
    graph.resume(SLUG)
    graph.resume(SLUG, {"decision": "approve"})

    published = find_latest_problem_list(audit_project)
    assert [entry.id for entry in published.entries if not entry.is_problem] == ["Q1"]
    assert "Answer:" in published.text


def test_a_failed_reviewer_does_not_stop_the_audit(audit_project: Project, start) -> None:
    """Proves the audit goes on with one reviewer's work when the other gives no answer, and
    tells the owner."""
    turns = default_turns(audit_project) | {"rules_reviewer": [None]}
    job, agents, graph = start(turns)

    folder = audit_folder(audit_project)
    assert job.status == JobStatus.WAITING
    assert not (folder / "rules_reviewer.json").exists() and (folder / "standard_reviewer.json").is_file()
    assert "rules_reviewer gave no answer" in (folder / "draft.md").read_text(encoding="utf-8")
    assert "Owner note" not in graph.resume(SLUG, {"decision": "approve"}).data["list"]
    assert "gave no answer" not in find_latest_problem_list(audit_project).text


def test_a_missing_verdict_is_noted(audit_project: Project, start) -> None:
    """Proves the audit tells the owner of an inventory item with no verdict."""
    def auditor(prompt: str) -> dict:
        answer = auditor_turn(audit_project)
        answer["units"] = answer["units"][:-1]
        return answer

    job, agents, graph = start(default_turns(audit_project) | {"auditor": [auditor]})

    text = (audit_folder(audit_project) / "draft.md").read_text(encoding="utf-8")
    assert "gave no verdict on 1 items of the inventory" in text


def running_bash(name: str, command: str, answer: Turn) -> Turn:
    """Returns a turn that writes a log in which the agent 'name' ran one Bash command, and then
    gives the answer of the turn 'answer'."""
    message = json.dumps({"content": [{"name": "Bash", "input": {"command": command}}]})

    def turn(prompt: str) -> dict:
        Path(f"{name}.log").write_text("--- messages ---\n" + message + "\n", encoding="utf-8")
        return answer(prompt) if callable(answer) else answer
    return turn


def halted_jobs(project: Project) -> list:
    """Returns the audit's jobs."""
    return build_audit_graph(project, AuditSettings()).jobs()


def test_an_agent_that_passes_output_to_git_halts_the_audit(
    audit_project: Project, start
) -> None:
    """Proves an auditor that ran git with --output, which writes a file, halts the audit before
    the review."""
    rules = (audit_project.root / ".ratchet" / "rules.md").as_posix()
    auditor = running_bash("auditor", f"git log -1 --output={rules}", auditor_turn(audit_project))

    with pytest.raises(AuditError, match="--output"):
        start(default_turns(audit_project) | {"auditor": [auditor]})
    jobs = halted_jobs(audit_project)

    assert [job.status for job in jobs] == [JobStatus.HALTED]
    assert "--output" in jobs[0].halt_reason
    folder = audit_folder(audit_project)
    assert not (folder / "rules_reviewer.json").exists() and not (folder / "draft.md").exists()


def test_a_rules_reviewer_that_passes_output_to_git_halts_the_audit(
    audit_project: Project, start
) -> None:
    """Proves a reviewer that ran git with --output halts the audit, as the auditor does."""
    reviewer = running_bash("rules_reviewer", "git diff --output=out.txt", review_turn())

    with pytest.raises(AuditError, match="--output"):
        start(default_turns(audit_project) | {"rules_reviewer": [reviewer]})
    jobs = halted_jobs(audit_project)

    assert [job.status for job in jobs] == [JobStatus.HALTED]
    assert "--output" in jobs[0].halt_reason
    assert not (audit_folder(audit_project) / "draft.md").exists()


def test_the_cost_cap_halts_the_job_before_a_call(audit_project: Project, start) -> None:
    """Proves a step whose budgets could pass the cap is not started, and that earlier spending
    counts toward the cap."""
    with pytest.raises(AuditError, match="cap"):
        start(settings=AuditSettings(max_cost_usd=5.0))
    assert not (audit_project.state_dir / "audits" / SLUG / "prompts").exists()

    agents = {}

    def make(*_: object) -> dict:
        agents.update({"auditor": ScriptedAgent("auditor", AuditorAnswer,
                                               [auditor_turn(audit_project)])})
        return agents

    graph = build_audit_graph(audit_project, AuditSettings(max_cost_usd=12.0), make)
    with pytest.raises(AuditError, match="cap"):
        graph.start("", slug="capped")
    job = graph.load("capped")
    assert job.status == JobStatus.HALTED and "cap" in job.halt_reason
    assert len(agents["auditor"].prompts) == 1 and job.cost_usd == pytest.approx(0.5)


@pytest.mark.parametrize(("rules_budget", "standard_budget"), [(1.0, 5.0), (5.0, 1.0)])
def test_the_review_step_counts_both_reviewers_budgets_toward_the_cap(
    audit_project: Project, start, rules_budget: float, standard_budget: float
) -> None:
    """Proves the cost check before the review adds the rules reviewer's budget to the standard
    reviewer's. The auditor's budget of 1 dollar fits the cap of 4 dollars, and after the
    auditor spends 0.5 dollars, two budgets of 1 dollar would fit too. A budget of 1 dollar and
    one of 5 dollars do not, so the audit halts before either reviewer is asked."""
    role = AuditSettings().auditor
    settings = AuditSettings(
        max_cost_usd=4.0,
        auditor=role.model_copy(update={"budget_usd": 1.0}),
        rules_reviewer=role.model_copy(update={"budget_usd": rules_budget}),
        standard_reviewer=role.model_copy(update={"budget_usd": standard_budget}),
    )

    with pytest.raises(AuditError, match="cap"):
        start(settings=settings)
    job = halted_jobs(audit_project)[0]

    assert job.status == JobStatus.HALTED and job.halt_reason.startswith("step 'review'")
    assert "$6.00" in job.halt_reason
    prompts = audit_folder(audit_project) / "prompts"
    assert (prompts / "auditor-prompt.md").is_file()
    for name in ("rules_reviewer", "standard_reviewer"):
        assert not (prompts / f"{name}-prompt.md").exists(), name


def test_discard_writes_no_list(audit_project: Project, start) -> None:
    """Proves discarding ends the job with a note and leaves the records as they were."""
    job, agents, graph = start()
    before = records_log(audit_project.root)

    job = graph.resume(SLUG, {"decision": "discard"})

    assert job.status == JobStatus.DONE and job.data["outcome"] == "discarded"
    assert (audit_folder(audit_project) / "discarded.txt").is_file()
    assert find_problem_lists(audit_project) == []
    assert records_log(audit_project.root) == before


def test_approval_is_refused_when_head_has_moved(audit_project: Project, start) -> None:
    """Proves a list is not published for code that has changed since the audit began."""
    job, agents, graph = start()
    (audit_project.root / "later.py").write_text("x = 1\n", encoding="utf-8")
    git(audit_project.root, "add", "-A")
    git(audit_project.root, "commit", "-q", "-m", "later")

    with pytest.raises(AuditError, match="HEAD has moved"):
        graph.resume(SLUG, {"decision": "approve"})
    assert find_problem_lists(audit_project) == []


def test_a_dirty_tree_stops_publish(audit_project: Project, start) -> None:
    """Proves a list is not published while the project holds a file that the audit did not see."""
    job, agents, graph = start()
    (audit_project.root / "stray.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(AuditError, match="working tree has changes"):
        graph.resume(SLUG, {"decision": "approve"})
    assert find_problem_lists(audit_project) == []


def test_publish_needs_a_decision(audit_project: Project, start) -> None:
    """Proves the draft gate does not publish when it is resumed without a decision."""
    job, agents, graph = start()

    with pytest.raises(AuditError, match="decision"):
        graph.resume(SLUG)


def test_an_edited_draft_is_what_gets_published(audit_project: Project, start) -> None:
    """Proves the owner's edits to draft.md reach the list, and an edit that breaks the draft
    is refused."""
    job, agents, graph = start()
    path = audit_folder(audit_project) / "draft.md"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("total rounds the sum", "total rounds the sum, edited"),
                    encoding="utf-8")

    graph.resume(SLUG, {"decision": "approve"})

    assert "total rounds the sum, edited" in find_latest_problem_list(audit_project).text


def test_a_broken_draft_is_refused(audit_project: Project, start) -> None:
    """Proves publish checks the draft file again, as an owner's edit may have broken it."""
    job, agents, graph = start()
    path = audit_folder(audit_project) / "draft.md"
    path.write_text(re.sub(r"Status: open", "Status: sideways", path.read_text(encoding="utf-8"),
                           count=1), encoding="utf-8")

    with pytest.raises(AuditError, match="sideways"):
        graph.resume(SLUG, {"decision": "approve"})


def test_a_template_for_rules_stops_the_audit(tmp_path: Path) -> None:
    """Proves the audit refuses while rules.md still has the template marker."""
    project = build_project(tmp_path / "project", template=True)
    graph = build_audit_graph(project, AuditSettings(), lambda *_: {})

    with pytest.raises(AuditError, match="template"):
        graph.start("", slug=SLUG)


def test_a_project_that_has_not_run_init_stops_the_audit(tmp_path: Path) -> None:
    """Proves the audit refuses before init and names the command."""
    project = build_project(tmp_path / "project", initialized=False)
    graph = build_audit_graph(project, AuditSettings(), lambda *_: {})

    with pytest.raises(AuditError, match="ratchet init"):
        graph.start("", slug=SLUG)


def test_a_dirty_working_tree_stops_the_audit(audit_project: Project) -> None:
    """Proves the audit refuses when the tree differs from HEAD, and makes no copy."""
    (audit_project.root / "stray.py").write_text("x = 1\n", encoding="utf-8")
    graph = build_audit_graph(audit_project, AuditSettings(), lambda *_: {})

    with pytest.raises(AuditError, match="stray.py"):
        graph.start("", slug=SLUG)


def graph_edges(graph) -> tuple[set[tuple[str, str]], set[str]]:
    """Reads a graph's diagram as its arrows between step names, and its gates."""
    labels = dict(re.findall(r'(s\d+)\["(.+?)"\]', graph.diagram()))
    names = {node: label.removesuffix(" (gate)") for node, label in labels.items()}
    edges = set()
    for source, target in re.findall(r"(s\d+) --> (\S+)", graph.diagram()):
        edges.add((names[source], "finish" if target.startswith("finish") else names[target]))
    return edges, {names[node] for node, label in labels.items() if label.endswith("(gate)")}


def test_the_diagram_matches_the_table(audit_project: Project) -> None:
    """Proves the graph has the steps, the arrows and the two gates of the design's table."""
    edges, gates = graph_edges(build_audit_graph(audit_project, AuditSettings()))

    assert edges == {
        ("prepare", "audit"),
        ("audit", "review"), ("audit", "group"),
        ("review", "group"),
        ("group", "check-citations"),
        ("check-citations", "questions"), ("check-citations", "check"),
        ("questions", "check"),
        ("check", "draft"), ("check", "publish"),
        ("draft", "publish"),
        ("publish", "finish"),
    }
    assert gates == {"questions", "draft"}


def test_the_prompts_and_profiles_are_kept(audit_project: Project, start) -> None:
    """Proves each agent's prompt is saved in the audit's folder."""
    job, agents, graph = start()

    prompts = sorted(path.name for path in (audit_folder(audit_project) / "prompts").iterdir())
    assert prompts == ["auditor-prompt.md", "duplicate_judge-prompt.md", "final_judge-prompt.md",
                       "rules_reviewer-prompt.md", "standard_reviewer-prompt.md"]


# ---------------------------------------------------------------- the re-audit

AGAIN = "audit-2"
OLD_NAME = "problems-20260101-10h00m"


def age_list(project: Project) -> str:
    """Renames the newest list and its ledger to an older time, so that a second list can be
    published in the same minute, and returns the new name."""
    latest = find_latest_problem_list(project).name
    (project.folder / f"{latest}.md").rename(project.folder / f"{OLD_NAME}.md")
    shutil.move(project.state_dir / latest, project.state_dir / OLD_NAME)
    path = project.state_dir / OLD_NAME / "ledger.json"
    ledger = load_ledger(path, OLD_NAME)
    ledger.problem_list = OLD_NAME
    save_ledger(ledger, path)
    return OLD_NAME


@pytest.fixture
def old_list(audit_project: Project, start) -> list:
    """Publishes a first audit, ages its list, and returns the list's problem entries."""
    start()[2].resume(SLUG, {"decision": "approve"})
    entries = [entry for entry in find_latest_problem_list(audit_project).entries
               if entry.is_problem]
    age_list(audit_project)
    return entries


def entry_titled(entries: list, title: str):
    """Returns the entry with a title."""
    return next(entry for entry in entries if entry.title == title)


def old_check(entry, verdict: str, left: str = "") -> dict:
    """Returns the old list checker's check of an old entry, citing the entry's first place as it stands."""
    places = old_citations(entry)
    if places:
        place = places[0]
        current = finding(entry.fields["Subject"], entry.title, place.file, place.line, place.quote)
        current["evidence"][0]["end_line"] = place.end_line
    else:
        # an entry with no evidence is cited at the first line of the cli module
        current = finding(entry.fields["Subject"], entry.title, MAIN, 1, "from app.core import model")
    return {"id": entry.id, "verdict": verdict, "what_is_left": left, "finding": current}


def old_list_checker_turn(entries: list, verdicts: dict[str, str] | None = None, left: str = "",
                 **changes: object) -> dict:
    """Returns the old list checker's answer, with a check for every entry. The verdict of an entry is
    looked up by its title and is open when the title is not given."""
    given = verdicts or {}
    checks = [old_check(entry, given.get(entry.title, "open"), left) for entry in entries]
    return {"checks": checks, "answered_questions": [], "from_reviewer_notes": []} | changes


def blind_auditor_turn(project: Project, problems: list[dict] | None = None) -> dict:
    """Returns the blind auditor's answer, with a verdict on every inventory item."""
    return auditor_turn(project, problems=problems or [])


def continuing_final_judge(split: dict[str, list[dict]] | None = None) -> Turn:
    """Returns a final judge turn that keeps every group and marks the entry of an old problem as
    continuing it. A group whose old id is a key of 'split' is split into the given entries."""
    def turn(prompt: str) -> dict:
        checks = []
        for row in rows_of(prompt):
            text = {name: value for name, value in row["text"].items()
                    if name not in ("verdicts", "old_verdict")}
            if row["old_id"] in (split or {}):
                outcome = "split"
                entries = [{"continues_old": number == 0, "finding": entry}
                           for number, entry in enumerate(split[row["old_id"]])]
            else:
                outcome = "keep"
                entries = [{"continues_old": bool(row["old_id"]), "finding": text}]
            checks.append({"key": row["key"], "outcome": outcome, "merge_into": "",
                           "reason": "it holds", "entries": entries})
        return {"checks": checks, "decisions": []}
    return turn


def reaudit_turns(project: Project, entries: list, **changes: object) -> dict[str, list[Turn]]:
    """Returns the turns of a re-audit in which every agent answers and every old problem is open."""
    return {
        "blind_auditor": [blind_auditor_turn(project)],
        "old_list_checker": [old_list_checker_turn(entries)],
        "duplicate_judge": [duplicate_judge_turn],
        "final_judge": [continuing_final_judge()],
    } | changes


def test_a_blind_audit_that_diffs_the_old_list_through_dotdot_halts_the_job(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves a blind audit that reads the old list through a relative '..' path, as in
    'git diff --no-index', stops the job."""
    # the copy is a folder directly under the temp folder, so one '..' reaches the temp folder
    old = audit_project.root / ".ratchet" / "problems.md"
    climb = "../" + Path(os.path.relpath(old, tempfile.gettempdir())).as_posix()
    command = json.dumps(f"git diff --no-index {climb} /dev/null")
    message = '{"content": [{"name": "Bash", "input": {"command": %s}}]}' % command

    def blind_auditor(prompt: str) -> dict:
        Path("blind_auditor.log").write_text("--- messages ---\n" + message + "\n", encoding="utf-8")
        return blind_auditor_turn(audit_project)

    with pytest.raises(AuditError, match="blind auditor"):
        start(reaudit_turns(audit_project, old_list) | {"blind_auditor": [blind_auditor]}, slug=AGAIN)
    assert not (audit_folder(audit_project, AGAIN) / "blind_auditor.json").exists()


def test_a_blind_audit_that_reads_through_home_halts_the_job(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves a blind audit that reads the old list through a path that starts with $HOME, as in
    'git diff --no-index', stops the job."""
    command = json.dumps("git diff --no-index $HOME/code/x/.ratchet/problems-x.md /dev/null")
    message = '{"content": [{"name": "Bash", "input": {"command": %s}}]}' % command

    def blind_auditor(prompt: str) -> dict:
        Path("blind_auditor.log").write_text("--- messages ---\n" + message + "\n", encoding="utf-8")
        return blind_auditor_turn(audit_project)

    with pytest.raises(AuditError, match="blind auditor"):
        start(reaudit_turns(audit_project, old_list) | {"blind_auditor": [blind_auditor]}, slug=AGAIN)
    assert not (audit_folder(audit_project, AGAIN) / "blind_auditor.json").exists()


def test_a_blind_audit_that_cds_to_the_root_halts_the_job(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves a blind audit that goes to the root with 'cd /' and then reads the old list through a
    path relative to the root stops the job."""
    old = (audit_project.folder / f"{OLD_NAME}.md").resolve()
    below_root = old.relative_to(old.anchor).as_posix()
    command = json.dumps(f"cd / && cat {below_root}")
    message = '{"content": [{"name": "Bash", "input": {"command": %s}}]}' % command

    def blind_auditor(prompt: str) -> dict:
        Path("blind_auditor.log").write_text("--- messages ---\n" + message + "\n", encoding="utf-8")
        return blind_auditor_turn(audit_project)

    with pytest.raises(AuditError, match="blind auditor"):
        start(reaudit_turns(audit_project, old_list) | {"blind_auditor": [blind_auditor]}, slug=AGAIN)
    assert not (audit_folder(audit_project, AGAIN) / "blind_auditor.json").exists()


def test_a_reaudit_runs_the_blind_auditor_and_the_old_list_checker_and_skips_the_rest(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves a project with a list gets a re-audit in which only the blind auditor, the old list
    checker, the duplicate judge and the final judge are asked, which waits at the draft, and
    whose blind auditor and old list checker write their answers under their own names."""
    job, agents, graph = start(reaudit_turns(audit_project, old_list), slug=AGAIN)

    folder = audit_folder(audit_project, AGAIN)
    assert job.status == JobStatus.WAITING and job.next_step == "publish"
    assert job.data["reaudit"] and job.data["old_list"] == OLD_NAME
    for name in ("evidence-hints.md", "blind_auditor.json", "old_list_checker.json", "groups.json", "draft.md"):
        assert (folder / name).is_file(), name
    assert not (folder / "auditor.json").exists() and not (folder / "rules_reviewer.json").exists()
    for name in ("auditor", "rules_reviewer", "standard_reviewer"):
        assert agents[name].prompts == []
    assert len(agents["blind_auditor"].prompts) == 1 and len(agents["old_list_checker"].prompts) == 1
    for name in ("fresh.json", "recheck.json"):
        assert not (folder / name).exists(), name


def test_the_blind_audits_prompt_holds_none_of_the_old_titles(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves the blind auditor is not told of the old list while the old list checker is."""
    job, agents, graph = start(reaudit_turns(audit_project, old_list), slug=AGAIN)

    # the code checker's findings are given to every agent, so only the agents' entries count
    agents_entries = [entry for entry in old_list if not entry.fields["Sources"].startswith("C")]
    assert len(agents_entries) == 2
    for entry in agents_entries:
        assert entry.title not in agents["blind_auditor"].prompts[0]
        assert entry.title in agents["old_list_checker"].prompts[0]
    assert "Events:" in agents["old_list_checker"].prompts[0]
    assert "# Evidence hints" in agents["old_list_checker"].prompts[0]


def test_the_agents_work_in_the_project_with_ratchet_hidden(
    audit_project: Project, old_list: list
) -> None:
    """Proves a re-audit builds its agents, the blind auditor and the old list checker included, to work in
    the project's folder with .ratchet hidden, and reaches its draft with no copy of the code."""
    answers = {"auditor": AuditorAnswer, "blind_auditor": AuditorAnswer, "rules_reviewer": ListReview,
               "standard_reviewer": ListReview, "old_list_checker": OldListCheckerAnswer,
               "duplicate_judge": DuplicateJudgeAnswer, "final_judge": FinalJudgeAnswer}
    turns = reaudit_turns(audit_project, old_list)
    scripted = {name: ScriptedAgent(name, schema, turns.get(name, []))
                for name, schema in answers.items()}
    built = {}

    def record(*arguments) -> dict:
        """Keeps the agents that make_agents builds and returns the scripted ones in their place."""
        built.update(make_agents(*arguments))
        return scripted

    graph = build_audit_graph(audit_project, AuditSettings(), record)
    job = graph.start("", slug=AGAIN)

    assert job.status == JobStatus.WAITING and job.next_step == "publish"
    assert len(scripted["blind_auditor"].prompts) == 1 and len(scripted["old_list_checker"].prompts) == 1
    assert "copy" not in job.data
    assert set(built) == set(answers)
    for name, agent in built.items():
        assert Path(agent.cwd) == audit_project.root, name
        assert ".ratchet/**" in agent.hidden, name


def test_the_old_list_checker_is_asked_again_for_a_missing_id_and_a_missing_citation(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves an id left out and an id with a citation that does not match the code are asked for
    again, in one retry that lists exactly those ids, and that the retry's checks are used."""
    left_out, bogus = old_list[0], old_list[1]
    first = old_list_checker_turn(old_list)
    first["checks"] = [check for check in first["checks"] if check["id"] != left_out.id]
    wrong = next(check for check in first["checks"] if check["id"] == bogus.id)
    wrong["finding"]["evidence"][0]["quote"] = "this text is not in the file"

    turns = reaudit_turns(audit_project, old_list) | {
        "old_list_checker": [first, old_list_checker_turn(old_list, {left_out.title: "reopen"})]}
    job, agents, graph = start(turns, slug=AGAIN)

    retry = agents["old_list_checker"].prompts[1].split("# A retry", 1)[1]
    assert len(agents["old_list_checker"].prompts) == 2
    assert f"{left_out.id}, {bogus.id}" in retry.split(". Answer for")[0]
    folder = audit_folder(audit_project, AGAIN)
    assert (folder / "prompts" / "retry" / "old_list_checker-prompt.md").is_file()
    assert job.status == JobStatus.WAITING
    groups = json.loads((folder / "groups.json").read_text(encoding="utf-8"))["groups"]
    assert {group["old_id"]: group["status"] for group in groups}[left_out.id] == "reopened"


def test_an_old_list_checker_that_stays_wrong_halts_the_audit(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves the audit halts when the retry still leaves an id without a current citation."""
    first = old_list_checker_turn(old_list)
    first["checks"] = first["checks"][1:]
    turns = reaudit_turns(audit_project, old_list) | {"old_list_checker": [first, first]}

    with pytest.raises(AuditError, match=f"{old_list[0].id}.*even when asked again"):
        start(turns, slug=AGAIN)


def test_an_old_list_checker_that_passes_output_to_git_halts_the_reaudit(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves an old list checker that ran git with --output halts the re-audit before its checks are
    kept."""
    old_list_checker = running_bash("old_list_checker", "git log -1 --output=out.txt", old_list_checker_turn(old_list))
    turns = reaudit_turns(audit_project, old_list) | {"old_list_checker": [old_list_checker]}

    with pytest.raises(AuditError, match="--output"):
        start(turns, slug=AGAIN)
    jobs = {job.slug: job for job in halted_jobs(audit_project)}

    assert jobs[AGAIN].status == JobStatus.HALTED and "--output" in jobs[AGAIN].halt_reason
    assert not (audit_folder(audit_project, AGAIN) / "old_list_checker.json").exists()


def test_the_retry_cost_check_counts_what_the_step_already_spent(
    audit_project: Project, old_list: list
) -> None:
    """Proves the cost cap check before the old list checker's retry counts what the blind auditor and the
    old list checker spent earlier in the same step. With a cap of 20 dollars, the blind auditor spends 8
    and the old list checker spends 7, so a retry with a budget of 6 would pass the cap and is not run."""
    answers = {"auditor": AuditorAnswer, "blind_auditor": AuditorAnswer, "rules_reviewer": ListReview,
               "standard_reviewer": ListReview, "old_list_checker": OldListCheckerAnswer,
               "duplicate_judge": DuplicateJudgeAnswer, "final_judge": FinalJudgeAnswer}
    costs = {"blind_auditor": 8.0, "old_list_checker": 7.0}
    first = old_list_checker_turn(old_list)
    first["checks"] = first["checks"][1:]
    turns = reaudit_turns(audit_project, old_list) | {
        "old_list_checker": [first, old_list_checker_turn(old_list)]}
    scripted = {name: ScriptedAgent(name, schema, turns.get(name, []),
                                    cost_usd=costs.get(name, 0.5))
                for name, schema in answers.items()}
    graph = build_audit_graph(audit_project, AuditSettings(max_cost_usd=20.0),
                              lambda *_: scripted)

    with pytest.raises(AuditError, match="cap"):
        graph.start("", slug=AGAIN)
    job = graph.load(AGAIN)

    prompts = audit_folder(audit_project, AGAIN) / "prompts"
    assert len(scripted["old_list_checker"].prompts) == 1
    assert (prompts / "old_list_checker-prompt.md").is_file()
    assert not (prompts / "retry" / "old_list_checker-prompt.md").exists()
    assert job.status == JobStatus.HALTED
    assert job.halt_reason.startswith("step 'audit'") and "$15.00" in job.halt_reason
    assert job.cost_usd == pytest.approx(15.0)


def test_a_reopened_problem_keeps_its_id_with_carried_and_reopened_events(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves a problem the old list checker reopens keeps its id and is listed as reopened and carried
    from the old list, and that its ledger holds a carried and a reopened event."""
    target = entry_titled(old_list, FIRST["title"])
    turns = reaudit_turns(audit_project, old_list) | {
        "old_list_checker": [old_list_checker_turn(old_list, {target.title: "reopen"}, left="still rounds twice")]}
    job, agents, graph = start(turns, slug=AGAIN)
    graph.resume(AGAIN, {"decision": "approve"})

    latest = find_latest_problem_list(audit_project)
    entry = entry_titled(latest.entries, target.title)
    assert entry.id == target.id
    assert entry.fields["Status"] == "reopened"
    assert entry.fields["Carried from"] == f"{OLD_NAME} {target.id}"
    ledger = load_ledger(audit_project.state_dir / latest.name / "ledger.json", latest.name)
    record = ledger.problems[target.id]
    assert [(event.kind, event.by) for event in record.events] == [
        ("carried", "re-audit"), ("reopened", "re-audit")]
    assert record.events[1].note == "still rounds twice"
    assert record.carried_from == f"{OLD_NAME} {target.id}"
    assert records_log(audit_project.root)[0].endswith(f"audit: publish {latest.name}")


def test_a_problem_done_in_the_old_ledger_is_reopened_and_published(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves a problem that refactor marked done, and that the old list checker reopens, passes the
    draft check and publish under its old id, listed as reopened and carried from the old list."""
    target = entry_titled(old_list, FIRST["title"])
    path = audit_project.state_dir / OLD_NAME / "ledger.json"
    ledger = load_ledger(path, OLD_NAME)
    ledger.problems[target.id].status = ProblemStatus.DONE
    ledger.problems[target.id].events.append(
        ProblemEvent(when="2026-01-02 09:00", kind="done", by="J01", job="J01"))
    save_ledger(ledger, path)
    turns = reaudit_turns(audit_project, old_list) | {
        "old_list_checker": [old_list_checker_turn(old_list, {target.title: "reopen"}, left="still rounds twice")]}
    job, agents, graph = start(turns, slug=AGAIN)

    assert job.status == JobStatus.WAITING and job.next_step == "publish"
    job = graph.resume(AGAIN, {"decision": "approve"})

    assert job.status == JobStatus.DONE and job.data["outcome"] == "published"
    latest = find_latest_problem_list(audit_project)
    assert latest.name != OLD_NAME
    entry = entry_titled(latest.entries, target.title)
    assert entry.id == target.id
    assert entry.fields["Status"] == "reopened"
    assert entry.fields["Carried from"] == f"{OLD_NAME} {target.id}"
    record = load_ledger(audit_project.state_dir / latest.name / "ledger.json",
                         latest.name).problems[target.id]
    assert record.status == ProblemStatus.OPEN
    assert [(event.kind, event.by) for event in record.events] == [
        ("carried", "re-audit"), ("reopened", "re-audit")]
    assert records_log(audit_project.root)[0].endswith(f"audit: publish {latest.name}")


def test_a_blocked_problem_comes_back_abandoned_with_its_sendbacks(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves a blocked problem is set aside in the new ledger, with the sendbacks it had."""
    target = entry_titled(old_list, SECOND["title"])
    path = audit_project.state_dir / OLD_NAME / "ledger.json"
    ledger = load_ledger(path, OLD_NAME)
    ledger.problems[target.id].sendbacks = 2
    save_ledger(ledger, path)
    turns = reaudit_turns(audit_project, old_list) | {
        "old_list_checker": [old_list_checker_turn(old_list, {target.title: "blocked"}, left="needs a decision")]}
    job, agents, graph = start(turns, slug=AGAIN)
    graph.resume(AGAIN, {"decision": "approve"})

    latest = find_latest_problem_list(audit_project)
    assert entry_titled(latest.entries, target.title).fields["Status"] == "blocked"
    record = load_ledger(audit_project.state_dir / latest.name / "ledger.json",
                         latest.name).problems[target.id]
    assert record.status == ProblemStatus.ABANDONED and record.sendbacks == 2
    assert [event.kind for event in record.events] == ["carried", "abandoned"]


def test_a_blind_auditor_finding_with_a_reopened_problem_is_split_and_the_old_id_stays(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves a blind auditor finding that holds a reopened problem and another is split by the
    final judge,    and that the old id stays on the part that continues the old problem."""
    target = entry_titled(old_list, FIRST["title"])
    broad = finding("app.cli.main.run", "run does two things wrong", MAIN, 5,
                    "return round(model.total(1, 2), 2)")
    broad["evidence"].append({"file": MODEL, "line": 7, "end_line": 7,
                              "quote": "return round(a + b, 2)", "shows": "the line"})
    extra = finding("app.cli.main", "run has no description", MAIN, 4, "def run():")
    turns = reaudit_turns(audit_project, old_list) | {
        "blind_auditor": [blind_auditor_turn(audit_project, [broad])],
        "old_list_checker": [old_list_checker_turn(old_list, {target.title: "reopen"})],
        "final_judge": [continuing_final_judge({target.id: [FIRST, extra]})],
    }
    job, agents, graph = start(turns, slug=AGAIN)
    graph.resume(AGAIN, {"decision": "approve"})

    entries = find_latest_problem_list(audit_project).entries
    continuing = entry_titled(entries, target.title)
    created = entry_titled(entries, extra["title"])
    assert continuing.id == target.id and continuing.fields["Status"] == "reopened"
    assert re.search(r"\bF\d+\b", continuing.fields["Sources"])
    assert created.id not in {entry.id for entry in old_list}
    assert "Carried from" not in created.fields


def test_the_history_reads_as_one_timeline_across_both_lists(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves `ratchet problems P7 --history` follows a carried problem back into the old list."""
    target = entry_titled(old_list, FIRST["title"])
    turns = reaudit_turns(audit_project, old_list) | {
        "old_list_checker": [old_list_checker_turn(old_list, {target.title: "reopen"}, left="still rounds twice")]}
    start(turns, slug=AGAIN)[2].resume(AGAIN, {"decision": "approve"})
    latest = find_latest_problem_list(audit_project).name

    lines = show_history(audit_project, target.id).splitlines()

    assert lines[0] == f"{target.id}  {target.title}"
    assert [line.split("  ", 1)[1] for line in lines[1:]] == [
        f"opened in {OLD_NAME} ({SLUG})",
        f"carried into {latest} ({AGAIN})",
        'reopened by a re-audit: "still rounds twice"',
    ]


def test_a_blind_audit_that_leaves_the_copy_halts_the_job(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves a contaminated blind audit stops the job instead of being noted."""
    outside = json.dumps(str(audit_project.root / ".ratchet" / "problems.md"))
    message = '{"content": [{"name": "Read", "input": {"file_path": %s}}]}' % outside

    def blind_auditor(prompt: str) -> dict:
        Path("blind_auditor.log").write_text("--- messages ---\n" + message + "\n", encoding="utf-8")
        return blind_auditor_turn(audit_project)

    with pytest.raises(AuditError, match="blind auditor"):
        start(reaudit_turns(audit_project, old_list) | {"blind_auditor": [blind_auditor]}, slug=AGAIN)
    assert not (audit_folder(audit_project, AGAIN) / "blind_auditor.json").exists()


# The text that Claude Code gives as the result of a Read call that its permission rules refuse.
READ_DENIED = ("Permission to use Read has been denied because Claude Code is running in don't "
               "ask mode.")


def calling_tools(name: str, calls: list[tuple[str, dict, str | None]], answer: Turn) -> Turn:
    """Returns a turn that writes a log in which the agent 'name' made each of 'calls', and then
    gives the answer of the turn 'answer'.

    Each call is a tool's name, its input and the text of a refusal. A call with a refusal is
    followed by an error result that holds the text, as Claude Code logs a call that its
    permission rules refused. A call whose refusal is None has no result.
    """
    def turn(prompt: str) -> dict:
        lines = ["--- messages ---"]
        for number, (tool, arguments, refusal) in enumerate(calls):
            use = {"id": f"toolu_{number}", "name": tool, "input": arguments}
            lines.append(json.dumps({"type": "AssistantMessage", "content": [use]}))
            if refusal is not None:
                result = {"tool_use_id": f"toolu_{number}", "content": refusal, "is_error": True}
                lines.append(json.dumps({"type": "UserMessage", "content": [result]}))
        Path(f"{name}.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
        return answer(prompt) if callable(answer) else answer
    return turn


def test_a_search_for_an_api_path_does_not_halt_a_blind_audit(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves a blind audit that searches the history for the text "/api/v1" reaches its draft,
    since the text names no path."""
    blind_auditor = calling_tools(
        "blind_auditor", [("Bash", {"command": 'git log -S "/api/v1" --oneline'}, None)],
        blind_auditor_turn(audit_project))

    job, agents, graph = start(reaudit_turns(audit_project, old_list) | {"blind_auditor": [blind_auditor]},
                               slug=AGAIN)

    assert job.status == JobStatus.WAITING and job.next_step == "publish"
    assert (audit_folder(audit_project, AGAIN) / "blind_auditor.json").is_file()


def test_an_allowed_call_that_names_ratchet_halts_the_blind_audit(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves a blind audit that ran a command naming .ratchet, which the permission rules let
    through, stops the job before its answer is kept."""
    blind_auditor = calling_tools(
        "blind_auditor", [("Bash", {"command": "git show HEAD:.ratchet/problems.md"}, None)],
        blind_auditor_turn(audit_project))

    with pytest.raises(AuditError, match="blind auditor"):
        start(reaudit_turns(audit_project, old_list) | {"blind_auditor": [blind_auditor]}, slug=AGAIN)
    assert not (audit_folder(audit_project, AGAIN) / "blind_auditor.json").exists()


def test_a_denied_call_is_noted_not_counted(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves a blind audit's Read of .ratchet that the permission rules refused does not stop the
    job, and is noted for the owner in the draft."""
    blind_auditor = calling_tools(
        "blind_auditor", [("Read", {"file_path": ".ratchet/problems.md"}, READ_DENIED)],
        blind_auditor_turn(audit_project))

    job, agents, graph = start(reaudit_turns(audit_project, old_list) | {"blind_auditor": [blind_auditor]},
                               slug=AGAIN)

    assert job.status == JobStatus.WAITING and job.next_step == "publish"
    draft = (audit_folder(audit_project, AGAIN) / "draft.md").read_text(encoding="utf-8")
    assert "the permission rules refused" in draft


def short_name(path: Path) -> str:
    """Returns the Windows 8.3 short form of an existing path, such as C:\\X\\RATCHE~1 for
    C:\\X\\.ratchet, or "" when Windows gives none."""
    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    return buffer.value


@pytest.mark.skipif(os.name != "nt", reason="8.3 short names exist only on Windows")
def test_an_allowed_read_of_a_short_name_path_into_ratchet_halts_the_blind_audit(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves a blind audit that read the old list through the 8.3 short name of .ratchet, which
    names no '.ratchet' in its text, stops the job before its answer is kept."""
    short = short_name(audit_project.root / ".ratchet")
    if not short or Path(short).name == ".ratchet":
        pytest.skip("this volume makes no short name for .ratchet")
    blind_auditor = calling_tools(
        "blind_auditor", [("Read", {"file_path": f"{short}\\{OLD_NAME}.md"}, None)],
        blind_auditor_turn(audit_project))

    with pytest.raises(AuditError, match="blind auditor"):
        start(reaudit_turns(audit_project, old_list) | {"blind_auditor": [blind_auditor]}, slug=AGAIN)
    assert not (audit_folder(audit_project, AGAIN) / "blind_auditor.json").exists()


def test_an_allowed_grep_outside_the_project_halts_the_blind_audit(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves a blind audit whose Grep searched the folder above the project, which the rules let
    through, stops the job before its answer is kept."""
    blind_auditor = calling_tools(
        "blind_auditor", [("Grep", {"pattern": "P1", "path": str(audit_project.root.parent)}, None)],
        blind_auditor_turn(audit_project))

    with pytest.raises(AuditError, match="blind auditor"):
        start(reaudit_turns(audit_project, old_list) | {"blind_auditor": [blind_auditor]}, slug=AGAIN)
    assert not (audit_folder(audit_project, AGAIN) / "blind_auditor.json").exists()


# The text that Claude Code gives as the result of a Read of a file in a folder that a Read deny
# rule covers.
FOLDER_DENIED = ("<tool_use_error>File is in a directory that is denied by your permission "
                 "settings.</tool_use_error>")


def test_a_read_of_ratchet_refused_in_the_cli_file_wording_is_noted_not_halted(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves a blind audit's Read of .ratchet that Claude Code refused with its wording for a
    denied folder does not stop the job, and is noted for the owner in the draft."""
    blind_auditor = calling_tools(
        "blind_auditor", [("Read", {"file_path": ".ratchet/problems.md"}, FOLDER_DENIED)],
        blind_auditor_turn(audit_project))

    job, agents, graph = start(reaudit_turns(audit_project, old_list) | {"blind_auditor": [blind_auditor]},
                               slug=AGAIN)

    assert job.status == JobStatus.WAITING and job.next_step == "publish"
    draft = (audit_folder(audit_project, AGAIN) / "draft.md").read_text(encoding="utf-8")
    assert "the permission rules refused" in draft


def test_an_allowed_read_outside_the_project_halts_the_old_list_checker(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves an old list checker that read a file outside the project, such as a session transcript of
    Claude Code, stops the job."""
    outside = audit_project.root.parent / "transcript.jsonl"
    outside.write_text("{}\n", encoding="utf-8")
    old_list_checker = calling_tools(
        "old_list_checker", [("Read", {"file_path": str(outside)}, None)], old_list_checker_turn(old_list))

    with pytest.raises(AuditError, match="old_list_checker"):
        start(reaudit_turns(audit_project, old_list) | {"old_list_checker": [old_list_checker]}, slug=AGAIN)


def test_an_answered_old_question_is_decided_by_the_old_list_checker(
    audit_project: Project, start
) -> None:
    """Proves the owner's answer under an old question reaches the old list checker, and a rule the
    old list checker settles from it is added to rules.md."""
    question = {"kind": "rules", "title": "Is rounding twice deliberate", "evidence": [],
                "why_it_may_be_deliberate": "Money is rounded at each step.",
                "what_would_settle": "A word from the owner."}
    first = default_turns(audit_project) | {
        "auditor": [auditor_turn(audit_project, questions=[question])]}
    job, agents, graph = start(first)
    graph.resume(SLUG)
    graph.resume(SLUG, {"decision": "approve"})
    entries = [entry for entry in find_latest_problem_list(audit_project).entries
               if entry.is_problem]
    age_list(audit_project)
    old_path = audit_project.folder / f"{OLD_NAME}.md"
    old_path.write_text(old_path.read_text(encoding="utf-8").replace(
        "Answer:", "Answer: Round only in pricing.", 1), encoding="utf-8")
    decision = {"id": "Q1", "decision": "rule", "reason": "settled", "finding": SECOND,
                "rule_text": "Amounts are rounded to cents in one place only."}
    turns = reaudit_turns(audit_project, entries) | {
        "old_list_checker": [old_list_checker_turn(entries, answered_questions=[decision])]}
    job, agents, graph = start(turns, slug=AGAIN)

    assert "Answer: Round only in pricing." in agents["old_list_checker"].prompts[0]
    graph.resume(AGAIN, {"decision": "approve"})
    rules = (audit_project.folder / "rules.md").read_text(encoding="utf-8")
    assert f"- Amounts are rounded to cents in one place only. (decided in {AGAIN})\n" in rules
    assert [entry.id for entry in find_latest_problem_list(audit_project).entries
            if not entry.is_problem] == []


def test_findings_grouped_with_a_done_problem_reach_the_final_judge_and_the_done_list_is_published(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves a note finding that cites the place of a problem the old list checker found done is shown
    to the final judge and published as a new problem, and that the done problem is listed under
    Done instead of under Open."""
    target = entry_titled(old_list, FIRST["title"])
    place = old_citations(target)[0]
    noted = finding("app.cli.main.run", "run hides the rounding from the caller", place.file,
                    place.line, place.quote)
    turns = reaudit_turns(audit_project, old_list) | {
        "old_list_checker": [old_list_checker_turn(old_list, {target.title: "done"}, left="nothing is left",
                                   from_reviewer_notes=[noted])]}
    job, agents, graph = start(turns, slug=AGAIN)

    rows = rows_of(agents["final_judge"].prompts[0])
    assert any(row["text"]["title"] == noted["title"] and row["old_id"] == "" for row in rows)
    groups = json.loads((audit_folder(audit_project, AGAIN) / "groups.json")
                        .read_text(encoding="utf-8"))
    split = [group for group in groups["groups"]
             if any(item["source"] == "R" for item in group["items"])]
    assert len(split) == 1 and split[0]["old_id"] == ""
    done = [group for group in groups["done"] if group["old_id"] == target.id]
    assert [[item["source"] for item in group["items"]] for group in done] == [["O"]]

    graph.resume(AGAIN, {"decision": "approve"})

    published = find_latest_problem_list(audit_project)
    created = entry_titled(published.entries, noted["title"])
    assert created.id not in {entry.id for entry in old_list}
    assert created.fields["Status"] == "new"
    assert re.search(r"\bR1\b", created.fields["Sources"])
    assert target.id not in published.ids
    done_section = published.text.split("## Done\n", 1)[1].split("\n## ", 1)[0]
    lines = [line for line in done_section.splitlines()
             if line.startswith(f"- {target.id}. {target.title} (done)")]
    assert len(lines) == 1 and "nothing is left" in lines[0]


# ---------------------------------------------------------------- .ratchet in the git history

# The note that a re-audit gives when .ratchet is in the git history, with {commit} to fill in.
NOT_BLIND = ("The blind audit was skipped. .ratchet was committed to git history in {commit}, so "
             "the old problem lists can still be read there.")


def git_output(root: Path, *arguments: str, env: dict[str, str] | None = None,
               given: str | None = None) -> str:
    """Runs git in the project, with 'given' as its input, checks that it worked, and returns its
    output without the trailing newline."""
    return subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True,
                          text=True, env=env, input=given).stdout.strip()


def commit_ratchet(root: Path, parent: str = "HEAD", name: str = "problems-old.md") -> str:
    """Commits a file .ratchet/<name> on top of the commit 'parent', and returns the new commit's
    hash. The project's work tree, index and branches are left as they were, so no branch holds
    the commit until the caller moves one there."""
    with tempfile.TemporaryDirectory() as scratch:
        # build the commit's tree in an index of its own
        env = os.environ | {"GIT_INDEX_FILE": str(Path(scratch) / "index")}
        git_output(root, "read-tree", parent, env=env)
        blob = git_output(root, "hash-object", "-w", "--stdin",
                          given="# Problems\n\n## P1. an old problem\n")
        git_output(root, "update-index", "--add", "--cacheinfo",
                   f"100644,{blob},.ratchet/{name}", env=env)
        tree = git_output(root, "write-tree", env=env)
        return git_output(root, "commit-tree", tree, "-p", parent, "-m", f"add {name}", env=env)


def short_hash(root: Path, commit: str) -> str:
    """Returns a commit's hash in git's short form."""
    return git_output(root, "rev-parse", "--short", commit)


def leave_in_reflog(root: Path, commit: str) -> None:
    """Moves the current branch to a commit and back again, so that only the reflog reaches the
    commit."""
    branch = git_output(root, "symbolic-ref", "HEAD")
    head = git_output(root, "rev-parse", "HEAD")
    git_output(root, "update-ref", "-m", "move away", branch, commit)
    git_output(root, "update-ref", "-m", "move back", branch, head)


def non_blind_turns(project: Project, entries: list, **changes: object) -> dict[str, list[Turn]]:
    """Returns the turns of a re-audit in which the auditor answers in place of the blind auditor,
    every agent answers and every old problem is open."""
    turns = reaudit_turns(project, entries)
    turns["auditor"] = turns.pop("blind_auditor")
    return turns | changes


def test_a_reaudit_whose_history_holds_ratchet_skips_the_blind_audit(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves a re-audit of a project with .ratchet committed on another branch asks the auditor
    in place of the blind audit, names the oldest such commit in the draft, and publishes a list
    that says the audit was not blind."""
    root = audit_project.root
    first = commit_ratchet(root)
    second = commit_ratchet(root, first, "problems-newer.md")
    git_output(root, "update-ref", "refs/heads/other", second)
    note = NOT_BLIND.format(commit=short_hash(root, first))

    job, agents, graph = start(non_blind_turns(audit_project, old_list), slug=AGAIN)

    assert job.status == JobStatus.WAITING and job.next_step == "publish"
    assert agents["blind_auditor"].prompts == []
    assert len(agents["auditor"].prompts) == 1 and len(agents["old_list_checker"].prompts) == 1
    assert job.data["ratchet_commit"] == short_hash(root, first)
    draft = (audit_folder(audit_project, AGAIN) / "draft.md").read_text(encoding="utf-8")
    assert f"Not blind: {note}" in draft.splitlines()

    job = graph.resume(AGAIN, {"decision": "approve"})

    assert job.data["outcome"] == "published"
    assert f"Not blind: {note}" in find_latest_problem_list(audit_project).text.splitlines()


def test_ratchet_only_in_the_reflog_counts(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves a .ratchet commit that only the reflog reaches still skips the blind audit, and is
    the commit that the draft names."""
    root = audit_project.root
    commit = commit_ratchet(root)
    leave_in_reflog(root, commit)
    note = NOT_BLIND.format(commit=short_hash(root, commit))

    job, agents, graph = start(non_blind_turns(audit_project, old_list), slug=AGAIN)

    assert job.status == JobStatus.WAITING and job.next_step == "publish"
    assert agents["blind_auditor"].prompts == [] and len(agents["auditor"].prompts) == 1
    assert job.data["ratchet_commit"] == short_hash(root, commit)
    draft = (audit_folder(audit_project, AGAIN) / "draft.md").read_text(encoding="utf-8")
    assert f"Not blind: {note}" in draft.splitlines()


def test_a_non_blind_audit_whose_auditor_reads_outside_the_project_halts(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves the auditor that stands in for the blind audit, when .ratchet is in the history, is
    held to the same rule, so its Read of a file outside the project stops the job."""
    root = audit_project.root
    git_output(root, "update-ref", "refs/heads/other", commit_ratchet(root))
    outside = root.parent / "elsewhere.md"
    outside.write_text("x\n", encoding="utf-8")
    auditor = calling_tools(
        "auditor", [("Read", {"file_path": str(outside)}, None)], blind_auditor_turn(audit_project))

    with pytest.raises(AuditError, match="elsewhere.md"):
        start(non_blind_turns(audit_project, old_list, auditor=[auditor]), slug=AGAIN)
    assert not (audit_folder(audit_project, AGAIN) / "blind_auditor.json").exists()


def test_a_first_audit_with_ratchet_in_history_has_no_note(
    audit_project: Project, start
) -> None:
    """Proves a first audit, which has no blind part, runs as usual on a history that holds
    .ratchet and writes no "Not blind" line."""
    root = audit_project.root
    git_output(root, "update-ref", "refs/heads/other", commit_ratchet(root))

    job, agents, graph = start()

    assert job.status == JobStatus.WAITING and job.next_step == "publish"
    assert len(agents["auditor"].prompts) == 1 and agents["blind_auditor"].prompts == []
    assert job.data.get("ratchet_commit", "") == ""
    draft = (audit_folder(audit_project) / "draft.md").read_text(encoding="utf-8")
    assert "Not blind" not in draft


def test_a_draft_step_of_a_job_without_ratchet_commit_still_runs(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves a re-audit saved at the questions gate without ratchet_commit, as a job saved before
    ratchet recorded it would be, still reaches its draft when resumed, with no "Not blind" line."""
    question = {"kind": "rules", "title": "Is rounding twice deliberate",
                "evidence": [], "why_it_may_be_deliberate": "Money is rounded at each step.",
                "what_would_settle": "A word from the owner."}
    turns = reaudit_turns(audit_project, old_list) | {
        "blind_auditor": [auditor_turn(audit_project, problems=[], questions=[question])]}
    job, agents, graph = start(turns, slug=AGAIN)
    assert job.status == JobStatus.WAITING and job.next_step == "check"

    # remove ratchet_commit from the saved job
    saved = graph.state_dir / f"{AGAIN}.json"
    data = json.loads(saved.read_text(encoding="utf-8"))
    del data["data"]["ratchet_commit"]
    saved.write_text(json.dumps(data), encoding="utf-8")

    job = graph.resume(AGAIN, {"answers": {"Q1": "Round only in pricing."}})

    assert job.status == JobStatus.WAITING and job.next_step == "publish"
    draft = (audit_folder(audit_project, AGAIN) / "draft.md").read_text(encoding="utf-8")
    assert "Not blind" not in draft


def test_a_non_blind_audit_with_a_denied_ratchet_read_is_noted_not_halted(
    audit_project: Project, start, old_list: list
) -> None:
    """Proves that when the history holds .ratchet, a Read of .ratchet that the permission rules
    refused the auditor, in place of the blind audit, is noted for the owner and does not halt the
    job."""
    root = audit_project.root
    git_output(root, "update-ref", "refs/heads/other", commit_ratchet(root))
    auditor = calling_tools(
        "auditor", [("Read", {"file_path": ".ratchet/problems.md"}, READ_DENIED)],
        blind_auditor_turn(audit_project))

    job, agents, graph = start(
        non_blind_turns(audit_project, old_list, auditor=[auditor]), slug=AGAIN)

    assert job.status == JobStatus.WAITING and job.next_step == "publish"
    draft = (audit_folder(audit_project, AGAIN) / "draft.md").read_text(encoding="utf-8")
    assert "the permission rules refused" in draft


def test_a_final_judge_answer_for_no_group_is_noted_in_the_verdicts(
    audit_project: Project, start
) -> None:
    """Proves a final judge answer whose key names no group is left out, and that verdicts.md says
    so in a note line."""
    def final_judge(prompt: str) -> dict:
        answer = final_judge_turn()(prompt)
        stray = dict(answer["checks"][0], key="G999")
        return answer | {"checks": [*answer["checks"], stray]}

    start(default_turns(audit_project) | {"final_judge": [final_judge]})

    verdicts = (audit_folder(audit_project) / "verdicts.md").read_text(encoding="utf-8")
    assert "- note: the final judge answered for G999, which is not a group" in verdicts


def ignore_env(project: Project) -> None:
    """Adds .env to the project's .gitignore, commits that, and writes a .env file that holds a
    secret."""
    ignore = project.root / ".gitignore"
    ignore.write_text(ignore.read_text(encoding="utf-8") + ".env\n", encoding="utf-8")
    git(project.root, "add", ".gitignore")
    git(project.root, "commit", "-q", "-m", "ignore .env")
    (project.root / ".env").write_text("KEY=secret\n", encoding="utf-8")


def test_a_hook_refused_read_of_an_ignored_env_is_noted_not_halted(
    audit_project: Project, start
) -> None:
    """Proves an auditor's Read of the ignored .env, which rooster's hook refused and the CLI
    logged as a 'PreToolUse:Read hook error', does not stop the job, and is noted for the owner
    in the draft as a call that would read a hidden path."""
    ignore_env(audit_project)
    refusal = "PreToolUse:Read hook error: the path .env is hidden from this agent, so it is refused"
    auditor = calling_tools("auditor", [("Read", {"file_path": ".env"}, refusal)],
                            auditor_turn(audit_project))

    job, agents, graph = start(default_turns(audit_project) | {"auditor": [auditor]})

    assert job.status == JobStatus.WAITING and job.next_step == "publish"
    draft = (audit_folder(audit_project) / "draft.md").read_text(encoding="utf-8")
    assert "the permission rules refused" in draft
    assert "read a hidden path" in draft


def test_an_allowed_read_of_an_ignored_env_halts_the_audit(audit_project: Project, start) -> None:
    """Proves an auditor's Read of the ignored .env that got through stops the job, with a
    reason that names the auditor and says it may have seen a secret."""
    ignore_env(audit_project)
    auditor = calling_tools("auditor", [("Read", {"file_path": ".env"}, None)],
                            auditor_turn(audit_project))

    with pytest.raises(AuditError) as caught:
        start(default_turns(audit_project) | {"auditor": [auditor]})

    assert "auditor" in str(caught.value)
    assert "a secret" in str(caught.value)
    assert not (audit_folder(audit_project) / "rules_reviewer.json").exists()


def test_a_hook_refused_forbidden_option_is_noted_not_halted(
    audit_project: Project, start
) -> None:
    """Proves an auditor's 'git diff --no-index', which rooster's hook refused and the CLI logged
    as a 'PreToolUse:Bash hook error', does not stop the job, and is noted for the owner."""
    refusal = ("PreToolUse:Bash hook error: the option --no-index is forbidden here, because it "
               "reaches outside the project")
    auditor = calling_tools("auditor", [("Bash", {"command": "git diff --no-index a b"}, refusal)],
                            auditor_turn(audit_project))

    job, agents, graph = start(default_turns(audit_project) | {"auditor": [auditor]})

    assert job.status == JobStatus.WAITING and job.next_step == "publish"
    draft = (audit_folder(audit_project) / "draft.md").read_text(encoding="utf-8")
    assert "the permission rules refused" in draft


# ---------------------------------------------------------------- the owner's questions

FLOAT_ANSWER = "Float arithmetic is totally acceptable"
DECIMAL_ANSWER = "Sure, use Decimal for money"
TOTAL_LINE = "return round(a + b, 2)"


def plain_question(title: str, *places: tuple[str, int, int, str, str]) -> dict:
    """Returns a question for the owner in plain words. Each place is given as (file, first
    line, last line, quote, what the lines show)."""
    return {
        "kind": "design", "title": title,
        "evidence": [{"file": file, "line": line, "end_line": end, "quote": quote, "shows": shows}
                     for file, line, end, quote, shows in places],
        "why_it_may_be_deliberate": "The total adds prices as plain floats, which can lose a cent.",
        "what_would_settle": "Yes: prices move to Decimal and totals stay exact. No: floats "
                             "stay, and small rounding errors are accepted.",
    }


# the same question about money, asked by three agents in their own words
FLOAT_COPIES = (
    plain_question("Is float arithmetic acceptable for money?",
                   (MODEL, 6, 7, "def total(a, b):", "adds two prices as floats")),
    plain_question("Should prices be kept as Decimal?",
                   (MODEL, 7, 7, TOTAL_LINE, "rounds a sum of floats")),
    plain_question("Can the total lose cents?",
                   (MODEL, 7, 7, TOTAL_LINE, "keeps money in floats")),
)


def float_copies_judge_turn(prompt: str) -> dict:
    """Answers a pair of two FLOAT_COPIES as the same question, and every other pair as
    different."""
    titles = {copy["title"] for copy in FLOAT_COPIES}
    return {"pairs": [
        {"pair": row["pair"],
         "same": row["first"]["title"] in titles and row["second"]["title"] in titles,
         "reason": "the same question about money"}
        for row in rows_of(prompt)]}


def three_copies_turns(project: Project, **changes: object) -> dict[str, list[Turn]]:
    """Returns the turns of a first audit in which the auditor, the rules reviewer and the
    standard reviewer each ask one copy of FLOAT_COPIES, and the duplicate judge joins them."""
    auditor, rules, standard = FLOAT_COPIES
    return default_turns(project) | {
        "auditor": [auditor_turn(project, questions=[auditor])],
        "rules_reviewer": [review_turn(added=[ADDED], questions=[rules])],
        "standard_reviewer": [review_turn(questions=[standard])],
        "duplicate_judge": [float_copies_judge_turn],
    } | changes


def verdict(label: str, decision: str, *, conflicts_with: tuple[str, ...] = (),
            rule_text: str = "") -> dict:
    """Returns the final judge's decision on the answered question 'label'."""
    return {"label": label, "decision": decision, "reason": "The answers leave it open.",
            "finding": SECOND, "rule_text": rule_text, "conflicts_with": list(conflicts_with)}


def published_questions(project: Project) -> list:
    """Returns the question entries of the newest list."""
    return [entry for entry in find_latest_problem_list(project).entries
            if entry.section == QUESTIONS_HEADING]


def answer_of(entry) -> str:
    """Returns the text on an entry's Answer line, which is "" when the line is empty."""
    found = re.search(r"^Answer:(.*)$", entry.text, re.MULTILINE)
    assert found, entry.text
    return found.group(1).strip()


def test_three_agents_ask_the_same_question_and_the_owner_is_asked_once(
    audit_project: Project, start
) -> None:
    """Proves the auditor and both reviewers asking the same question in their own words, at
    overlapping lines, gives the owner one question that shows the places of all three copies."""
    job, agents, graph = start(three_copies_turns(audit_project))

    assert job.status == JobStatus.WAITING and job.next_step == "check"
    rows = job.data["questions"]
    assert len(rows) == 1
    text = (audit_folder(audit_project) / "questions.md").read_text(encoding="utf-8")
    assert len(re.findall(r"^### Q", text, re.MULTILINE)) == 1
    for shows in ("adds two prices as floats", "rounds a sum of floats", "keeps money in floats"):
        assert shows in rows[0]["text"], shows


def test_a_different_question_about_the_same_lines_is_asked_separately(
    audit_project: Project, start
) -> None:
    """Proves a question that cites the same lines as the money questions but asks something
    else, such as whether a message should stay, is not merged with them, and the owner is asked
    two questions."""
    message = plain_question("Should the 'Big order!' message stay?",
                             (MODEL, 7, 7, TOTAL_LINE, "prints a message for a big order"))
    turns = three_copies_turns(audit_project)
    turns["standard_reviewer"] = [review_turn(questions=[FLOAT_COPIES[2], message])]

    job, agents, graph = start(turns)

    titles = [row["title"] for row in job.data["questions"]]
    assert len(titles) == 2
    assert "Should the 'Big order!' message stay?" in titles


def test_one_agent_asking_the_same_question_twice_is_asked_once(
    audit_project: Project, start
) -> None:
    """Proves one agent asking the same question twice, in another case, gives the owner one
    question."""
    title = "Is float arithmetic acceptable for money?"
    turns = default_turns(audit_project) | {"auditor": [auditor_turn(
        audit_project, questions=[plain_question(title), plain_question(title.lower())])]}

    job, agents, graph = start(turns)

    assert len(job.data["questions"]) == 1


def test_one_answer_reaches_every_copy_and_the_final_judge(audit_project: Project, start) -> None:
    """Proves the owner's one answer to a merged question is recorded for each agent's copy, and
    that the final judge's prompt shows the answer with the title of every copy."""
    job, agents, graph = start(three_copies_turns(audit_project))

    graph.resume(SLUG, {"answers": {"Q1": DECIMAL_ANSWER}})

    answers = json.loads((audit_folder(audit_project) / "answers.json").read_text(encoding="utf-8"))
    assert {label: answers.get(label) for label in ("L1", "A1", "B1")} == {
        "L1": DECIMAL_ANSWER, "A1": DECIMAL_ANSWER, "B1": DECIMAL_ANSWER}
    prompt = agents["final_judge"].prompts[0]
    assert DECIMAL_ANSWER in prompt
    for copy in FLOAT_COPIES:
        assert copy["title"] in prompt, copy["title"]


@pytest.mark.parametrize("decisions", [[], [verdict("Q1", "parked")]], ids=["none", "parked"])
def test_an_answered_question_never_publishes_with_an_empty_answer_line(
    audit_project: Project, start, decisions: list[dict]
) -> None:
    """Proves an answered question that the final judge leaves open, with no decision or parked
    with a reason, is published with the owner's answer on its Answer line."""
    turns = default_turns(audit_project) | {
        "auditor": [auditor_turn(audit_project, questions=[FLOAT_COPIES[0]])],
        "final_judge": [final_judge_turn(decisions=decisions)],
    }
    job, agents, graph = start(turns)
    graph.resume(SLUG, {"answers": {"Q1": DECIMAL_ANSWER}})
    graph.resume(SLUG, {"decision": "approve"})

    questions = published_questions(audit_project)

    assert len(questions) == 1
    assert answer_of(questions[0]) == DECIMAL_ANSWER


def disagreeing_turns(project: Project, decisions: list[dict]) -> dict[str, list[Turn]]:
    """Returns the turns of a first audit in which the auditor and the rules reviewer ask two
    questions that code cannot join, and the final judge gives the decisions 'decisions'."""
    float_question = plain_question("Is float arithmetic acceptable for money?",
                                    (MODEL, 7, 7, TOTAL_LINE, "adds two prices as floats"))
    decimal_question = plain_question(
        "Should run keep prices as Decimal instead?",
        (MAIN, 5, 5, "return round(model.total(1, 2), 2)", "rounds the total again"))
    return default_turns(project) | {
        "auditor": [auditor_turn(project, questions=[float_question])],
        "rules_reviewer": [review_turn(added=[ADDED], questions=[decimal_question])],
        "final_judge": [final_judge_turn(decisions=decisions)],
    }


def test_two_disagreeing_answers_keep_the_question_open_with_both_answers(
    audit_project: Project, start
) -> None:
    """Proves two answered questions that the final judge parks as in conflict with each other
    are published as one open question that names the other and holds both answers, and that no
    rule is added."""
    decisions = [verdict("Q1", "parked", conflicts_with=("Q2",)),
                 verdict("Q2", "parked", conflicts_with=("Q1",))]
    job, agents, graph = start(disagreeing_turns(audit_project, decisions))
    assert len(job.data["questions"]) == 2
    graph.resume(SLUG, {"answers": {"Q1": FLOAT_ANSWER, "Q2": DECIMAL_ANSWER}})
    graph.resume(SLUG, {"decision": "approve"})

    questions = published_questions(audit_project)

    assert len(questions) == 1
    assert "Should run keep prices as Decimal instead?" in questions[0].text
    answer = answer_of(questions[0])
    assert FLOAT_ANSWER in answer and DECIMAL_ANSWER in answer
    assert "(decided in" not in (audit_project.folder / "rules.md").read_text(encoding="utf-8")


def test_a_disputed_answer_never_becomes_a_rule_even_when_the_other_side_was_settled(
    audit_project: Project, start
) -> None:
    """Proves a question that the final judge settles as a rule is still kept open, with no rule
    and no dropped entry, when another answered question names it as in conflict."""
    decisions = [verdict("Q1", "parked", conflicts_with=("Q2",)),
                 verdict("Q2", "rule", rule_text="Money is kept as Decimal.")]
    job, agents, graph = start(disagreeing_turns(audit_project, decisions))
    graph.resume(SLUG, {"answers": {"Q1": FLOAT_ANSWER, "Q2": DECIMAL_ANSWER}})
    graph.resume(SLUG, {"decision": "approve"})

    published = find_latest_problem_list(audit_project)
    questions = published_questions(audit_project)

    rules = (audit_project.folder / "rules.md").read_text(encoding="utf-8")
    assert "Money is kept as Decimal." not in rules
    titles = {"Is float arithmetic acceptable for money?", "Should run keep prices as Decimal instead?"}
    assert not [entry for entry in published.entries
                if entry.section == "Dropped" and entry.title in titles]
    assert len(questions) == 1
    answer = answer_of(questions[0])
    assert FLOAT_ANSWER in answer and DECIMAL_ANSWER in answer


def test_an_audit_with_no_questions_writes_an_empty_question_file_and_skips_the_gate(
    audit_project: Project, start
) -> None:
    """Proves an audit in which no agent asks a question writes an empty questions.json and runs
    on to the draft without stopping at the questions gate."""
    job, agents, graph = start()

    questions = audit_folder(audit_project) / "questions.json"
    assert questions.is_file()
    assert json.loads(questions.read_text(encoding="utf-8")) == []
    assert job.status == JobStatus.WAITING and job.next_step == "publish"
    assert not job.data.get("questions")


def test_a_parked_old_question_is_asked_again_with_its_answer_on_the_answer_line(
    audit_project: Project, start
) -> None:
    """Proves an answered old question that the old list checker parks is published again with
    the owner's answer on its Answer line, and not repeated in its text."""
    title = "Is float arithmetic acceptable for money?"
    first = default_turns(audit_project) | {
        "auditor": [auditor_turn(audit_project, questions=[plain_question(title)])]}
    job, agents, graph = start(first)
    graph.resume(SLUG)
    graph.resume(SLUG, {"decision": "approve"})
    entries = [entry for entry in find_latest_problem_list(audit_project).entries
               if entry.is_problem]
    age_list(audit_project)
    old_path = audit_project.folder / f"{OLD_NAME}.md"
    old_path.write_text(old_path.read_text(encoding="utf-8").replace(
        "Answer:", "Answer: Round only in pricing.", 1), encoding="utf-8")
    decision = {"id": "Q1", "decision": "parked", "reason": "The answer does not say which.",
                "finding": SECOND, "rule_text": ""}
    turns = reaudit_turns(audit_project, entries) | {
        "old_list_checker": [old_list_checker_turn(entries, answered_questions=[decision])]}
    job, agents, graph = start(turns, slug=AGAIN)
    graph.resume(AGAIN, {"decision": "approve"})

    asked = [entry for entry in published_questions(audit_project) if entry.title == title]

    assert len(asked) == 1
    assert answer_of(asked[0]) == "Round only in pricing."
    assert "The owner answered" not in asked[0].text


# the words of the audit's own workings, which the owner should never have to read
INTERNAL_WORDS = re.compile(r"(?i)\b(grade|stated|inferred|fault|finding|auditor|reviewer|judge)\b")
# a source label such as C1, L1 or B2
SOURCE_LABEL = re.compile(r"\b[CLABFOR]\d+\b")


def test_a_question_reads_plainly_with_no_boilerplate_or_internal_words(
    audit_project: Project, start
) -> None:
    """Proves a question for the owner, in questions.md and in the terminal, opens with its
    context and holds no line naming its kind, no label before the choices, no word of the
    audit's workings and no source label."""
    question = plain_question("Should prices be kept as Decimal instead of float?",
                              (MODEL, 7, 7, TOTAL_LINE, "the total adds two floats"))
    turns = default_turns(audit_project) | {
        "auditor": [auditor_turn(audit_project, questions=[question])]}
    job, agents, graph = start(turns)
    rows = job.data["questions"]
    shown: list[str] = []
    ask_questions([DraftQuestion(row["id"], row["title"], row["text"]) for row in rows],
                  ask=lambda _: "", show=shown.append)

    assert rows[0]["text"].startswith(question["why_it_may_be_deliberate"])
    for text in ((audit_folder(audit_project) / "questions.md").read_text(encoding="utf-8"),
                 "\n".join(shown)):
        assert "This is a question about" not in text
        assert "What would settle it" not in text
        assert not INTERNAL_WORDS.search(text), INTERNAL_WORDS.search(text)
        assert not SOURCE_LABEL.search(text), SOURCE_LABEL.search(text)
