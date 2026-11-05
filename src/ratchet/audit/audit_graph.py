"""Builds the graph that carries one audit, from the checks before it starts to the published
problem list, and the agents that the graph calls.

A job goes through these steps:
    prepare: code checks that the audit may start, runs the code checker, takes the inventory and
        makes the copy of the code that the agents work in.
    audit: the auditor writes the list.
    review: list reviewers A and B each review the list, and may add problems.
    group: code joins the findings that are about the same problem, and the pair judge decides the
        pairs that its rules cannot settle.
    check-citations: code checks every citation against the copy and fixes lines that moved.
    questions: a gate. The questions for the owner are written, and the job waits for the answers.
    check: the checker judges the groups, code applies its verdicts, and the copy is deleted.
    draft: a gate. The ids are given out, the draft is written, and the job waits for approval.
    publish: the list is written with its ledger, the rules decisions are added to rules.md, and
        everything is committed in the records repository.

When the job ends, job.data["outcome"] is "published" or "discarded". Every file the audit makes
goes in .ratchet/state/audits/<job name>/.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import fields, is_dataclass, replace
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel
from rooster import Agent, AgentGraph, Job

from ratchet import architecture
from ratchet.agent_calls import NoAnswerError, ask, ask_together
from ratchet.architecture import CodeFinding, check_architecture
from ratchet.audit.agent_answers import (
    AuditorAnswer,
    Bug,
    CheckAnswer,
    CheckedEntry,
    Citation,
    EntryReview,
    Finding,
    Grade,
    GroupCheck,
    ListReview,
    OldVerdict,
    Outcome,
    Question,
    QuestionDecision,
    QuestionVerdict,
)
from ratchet.audit.agent_profiles import (
    Role,
    format_auditor_prompt,
    format_check_prompt,
    format_pair_prompt,
    format_review_prompt,
    make_agents,
)
from ratchet.audit.citations import CitationResult, check_citations, render_report
from ratchet.audit.code_copy import make_copy, remove_copy
from ratchet.audit.code_facts import InventoryItem, build_inventory
from ratchet.audit.grouping import (
    CODE,
    Group,
    GroupingResult,
    NEW,
    OPEN,
    Item,
    finish_groups,
    group_items,
    join_pairs,
    make_items,
    pair_candidates,
)
from ratchet.audit.list_markdown import (
    Draft,
    DraftDropped,
    DraftProblem,
    DraftQuestion,
    check_draft,
    render_draft,
    strip_owner_notes,
)
from ratchet.audit.settings import AuditSettings
from ratchet.audit.turn_logs import paths_outside, unopened_files
from ratchet.audit.verdicts import AppliedVerdicts, DroppedEntry, FinalEntry, apply_verdicts
from ratchet.git import Repo
from ratchet.project import FOLDER, Project, read_markdown
from ratchet.records import commit_records, has_records
from ratchet.refactor.ledger import (
    Ledger,
    ProblemEvent,
    ProblemRecord,
    ProblemStatus,
    load_ledger,
    save_ledger,
)
from ratchet.refactor.problem_list import find_problem_lists, highest_problem_id, split_entries
from ratchet.target_design import is_template, read_target_design

# The graph's name, which also names the folder under .ratchet/state that holds the audits.
GRAPH_NAME = "audits"

# The heading in rules.md under which the rules that audits settled are added.
DECIDED_HEADING = "## Decided in audits"

# The format of a problem list's time, as in problems-20261102-14h05m.
LIST_TIME = "problems-%Y%m%d-%Hh%Mm"
EVENT_TIME = "%Y-%m-%d %H:%M"

# A function that makes the audit's agents, with the arguments of make_agents().
AgentMaker = Callable[[Project, AuditSettings, Path, Path], dict[str, Agent]]


class AuditError(RuntimeError):
    """Signals an audit that cannot go on, such as rules that are not ready, a dirty working
    tree, a cost cap that is too low, or a draft that cannot be published."""


def _plain(value: object) -> object:
    """Turns a value made of dataclasses, schema instances, tuples and enums into plain data.

    Args:
        value: The value to turn.

    Returns:
        The same value with every dataclass as a dict and every tuple as a list, which JSON can
        hold.
    """
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _plain(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _write_json(path: Path, data: object) -> None:
    """Writes data to a JSON file, making the file's folder if needed.

    Args:
        path: The file.
        data: Plain data, as _plain() returns it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_plain(data), indent=2), encoding="utf-8")


