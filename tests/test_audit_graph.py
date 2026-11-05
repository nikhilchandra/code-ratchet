"""Walks the audit graph with scripted agents, on a scratch project that has run init."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from rooster import JobStatus

from ratchet.agent_calls import NoAnswerError
from ratchet.audit.agent_answers import (
    AuditorAnswer,
    CheckAnswer,
    ListReview,
    PairAnswer,
    RecheckAnswer,
)
from ratchet.audit.audit_graph import AuditError, build_audit_graph
from ratchet.audit.code_copy import remove_copy
from ratchet.audit.code_facts import build_inventory
from ratchet.audit.settings import COMMAND, AuditSettings
from ratchet.init_project import init_project
from ratchet.project import Project, load_project
from ratchet.records import commit_records, records_log
from ratchet.refactor.ledger import load_ledger
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


def review_turn(**changes: object) -> dict:
    """Returns a list reviewer's answer that agrees with both entries."""
    reviews = [{"label": label, "verdict": "agree", "reason": "", "finding": entry}
               for label, entry in (("L1", FIRST), ("L2", SECOND))]
    return {"reviews": reviews, "added": [], "questions": [], "bugs": []} | changes


def rows_of(prompt: str) -> list[dict]:
    """Reads the first JSON block of a prompt."""
    return json.loads(prompt.split("```json\n", 1)[1].split("\n```", 1)[0])


def judge_turn(prompt: str) -> dict:
    """Answers every pair as different."""
    return {"pairs": [{"pair": row["pair"], "same": False, "reason": "different"}
                      for row in rows_of(prompt)]}