def _read_json(path: Path) -> object:
    """Reads a JSON file that an earlier step wrote.

    Args:
        path: The file.

    Returns:
        The data in the file.
    """
    return json.loads(path.read_text(encoding="utf-8"))


def _load_answer[AnswerT: BaseModel](folder: Path, name: str, model: type[AnswerT]) -> AnswerT | None:
    """Reads an agent's saved answer.

    Args:
        folder: The audit's folder.
        name: The file's name without .json, such as "auditor".
        model: The answer's schema.

    Returns:
        The answer, or None when the agent gave none and no file was written.
    """
    path = folder / f"{name}.json"
    if not path.is_file():
        return None
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def _load_code_findings(folder: Path) -> list[CodeFinding]:
    """Reads the code checker's findings that prepare saved.

    Args:
        folder: The audit's folder.

    Returns:
        The findings.
    """
    found = []
    for row in _read_json(folder / "code-findings.json"):
        evidence = tuple(architecture.Citation(**place) for place in row.pop("evidence"))
        done_when = tuple(row.pop("done_when"))
        found.append(CodeFinding(**row, evidence=evidence, done_when=done_when))
    return found


def _load_inventory(folder: Path) -> list[InventoryItem]:
    """Reads the inventory that prepare saved.

    Args:
        folder: The audit's folder.

    Returns:
        The inventory's items.
    """
    return [InventoryItem(**row) for row in _read_json(folder / "inventory.json")]


def _item_from(data: dict) -> Item:
    """Rebuilds an item from its saved form."""
    verdict = data["old_verdict"]
    return Item(
        label=data["label"], source=data["source"], finding=Finding.model_validate(data["finding"]),
        subject=data["subject"], ranges=tuple(tuple(place) for place in data["ranges"]),
        old_id=data["old_id"], old_verdict=OldVerdict(verdict) if verdict else None,
        modules=tuple(data["modules"]),
    )


def _group_from(data: dict) -> Group:
    """Rebuilds a group from its saved form."""
    return Group(
        key=data["key"], items=tuple(_item_from(item) for item in data["items"]),
        related=tuple(data["related"]), status=data["status"], text_label=data["text_label"],
        text=Finding.model_validate(data["text"]), old_id=data["old_id"], area=data["area"],
        tests=tuple(data["tests"]), notes=tuple(data["notes"]),
    )


def _load_groups(folder: Path) -> GroupingResult:
    """Reads the groups that group and check-citations saved.

    Args:
        folder: The audit's folder.

    Returns:
        The groups for the checker and the groups for the done list.
    """
    data = _read_json(folder / "groups.json")
    return GroupingResult(
        tuple(_group_from(group) for group in data["groups"]),
        tuple(_group_from(group) for group in data["done"]),
    )


def _entry_from(data: dict) -> FinalEntry:
    """Rebuilds a final entry from its saved form."""
    return FinalEntry(
        finding=Finding.model_validate(data["finding"]), old_id=data["old_id"],
        status=data["status"], sources=tuple(data["sources"]), area=data["area"],
        tests=tuple(data["tests"]), notes=tuple(data["notes"]),
    )


def _load_applied(folder: Path) -> tuple[AppliedVerdicts, list[QuestionVerdict]]:
    """Reads the checker's verdicts as check applied them.

    Args:
        folder: The audit's folder.

    Returns:
        The applied verdicts, and the checker's decisions on the owner's answered questions.
    """
    data = _read_json(folder / "final.json")
    applied = AppliedVerdicts(
        problems=tuple(_entry_from(entry) for entry in data["problems"]),
        questions=tuple(_entry_from(entry) for entry in data["questions"]),
        bugs=tuple(_entry_from(entry) for entry in data["bugs"]),
        dropped=tuple(
            DroppedEntry(Finding.model_validate(row["finding"]), row["old_id"],
                         tuple(row["sources"]), row["reason"])
            for row in data["dropped"]
        ),
        done=tuple(_group_from(group) for group in data["done"]),
        notes=tuple(data["notes"]),
    )
    return applied, [QuestionVerdict.model_validate(row) for row in data["decisions"]]


def _gather(folder: Path) -> tuple[list[tuple[str, Question]], list[Bug]]:
    """Gathers the questions and the bugs that the auditor and the list reviewers gave.

    Args:
        folder: The audit's folder.

    Returns:
        The questions, each with its label such as "Q1" counted from 1, and the bugs. A question
        or bug whose title repeats an earlier one, ignoring case, is left out.
    """
    answers = [
        _load_answer(folder, "auditor", AuditorAnswer),
        _load_answer(folder, "review-a", ListReview),
        _load_answer(folder, "review-b", ListReview),
    ]
    questions: list[tuple[str, Question]] = []
    bugs: list[Bug] = []
    asked: set[str] = set()
    noticed: set[str] = set()
    for answer in answers:
        if answer is None:
            continue
        for question in answer.questions:
            if question.title.strip().lower() not in asked:
                asked.add(question.title.strip().lower())
                questions.append((f"Q{len(questions) + 1}", question))
        for bug in answer.bugs:
            if bug.title.strip().lower() not in noticed:
                noticed.add(bug.title.strip().lower())
                bugs.append(bug)
    return questions, bugs


def _places(citations: list[Citation]) -> list[str]:
    """Lists citations as lines such as "- app/views.py:3-4: the copy"."""
    return [f"- {place.file}:{place.line}-{place.end_line}: {place.shows}" for place in citations]


def _question_text(question: Question) -> str:
    """Writes a question for the owner, with its kind, the places it is about, and what would
    settle it.

    Args:
        question: The question an agent asked.

    Returns:
        The question's text as markdown.
    """
    lines = [f"This is a question about the {question.kind}.", ""]
    lines += [question.why_it_may_be_deliberate.strip(), ""]
    lines += [f"What would settle it: {question.what_would_settle.strip()}"]
    if question.evidence:
        lines += ["", *_places(question.evidence)]
    return "\n".join(lines).strip()


def _fix_finding(
    copy: Path, label: str, finding: Finding, results: list[CitationResult], removed: list[str]
) -> Finding:
    """Corrects the lines of a finding's citations and removes the ones that do not match.

    Args:
        copy: The folder the citations' files are relative to.
        label: The label of the item that holds the finding, for the record of removed citations.
        finding: The finding to fix.
        results: The list that every citation's result is added to.
        removed: The list that a description of each removed citation is added to.

    Returns:
        The finding with moved citations at their new lines and unmatched citations left out.
    """
    def fix(citations: list[Citation]) -> list[Citation]:
        kept = []
        for result in check_citations(copy, citations):
            results.append(result)
            if result.fixed is not None:
                kept.append(result.fixed)
            else:
                cited = result.citation
                removed.append(f"{label} {cited.file}:{cited.line}-{cited.end_line} ({result.status})")
        return kept

    return finding.model_copy(
        update={"evidence": fix(finding.evidence), "inferred_from": fix(finding.inferred_from)}
    )


def _fix_group(copy: Path, group: Group, results: list[CitationResult]) -> Group:
    """Corrects the citations of every text in a group.

    Args:
        copy: The folder the citations' files are relative to.
        group: The group.
        results: The list that every citation's result is added to.

    Returns:
        The group with fixed texts, and a note for the owner when citations were removed.
    """
    removed: list[str] = []
    items = tuple(
        replace(item, finding=_fix_finding(copy, item.label, item.finding, results, removed))
        for item in group.items
    )
    text = next(item.finding for item in items if item.label == group.text_label)
    notes = group.notes
    if removed:
        notes += (f"citations that do not match the code were removed in {group.key}: "
                  f"{', '.join(removed)}",)
    return replace(group, items=items, text=text, notes=notes)


def _skips_checker(group: Group) -> bool:
    """Answers whether a group is a single code finding graded stated, which needs no checker."""
    return (len(group.items) == 1 and group.items[0].source == CODE
            and group.text.grade == Grade.STATED)