def checker_turn(seen: list[str] | None = None, decisions: list[dict] | None = None) -> Turn:
    """Returns a checker turn that keeps every group it is given as it reads, and decides the
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
        "list_reviewer_a": [review_turn(added=[ADDED])],
        "list_reviewer_b": [review_turn()],
        "pair_judge": [judge_turn],
        "checker": [checker_turn()],
    }


@pytest.fixture
def start(audit_project: Project):
    """Returns a function that starts an audit with scripted turns and returns the job, the
    agents and the graph."""
    def run(turns: dict[str, list[Turn]] | None = None, settings: AuditSettings | None = None,
            prompt: str = ""):
        answers = {"auditor": AuditorAnswer, "fresh": AuditorAnswer, "list_reviewer_a": ListReview,
                   "list_reviewer_b": ListReview, "rechecker": RecheckAnswer,
                   "pair_judge": PairAnswer, "checker": CheckAnswer}
        given = turns or default_turns(audit_project)
        agents = {name: ScriptedAgent(name, schema, given.get(name, []))
                  for name, schema in answers.items()}
        graph = build_audit_graph(audit_project, settings or AuditSettings(),
                                  lambda *_: agents)
        return graph.start(prompt, slug=SLUG), agents, graph
    return run


def audit_folder(project: Project) -> Path:
    """Returns the folder of the audit that the tests start."""
    return project.state_dir / "audits" / SLUG


def test_a_full_walk_publishes_a_readable_list_and_commits(
    audit_project: Project, start
) -> None:
    """Proves a first audit walks every step, waits at the draft, and on approval writes a list
    whose problems have opened events, and commits the records."""
    seen: list[str] = []
    turns = default_turns(audit_project) | {"checker": [checker_turn(seen)]}
    job, agents, graph = start(turns, prompt="look at rounding")

    assert job.status == JobStatus.WAITING and job.next_step == "publish"
    folder = audit_folder(audit_project)
    for name in ("code-findings.json", "inventory.json", "auditor.json", "review-a.json",
                 "review-b.json", "groups.json", "pairs.json", "citations.md", "check.json",
                 "verdicts.md", "draft.json", "draft.md"):
        assert (folder / name).is_file(), name
    assert not Path(job.data["copy"]).exists()

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
    assert "list_reviewer_a" not in agents["auditor"].prompts[0]


def test_a_stated_code_finding_skips_the_checker_but_is_listed(
    audit_project: Project, start
) -> None:
    """Proves a group that is one stated code finding is not shown to the checker, and still
    comes out as a problem graded stated."""
    seen: list[str] = []
    job, agents, graph = start(default_turns(audit_project) | {"checker": [checker_turn(seen)]})
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


def test_the_pair_judge_is_asked_and_every_pair_is_recorded(
    audit_project: Project, start
) -> None:
    """Proves pairs that the rules cannot settle go to the pair judge and into pairs.json."""
    job, agents, graph = start()

    assert len(agents["pair_judge"].prompts) == 1
    records = json.loads((audit_folder(audit_project) / "pairs.json").read_text(encoding="utf-8"))
    assert records and all(record["same"] is False for record in records)


def test_the_questions_gate_waits_and_the_owners_answers_reach_the_checker(
    audit_project: Project, start
) -> None:
    """Proves a question stops the job at the gate, and that an answer reaches the checker's
    prompt and a rule decision is added to rules.md."""
    question = {"kind": "rules", "title": "Is rounding twice deliberate",
                "evidence": [], "why_it_may_be_deliberate": "Money is rounded at each step.",
                "what_would_settle": "A word from the owner."}
    rule = {"label": "Q1", "decision": "rule", "reason": "settled", "finding": SECOND,
            "rule_text": "Amounts are rounded to cents in one place only."}
    turns = default_turns(audit_project) | {
        "auditor": [auditor_turn(audit_project, questions=[question])],
        "checker": [checker_turn(decisions=[rule])],
    }
    job, agents, graph = start(turns)

    assert job.status == JobStatus.WAITING and job.next_step == "check"
    assert job.data["questions"][0]["id"] == "Q1"
    assert "Is rounding twice deliberate" in (audit_folder(audit_project) / "questions.md").read_text(
        encoding="utf-8")
    assert agents["checker"].prompts == []

    job = graph.resume(SLUG, {"answers": {"Q1": "Round only in pricing.", "Q9": " "}})

    assert "Round only in pricing." in agents["checker"].prompts[0]
    assert job.status == JobStatus.WAITING and job.next_step == "publish"
    graph.resume(SLUG, {"decision": "approve"})
    rules = (audit_project.folder / "rules.md").read_text(encoding="utf-8")
    assert (f"- Amounts are rounded to cents in one place only. (decided in {SLUG})\n"
            in rules)
    assert rules.index("## Decided in audits") < rules.index("Amounts are rounded")
    assert [entry.id for entry in find_latest_problem_list(audit_project).entries
            if not entry.is_problem] == []


def test_an_answered_question_can_become_a_problem(audit_project: Project, start) -> None:
    """Proves a question the checker turns into a problem is listed with a new id."""
    question = {"kind": "design", "title": "Is the tax in the report", "evidence": [],
                "why_it_may_be_deliberate": "x", "what_would_settle": "y"}
    problem = finding("app.core.model", "tax belongs in the model", MODEL, 7, "return round(a + b, 2)")
    decision = {"label": "Q1", "decision": "problem", "reason": "the owner agrees",
                "finding": problem, "rule_text": ""}
    turns = default_turns(audit_project) | {
        "auditor": [auditor_turn(audit_project, questions=[question])],
        "checker": [checker_turn(decisions=[decision])],
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
    """Proves the checker's finding for an answered question has its moved citation fixed and its
    unmatched citation removed."""
    question = {"kind": "design", "title": "Is the tax in the report", "evidence": [],
                "why_it_may_be_deliberate": "x", "what_would_settle": "y"}
    problem = finding("app.core.model", "tax belongs in the model", MODEL, 2, "return round(a + b, 2)")
    problem["evidence"].append({"file": MODEL, "line": 3, "end_line": 3,
                                "quote": "this text is not in the file", "shows": "nothing"})
    decision = {"label": "Q1", "decision": "problem", "reason": "the owner agrees",
                "finding": problem, "rule_text": ""}
    turns = default_turns(audit_project) | {
        "auditor": [auditor_turn(audit_project, questions=[question])],
        "checker": [checker_turn(decisions=[decision])],
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
    turns = default_turns(audit_project) | {"list_reviewer_a": [None]}
    job, agents, graph = start(turns)

    folder = audit_folder(audit_project)
    assert job.status == JobStatus.WAITING
    assert not (folder / "review-a.json").exists() and (folder / "review-b.json").is_file()
    assert "list_reviewer_a gave no answer" in (folder / "draft.md").read_text(encoding="utf-8")
    assert "Owner note" not in graph.resume(SLUG, {"decision": "approve"}).data["list"]
    assert "gave no answer" not in find_latest_problem_list(audit_project).text


def test_a_missing_verdict_and_a_path_outside_the_copy_are_noted(
    audit_project: Project, start
) -> None:
    """Proves the audit tells the owner of an inventory item with no verdict and of a path
    outside the copy that the auditor named."""
    outside = json.dumps(str(audit_project.root / "secret.txt"))
    message = '{"content": [{"name": "Read", "input": {"file_path": %s}}]}' % outside

    def auditor(prompt: str) -> dict:
        Path("auditor.log").write_text("--- messages ---\n" + message + "\n", encoding="utf-8")
        answer = auditor_turn(audit_project)
        answer["units"] = answer["units"][:-1]
        return answer

    job, agents, graph = start(default_turns(audit_project) | {"auditor": [auditor]})

    text = (audit_folder(audit_project) / "draft.md").read_text(encoding="utf-8")
    assert "gave no verdict on 1 items of the inventory" in text
    assert "named paths outside the copy of the code" in text


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
    remove_copy(Path(job.data["copy"]))
    assert job.status == JobStatus.HALTED and "cap" in job.halt_reason
    assert len(agents["auditor"].prompts) == 1 and job.cost_usd == pytest.approx(0.5)


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
        ("check", "draft"),
        ("draft", "publish"),
        ("publish", "finish"),
    }
    assert gates == {"questions", "draft"}


def test_the_copy_is_deleted_even_when_the_checker_fails(audit_project: Project, start) -> None:
    """Proves the copy of the code is removed when the checker gives no answer."""
    agents_turns = default_turns(audit_project) | {"checker": [None]}
    with pytest.raises(NoAnswerError):
        start(agents_turns)
    copies = [job.data["copy"] for job in build_audit_graph(
        audit_project, AuditSettings()).jobs()]

    assert copies and not Path(copies[0]).exists()


def test_the_prompts_and_profiles_are_kept(audit_project: Project, start) -> None:
    """Proves each agent's prompt is saved in the audit's folder."""
    job, agents, graph = start()

    prompts = sorted(path.name for path in (audit_folder(audit_project) / "prompts").iterdir())
    assert prompts == ["auditor-prompt.md", "checker-prompt.md", "list_reviewer_a-prompt.md",
                       "list_reviewer_b-prompt.md", "pair_judge-prompt.md"]