def _spell(text: str) -> str:
    """Joins the lines of a text into one line."""
    return " ".join(text.split())


def _tests_of(entry: FinalEntry) -> list[tuple[str, str]]:
    """Lists an entry's tests as pairs of the file and how it reaches the code.

    Args:
        entry: The final entry.

    Returns:
        The tests the finding names, then the test files that code found and the finding did not
        name.
    """
    named = [(test.file, test.how) for test in entry.finding.tests]
    known = {file for file, _ in named}
    named += [(file, "imports the cited modules") for file in entry.tests if file not in known]
    return named


def _waits_on(entry: FinalEntry, titles: dict[str, str]) -> list[str]:
    """Turns what an entry waits on into the ids of problems in the draft.

    Args:
        entry: The final entry.
        titles: The ids of the draft's problems by lowercase title, and also by lowercase id.

    Returns:
        The ids that the entry's waits_on names, by id or by title. A name that matches no problem
        of the draft is left out.
    """
    ids = []
    for name in entry.finding.waits_on:
        found = titles.get(name.strip().lower())
        if found and found not in ids:
            ids.append(found)
    return ids


def _add_rules(text: str, rules: list[str], slug: str) -> str:
    """Adds rules to the section of rules.md that holds the rules that audits settled.

    Args:
        text: The contents of rules.md.
        rules: The rules to add, each as the text the checker wrote.
        slug: The audit's name, which each rule names.

    Returns:
        The contents with a line for each rule at the end of the section. The section is added at
        the end when the file has none.
    """
    lines = text.splitlines()
    added = [f"- {_spell(rule)} (decided in {slug})" for rule in rules]
    if DECIDED_HEADING not in [line.strip() for line in lines]:
        lines += ["", DECIDED_HEADING, ""]
    start = [line.strip() for line in lines].index(DECIDED_HEADING)
    end = next((number for number in range(start + 1, len(lines))
                if lines[number].startswith("## ")), len(lines))
    # keep the blank line before the next heading
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    lines[end:end] = added
    return "\n".join(lines) + "\n"


def _ignores_ratchet(repo: Repo) -> bool:
    """Answers whether a project's git ignores the .ratchet folder and does not track it."""
    ignored = repo.run("check-ignore", FOLDER, check=False).strip()
    return bool(ignored) and not repo.run("ls-files", "--", FOLDER).strip()


def build_audit_graph(
    project: Project, settings: AuditSettings, agent_maker: AgentMaker = make_agents
) -> AgentGraph:
    """Returns the graph that carries one audit through its nine steps.

    The job's prompt is the owner's request given with -p, or "". The agents are made when the
    first step that needs them runs, since they work in the copy that the prepare step makes. A
    job that waits at a gate and is resumed in a new process makes them again from the copy's
    path in job.data["copy"].

    Args:
        project: The project, as read from its .ratchet folder.
        settings: The audit command's settings.
        agent_maker: The function that makes the audit's agents.

    Returns:
        The graph, ready to start jobs. A job waits at the questions gate for
        resume(slug, {"answers": {...}}), and at the draft gate for resume(slug, {"decision":
        "approve"}) or {"decision": "discard"}.
    """
    agents: dict[str, Agent] = {}
    graph = AgentGraph(GRAPH_NAME, agents, start="prepare", state_dir=project.state_dir)

    def folder_of(job: Job) -> Path:
        """Returns the folder that holds one audit's files."""
        return graph.state_dir / job.slug

    def agents_of(job: Job) -> dict[str, Agent]:
        """Returns the audit's agents, making them the first time they are asked for."""
        if not agents:
            agents.update(agent_maker(project, settings, Path(job.data["copy"]), folder_of(job)))
        return agents

    def check_cost(job: Job, *budgets: float) -> None:
        """Halts the job when the next agent calls could take it over the audit's cost cap."""
        needed = sum(budgets)
        if job.cost_usd + needed > settings.max_cost_usd:
            raise AuditError(
                f"the next step may spend up to ${needed:.2f}, and the audit has spent "
                f"${job.cost_usd:.2f} of its ${settings.max_cost_usd:.2f} cap"
            )

    def note(job: Job, text: str) -> None:
        """Adds a sentence for the owner that belongs to no entry."""
        job.data["notes"].append(text)

    def check_turn(
        job: Job, name: str, log_path: Path | None, files: list[str] | None = None
    ) -> None:
        """Notes the paths outside the copy that an agent's turn touched, and the files it never
        opened."""
        if log_path is None or not log_path.is_file():
            return
        copy = Path(job.data["copy"])
        outside = paths_outside(log_path, copy)
        if outside:
            note(job, f"the {name} named paths outside the copy of the code: "
                      f"{', '.join(outside[:5])}")
        unopened = unopened_files(log_path, copy, files or [])
        if unopened:
            note(job, f"the {name} never opened {len(unopened)} files of the inventory, such as "
                      f"{', '.join(unopened[:5])}")

    @graph.step("prepare", next=("audit",))
    def prepare(job: Job) -> str:
        if is_template(project.rules):
            raise AuditError("rules.md still has the template marker on its first line. Answer "
                             "the essential questions, then delete that line.")
        if not has_records(project.root):
            raise AuditError("this project has not been set up yet. Run `ratchet init` first.")
        repo = Repo(project.root)
        changed = repo.changed_paths()
        if changed:
            raise AuditError(f"the working tree has changes, so the audit would not match the "
                             f"code: {', '.join(changed[:10])}")
        if not _ignores_ratchet(repo):
            raise AuditError(f"the project's git does not ignore {FOLDER}. Run `ratchet init`.")
        check_cost(job, settings.auditor.budget_usd)

        design = read_target_design(project.rules)
        folder = folder_of(job)
        folder.mkdir(parents=True, exist_ok=True)
        _write_json(folder / "code-findings.json", check_architecture(project, design))
        _write_json(folder / "inventory.json", build_inventory(project, design))
        job.data.update(head=repo.run("rev-parse", "HEAD").strip(), reaudit=False, notes=[])
        job.data["copy"] = str(make_copy(project.root, settings.hide, settings.history_with_ratchet))
        return "audit"

    @graph.step("audit", next=("review", "group"))
    def audit(job: Job) -> str:
        folder = folder_of(job)
        check_cost(job, settings.auditor.budget_usd)
        inventory = _load_inventory(folder)
        prompt = format_auditor_prompt(job.prompt, _load_code_findings(folder), inventory)
        reply = ask(agents_of(job)[Role.AUDITOR], prompt, folder / "prompts")
        answer: AuditorAnswer = reply.data
        _write_json(folder / "auditor.json", answer)
        given = {unit.name for unit in answer.units}
        missing = [item.name for item in inventory if item.name not in given]
        if missing:
            note(job, f"the auditor gave no verdict on {len(missing)} items of the inventory, "
                      f"such as {', '.join(missing[:5])}")
        check_turn(job, "auditor", reply.log_path, sorted({item.file for item in inventory}))
        return "review"

    @graph.step("review", next=("group",))
    def review(job: Job) -> str:
        folder = folder_of(job)
        check_cost(job, 2 * settings.list_reviewer.budget_usd)
        listed = _load_answer(folder, "auditor", AuditorAnswer).problems
        entries = {f"L{number}": finding for number, finding in enumerate(listed, start=1)}
        prompt = format_review_prompt(entries, _load_code_findings(folder))
        chosen = agents_of(job)
        requests = [(chosen[Role.LIST_REVIEWER_A], prompt), (chosen[Role.LIST_REVIEWER_B], prompt)]
        for letter, (agent, _), reply in zip("ab", requests, ask_together(requests, folder / "prompts")):
            if isinstance(reply, NoAnswerError):
                note(job, f"{reply}, so the audit went on without that review")
                continue
            _write_json(folder / f"review-{letter}.json", reply.data)
            given = {verdict.label for verdict in reply.data.reviews}
            missing = [label for label in entries if label not in given]
            if missing:
                note(job, f"the {agent.name} gave no verdict on {', '.join(missing)}")
            check_turn(job, agent.name, reply.log_path)
        return "group"

    @graph.step("group", next=("check-citations",))
    def group(job: Job) -> str:
        folder = folder_of(job)
        design = read_target_design(project.rules)
        reviews = [_load_answer(folder, name, ListReview) for name in ("review-a", "review-b")]
        items = make_items(
            project, design,
            code=_load_code_findings(folder),
            listed=_load_answer(folder, "auditor", AuditorAnswer).problems,
            added_a=reviews[0].added if reviews[0] else [],
            added_b=reviews[1].added if reviews[1] else [],
            old=[], from_notes=[], fresh=[],
        )
        groups = group_items(items)

        # ask the pair judge about the pairs that the rules cannot settle
        candidates = pair_candidates(groups, settings.max_pairs)
        verdicts = []
        if candidates:
            check_cost(job, settings.pair_judge.budget_usd)
            try:
                reply = ask(agents_of(job)[Role.PAIR_JUDGE], format_pair_prompt(candidates),
                            folder / "prompts")
            except NoAnswerError as error:
                note(job, f"{error}, so no pair was joined by its decision")
            else:
                verdicts = reply.data.pairs
                check_turn(job, "pair_judge", reply.log_path)
        groups, records = join_pairs(groups, candidates, verdicts)
        result = finish_groups(project, design, items, groups, job.data["reaudit"])
        _write_json(folder / "pairs.json", records)
        _write_json(folder / "groups.json", {"groups": result.groups, "done": result.done})
        return "check-citations"

    @graph.step("check-citations", next=("questions", "check"))
    def check_citations_step(job: Job) -> str:
        folder = folder_of(job)
        copy = Path(job.data["copy"])
        result = _load_groups(folder)
        results: list[CitationResult] = []
        groups = [_fix_group(copy, group, results) for group in result.groups]
        done = [_fix_group(copy, group, results) for group in result.done]
        (folder / "citations.md").write_text(render_report(results), encoding="utf-8")
        _write_json(folder / "groups.json", {"groups": groups, "done": done})
        return "questions" if _gather(folder)[0] else "check"

    @graph.step("questions", next=("check",), gate=True)
    def questions(job: Job) -> str:
        folder = folder_of(job)
        rows = [{"id": label, "title": question.title, "text": _question_text(question)}
                for label, question in _gather(folder)[0]]
        job.data["questions"] = rows
        lines = ["# Questions for the owner", ""]
        for row in rows:
            lines += [f"### {row['id']}. {row['title']}", "", row["text"], "", "Answer:", ""]
        (folder / "questions.md").write_text("\n".join(lines), encoding="utf-8")
        return "check"

    @graph.step("check", next=("draft",))
    def check(job: Job) -> str:
        folder = folder_of(job)
        copy = Path(job.data["copy"])
        try:
            gathered = _gather(folder)[0]
            answers = {label: text.strip() for label, text in job.data.get("answers", {}).items()
                       if text.strip()}
            if "answers" in job.data:
                _write_json(folder / "answers.json", answers)
            result = _load_groups(folder)
            asked = [group for group in result.groups if not _skips_checker(group)]
            skipped = [group for group in result.groups if _skips_checker(group)]

            # ask the checker about the groups it must judge and the questions that were answered
            answered = [(label, question, answers[label]) for label, question in gathered
                        if label in answers]
            judged = CheckAnswer(checks=[], decisions=[])
            if asked or answered:
                check_cost(job, settings.checker.budget_usd)
                reviews: dict[str, list[EntryReview]] = {}
                for name in ("review-a", "review-b"):
                    given = _load_answer(folder, name, ListReview)
                    for verdict in given.reviews if given else []:
                        reviews.setdefault(verdict.label, []).append(verdict)
                prompt = format_check_prompt(
                    asked, (folder / "citations.md").read_text(encoding="utf-8"), answered, reviews)
                reply = ask(agents_of(job)[Role.CHECKER], prompt, folder / "prompts")
                judged = reply.data
                _write_json(folder / "check.json", judged)
                check_turn(job, "checker", reply.log_path)

            # code keeps each stated code finding as it is, then applies the verdicts
            unjudged = [
                GroupCheck(key=group.key, outcome=Outcome.KEEP, merge_into="",
                           reason="found by code from a stated rule, so the checker was not asked",
                           entries=[CheckedEntry(continues_old=False, finding=group.text)])
                for group in skipped
            ]
            everything = CheckAnswer(checks=unjudged + judged.checks, decisions=judged.decisions)
            applied = apply_verdicts(result, everything)

            # check the citations of the final entries again
            results: list[CitationResult] = []

            def fixed(entries: tuple[FinalEntry, ...]) -> list[FinalEntry]:
                made = []
                for entry in entries:
                    removed: list[str] = []
                    finding = _fix_finding(copy, "/".join(entry.sources), entry.finding, results, removed)
                    extra = (f"citations that do not match the code were removed: {', '.join(removed)}",)
                    made.append(replace(entry, finding=finding,
                                        notes=entry.notes + (extra if removed else ())))
                return made

            # check the citations of the findings that turn questions into problems
            decisions = []
            notes = list(applied.notes)
            for decision in judged.decisions:
                if decision.decision == QuestionDecision.PROBLEM:
                    removed: list[str] = []
                    fixed_finding = _fix_finding(
                        copy, decision.label, decision.finding, results, removed)
                    decision = decision.model_copy(update={"finding": fixed_finding})
                    if removed:
                        notes.append(
                            f"citations that do not match the code were removed: {', '.join(removed)}")
                decisions.append(decision)

            _write_json(folder / "final.json", {
                "problems": fixed(applied.problems),
                "questions": fixed(applied.questions),
                "bugs": fixed(applied.bugs),
                "dropped": applied.dropped,
                "done": applied.done,
                "notes": notes,
                "decisions": decisions,
            })
            lines = ["# What the checker decided", ""]
            for check_ in everything.checks:
                target = f" into {check_.merge_into}" if check_.merge_into else ""
                lines.append(f"- {check_.key}: {check_.outcome}{target}. {check_.reason}")
            for text in applied.notes:
                lines.append(f"- note: {text}")
            for entry in [*applied.problems, *applied.questions, *applied.bugs]:
                lines += [f"- note: {text}" for text in entry.notes]
            (folder / "verdicts.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        finally:
            remove_copy(copy)
        return "draft"

    @graph.step("draft", next=("publish",), gate=True)
    def draft(job: Job) -> str:
        folder = folder_of(job)
        applied, decisions = _load_applied(folder)
        gathered, bugs = _gather(folder)
        answers = {label: text.strip() for label, text in job.data.get("answers", {}).items()
                   if text.strip()}
        decided = {decision.label: decision for decision in decisions}
        made = Draft(notes=list(job.data["notes"]) + list(applied.notes))

        # turn the owner's answered questions into what the checker decided for each
        new_problems: list[tuple[FinalEntry, str]] = []
        rules: list[str] = []
        for label, question in gathered:
            decision = decided.get(label) if label in answers else None
            text = _question_text(question)
            if label in answers:
                text += f"\n\nThe owner answered: {answers[label]}"
            if decision is None or decision.decision == QuestionDecision.PARKED:
                if decision is not None:
                    text += f"\n\nThe audit left it open: {decision.reason.strip()}"
                made.questions.append(DraftQuestion(label, question.title, text))
            elif decision.decision == QuestionDecision.PROBLEM:
                status = NEW if job.data["reaudit"] else OPEN
                entry = FinalEntry(decision.finding, "", status, (label,), "", (), ())
                new_problems.append((entry, label))
            elif decision.decision == QuestionDecision.DROPPED:
                made.dropped.append(DraftDropped(
                    f"D{len(made.dropped) + 1}", question.title, decision.reason))
            elif decision.rule_text.strip():
                rules.append(decision.rule_text)

        # give out the ids, from one above the highest problem id in any list
        number = highest_problem_id(project)
        entries = [*applied.problems, *(entry for entry, _ in new_problems)]
        ids = {}
        for entry in entries:
            if entry.old_id:
                ids[id(entry)] = entry.old_id
            else:
                number += 1
                ids[id(entry)] = f"P{number}"
        names = {}
        for entry in entries:
            names[entry.finding.title.strip().lower()] = ids[id(entry)]
            names[ids[id(entry)].lower()] = ids[id(entry)]
        for entry in entries:
            finding = entry.finding
            made.problems.append(DraftProblem(
                id=ids[id(entry)], title=finding.title, status=entry.status,
                sources=list(entry.sources), grade=str(finding.grade), breaks=finding.breaks,
                subject=finding.subject, what_is_wrong=finding.what_is_wrong,
                after=_waits_on(entry, names), evidence=list(finding.evidence),
                simpler_option=finding.simpler_option, done_when=list(finding.done_when),
                tests=_tests_of(entry), notes=list(entry.notes),
            ))

        # the entries that the checker made questions of follow the questions that were asked
        for number, entry in enumerate(applied.questions, start=len(gathered) + 1):
            made.questions.append(
                DraftQuestion(f"Q{number}", entry.finding.title, entry.finding.what_is_wrong))
        for dropped in applied.dropped:
            made.dropped.append(DraftDropped(
                f"D{len(made.dropped) + 1}", dropped.finding.title, dropped.reason))
        for bug in bugs:
            made.bugs.append(_spell(f"{bug.title}: {bug.what_happens}"))
        for entry in applied.bugs:
            made.bugs.append(_spell(f"{entry.finding.title}: {entry.finding.what_is_wrong}"))

        text = render_draft(made)
        errors = check_draft(text, _done_ids(project))
        if errors:
            raise AuditError("the draft cannot be published: " + "; ".join(errors))
        (folder / "draft.md").write_text(text, encoding="utf-8")
        _write_json(folder / "draft.json", {"draft": made, "rules": rules})
        return "publish"

    @graph.step("publish", next=(None,))
    def publish(job: Job) -> None:
        folder = folder_of(job)
        decision = job.data.get("decision")
        if decision == "discard":
            (folder / "discarded.txt").write_text("The owner discarded this audit.\n", encoding="utf-8")
            job.data["outcome"] = "discarded"
            return None
        if decision != "approve":
            raise AuditError(f"the draft gate needs a decision of approve or discard, not {decision!r}")

        text = (folder / "draft.md").read_text(encoding="utf-8")
        errors = check_draft(text, _done_ids(project))
        if Repo(project.root).run("rev-parse", "HEAD").strip() != job.data["head"]:
            errors.append("HEAD has moved since the audit started, so the list is out of date")
        if errors:
            raise AuditError("the draft cannot be published: " + "; ".join(errors))

        # write the list without the owner notes
        now = datetime.now()
        name = now.strftime(LIST_TIME)
        path = project.folder / f"{name}.md"
        if path.exists():
            raise AuditError(f"{path} exists already, so a list cannot be made in the same minute")
        published = strip_owner_notes(text)
        path.write_text(published, encoding="utf-8", newline="\n")

        # seed the new list's ledger with an opened event for each problem
        ledger = Ledger(problem_list=name)
        for entry in split_entries(published):
            if entry.is_problem:
                carried = entry.fields.get("Carried from", "")
                ledger.problems[entry.id] = ProblemRecord(
                    status=ProblemStatus.OPEN, carried_from=carried,
                    events=[ProblemEvent(when=now.strftime(EVENT_TIME),
                                         kind="carried" if carried else "opened",
                                         by="audit", note=job.slug)],
                )
        save_ledger(ledger, project.state_dir / name / "ledger.json")

        # add the rules that the owner settled to rules.md
        rules = _read_json(folder / "draft.json")["rules"]
        if rules:
            (project.folder / "rules.md").write_text(
                _add_rules(read_markdown(project.folder, "rules.md"), rules, job.slug),
                encoding="utf-8", newline="\n")

        commit = commit_records(project.root, f"audit: publish {name}")
        (folder / "published.txt").write_text(f"{name} {commit}\n", encoding="utf-8")
        job.data.update(outcome="published", list=name)
        return None

    return graph


def _done_ids(project: Project) -> set[str]:
    """Collects the ids of the problems that any list's ledger marks done.

    Args:
        project: The project, as read from its .ratchet folder.

    Returns:
        The ids, which a new list may not reuse.
    """
    done = set()
    for _, path in find_problem_lists(project):
        ledger = load_ledger(project.state_dir / path.stem / "ledger.json", path.stem)
        done |= {pid for pid, record in ledger.problems.items() if record.status == ProblemStatus.DONE}
    return done
