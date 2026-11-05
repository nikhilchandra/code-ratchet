"""The module contains the functions that build the audit agents' instructions and the prompt of
each of their turns.

An agent's profile is the audit's shared prompt, which holds the project's rules.md and ratchet's
general standard, followed by the prompt of the agent's role. A turn's prompt is built by code
from the data the turn needs, and nothing else reaches the agent.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from enum import StrEnum
from pathlib import Path
from string import Template

from rooster import Agent, Command, Tool

from ratchet.architecture import CodeFinding
from ratchet.audit.agent_answers import (
    AuditorAnswer,
    CheckAnswer,
    EntryReview,
    Finding,
    ListReview,
    PairAnswer,
    Question,
    RecheckAnswer,
    ReviewVerdict,
)
from ratchet.audit.code_facts import InventoryItem
from ratchet.audit.grouping import Group, Item, PairCandidate
from ratchet.audit.settings import AuditSettings
from ratchet.project import Project, RoleSettings
from ratchet.refactor.agent_profiles import STANDARD

PROMPTS = Path(__file__).parent / "prompts"

# the name of the blind audit's agent, which has the auditor's profile
FRESH = "fresh"


class Role(StrEnum):
    """Names the audit's agents, each of which has a prompt of its own."""

    AUDITOR = "auditor"
    LIST_REVIEWER_A = "list_reviewer_a"
    LIST_REVIEWER_B = "list_reviewer_b"
    RECHECKER = "rechecker"
    PAIR_JUDGE = "pair_judge"
    CHECKER = "checker"


def build_profile(project: Project, role: Role) -> str:
    """Returns one agent's instructions, with the project's rules and the standard filled in.

    Args:
        project: The project, as read from its .ratchet folder.
        role: The agent the instructions are for.

    Returns:
        The shared prompt and the role's prompt, joined by a blank line.
    """
    values = {
        "rules": project.rules.strip(),
        "standard": STANDARD.read_text(encoding="utf-8").strip(),
    }
    parts = [PROMPTS / "shared.md", PROMPTS / f"{role}.md"]
    return "\n\n".join(
        Template(path.read_text(encoding="utf-8")).substitute(values).strip() for path in parts
    ) + "\n"


def write_profile(project: Project, role: Role, folder: Path) -> Path:
    """Writes one agent's instructions to a file, which an Agent takes as its profile.

    Args:
        project: The project, as read from its .ratchet folder.
        role: The agent the instructions are for.
        folder: The folder to write the file in, which is made if it does not exist.

    Returns:
        The file, named after the role, such as "auditor.md".
    """
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{role}.md"
    path.write_text(build_profile(project, role), encoding="utf-8")
    return path


def make_agents(project: Project, settings: AuditSettings, copy: Path, folder: Path) -> dict[str, Agent]:
    """Returns the audit's agents, each set up from its settings and working in the copy.

    No agent may edit. All but the pair judge may read the copy's git history. The blind audit's
    agent has the auditor's profile and settings, and is keyed "fresh".

    Args:
        project: The project, as read from its .ratchet folder.
        settings: The audit command's settings.
        copy: The audit's copy of the code, which every agent works in.
        folder: The folder for the agents' profiles and logs.

    Returns:
        The agents, keyed by their role's name, with the blind audit's agent keyed "fresh".
    """
    profiles = folder / "profiles"
    logs = folder / "logs"
    answers = {
        Role.AUDITOR: (settings.auditor, AuditorAnswer),
        Role.LIST_REVIEWER_A: (settings.list_reviewer, ListReview),
        Role.LIST_REVIEWER_B: (settings.list_reviewer, ListReview),
        Role.RECHECKER: (settings.rechecker, RecheckAnswer),
        Role.PAIR_JUDGE: (settings.pair_judge, PairAnswer),
        Role.CHECKER: (settings.checker, CheckAnswer),
    }
    agents = {}
    for role, (role_settings, answer) in answers.items():
        profile = write_profile(project, role, profiles)
        agents[str(role)] = _make_agent(str(role), profile, role, role_settings, answer, copy, logs)
        if role == Role.AUDITOR:
            agents[FRESH] = _make_agent(FRESH, profile, role, role_settings, answer, copy, logs)
    return agents


def _make_agent(
    name: str, profile: Path, role: Role, role_settings: RoleSettings, answer: type, copy: Path, logs: Path
) -> Agent:
    """Returns one audit agent, with the permissions its role has.

    Args:
        name: The agent's name, which also names its log folder.
        profile: The file that holds the agent's instructions.
        role: The agent's role.
        role_settings: The role's model, effort, budget and time limit.
        answer: The shape of the agent's answer.
        copy: The audit's copy of the code.
        logs: The folder that holds a log folder for each agent.

    Returns:
        The agent, which can inspect files and, unless it is the pair judge, read git history.
    """
    permissions = {} if role == Role.PAIR_JUDGE else {"commands": Command.GIT_READ}
    return Agent(
        name,
        profile,
        cwd=copy,
        log_dir=logs / name,
        model=role_settings.model,
        effort=role_settings.effort,
        schema=answer,
        max_work_usd=role_settings.budget_usd,
        timeout_s=role_settings.timeout_s,
        tools=Tool.INSPECT,
        **permissions,
    )


def _json_block(data: object) -> str:
    """Returns data as an indented JSON code block.

    Args:
        data: The data to show, which must be JSON serializable.

    Returns:
        The data as JSON in a fenced code block.
    """
    return "```json\n" + json.dumps(data, indent=2) + "\n```"


def _code_finding_data(found: CodeFinding) -> dict:
    """Returns a code finding as plain data, without its simpler option and done_when.

    Args:
        found: The fault that code found.

    Returns:
        The finding's kind, subject, title, what is wrong, grade, breaks and evidence.
    """
    return {
        "kind": found.kind,
        "subject": found.subject,
        "title": found.title,
        "what_is_wrong": found.what_is_wrong,
        "grade": found.grade,
        "breaks": found.breaks,
        "evidence": [asdict(place) for place in found.evidence],
    }


def _finding_data(finding: Finding) -> dict:
    """Returns an agent's finding as plain data.

    Args:
        finding: The finding an agent gave.

    Returns:
        The finding's fields, as JSON compatible values.
    """
    return finding.model_dump(mode="json")


def _code_findings_section(code_findings: list[CodeFinding]) -> str:
    """Returns the section that lists the faults that code found, or says that there are none.

    Args:
        code_findings: The faults that code found.

    Returns:
        The section as markdown.
    """
    if not code_findings:
        return "# The faults that code found\n\nNone."
    data = [_code_finding_data(found) for found in code_findings]
    return "# The faults that code found\n\nDo not repeat these.\n\n" + _json_block(data)


def format_auditor_prompt(
    request: str, code_findings: list[CodeFinding], inventory: list[InventoryItem]
) -> str:
    """Returns the prompt of the auditor, and of the blind audit, which are the same.

    The prompt holds no list, ledger or earlier answer, so the blind audit sees nothing of the
    audits before it.

    Args:
        request: The owner's request given with -p, or "".
        code_findings: The faults that code found, which the agent must not repeat.
        inventory: Every module and class that the agent must give a verdict on.

    Returns:
        The prompt as markdown.
    """
    parts = []
    if request:
        parts.append("# The owner's request\n\n" + request)
    parts.append(_code_findings_section(code_findings))
    rows = [
        {"name": item.name, "kind": item.kind, "file": item.file, "line": item.line, "size": item.size}
        for item in inventory
    ]
    parts.append("# The inventory\n\nGive a verdict for every item.\n\n" + _json_block(rows))
    return "\n\n".join(parts) + "\n"


def format_review_prompt(entries: dict[str, Finding], code_findings: list[CodeFinding]) -> str:
    """Returns the prompt of a list reviewer.

    Args:
        entries: The auditor's list, as findings keyed by label such as "L1".
        code_findings: The faults that code found.

    Returns:
        The prompt as markdown.
    """
    listed = {label: _finding_data(finding) for label, finding in entries.items()}
    return "\n\n".join([
        "# The list to review\n\nGive a verdict for every label.\n\n" + _json_block(listed),
        _code_findings_section(code_findings),
    ]) + "\n"


def format_recheck_prompt(
    old_list: str,
    histories: dict[str, str],
    records_log: list[str],
    git_log: str,
    hints: str,
    missing: list[str] | None = None,
) -> str:
    """Returns the prompt of the rechecker.

    Args:
        old_list: The text of the old problem list.
        histories: Each old problem's id with its history from the ledger and the reviewers' notes.
        records_log: The records commits since the list was published.
        git_log: The code's git log with --stat since the list's commit.
        hints: The evidence hints, which follow each old citation to where it went.
        missing: The ids that an earlier answer left out or gave without a current citation, for
            the retry. None for the first turn.

    Returns:
        The prompt as markdown. It asks for a check, with current citations and a current subject,
        for every old id.
    """
    ids = ", ".join(histories) or "none"
    parts = [
        "# The old list\n\n" + old_list.strip(),
        "# The history of each old problem\n\n"
        + "\n\n".join(f"## {problem_id}\n\n{text.strip()}" for problem_id, text in histories.items()),
        "# The records log since the list was published\n\n"
        + ("\n".join(f"- {commit}" for commit in records_log) or "None."),
        "# The code's git log since the list was published\n\n```\n" + git_log.strip() + "\n```",
        "# The evidence hints\n\n" + hints.strip(),
        f"# What to answer\n\nGive a check for every old id: {ids}. Each check needs the entry as "
        "it reads now, with current citations and a current subject, the done ones included.",
    ]
    if missing:
        parts.append(
            "# A retry\n\nYour last answer left these ids out, or gave them no current citation, "
            "or named a subject that is not in the code: " + ", ".join(missing) + ". Answer for "
            f"every old id again: {ids}. The ids listed above are the ones to fix, each with "
            "current citations and a current subject."
        )
    return "\n\n".join(parts) + "\n"


def format_pair_prompt(pairs: list[PairCandidate]) -> str:
    """Returns the prompt of the pair judge.

    Args:
        pairs: The numbered pairs of findings to decide.

    Returns:
        The prompt as markdown.
    """
    rows = [
        {
            "pair": pair.pair,
            "first": _pair_member(pair.first.finding),
            "second": _pair_member(pair.second.finding),
        }
        for pair in pairs
    ]
    return "# The pairs\n\nAnswer every pair, by its number.\n\n" + _json_block(rows) + "\n"


def _pair_member(finding: Finding) -> dict:
    """Returns the part of a finding that the pair judge needs, as plain data.

    Args:
        finding: One finding of a pair.

    Returns:
        The finding's title, subject, breaks and the places it cites.
    """
    return {
        "title": finding.title,
        "subject": finding.subject,
        "breaks": finding.breaks,
        "evidence": [f"{place.file}:{place.line}-{place.end_line}" for place in finding.evidence],
    }


def _checked_text(item: Item, finding: Finding, reviews: dict[str, list[EntryReview]]) -> dict:
    """Returns a text for the checker, with the verdicts given to it and without its source.

    Args:
        item: The item that holds the text.
        finding: The text to show.
        reviews: The list reviewers' verdicts, keyed by item label.

    Returns:
        The finding's data, with the list reviewers' verdicts and reasons under "verdicts" and the
        rechecker's verdict under "old_verdict" when the item has them. A verdict of `correct`
        also carries the reviewer's corrected finding under "corrected".
    """
    data = _finding_data(finding)
    given = reviews.get(item.label, [])
    if given:
        data["verdicts"] = []
        for review in given:
            row = {"verdict": str(review.verdict), "reason": review.reason}
            if review.verdict == ReviewVerdict.CORRECT:
                row["corrected"] = _finding_data(review.finding)
            data["verdicts"].append(row)
    if item.old_verdict is not None:
        data["old_verdict"] = str(item.old_verdict)
    return data


def format_check_prompt(
    groups: list[Group],
    citation_report: str,
    answers: list[tuple[str, Question, str]],
    reviews: dict[str, list[EntryReview]] | None = None,
) -> str:
    """Returns the prompt of the checker.

    The groups are given without their sources, so the checker weighs each text and not who wrote
    it. Each text carries the verdicts that the list reviewers gave it, with the corrected
    finding next to each `correct` verdict, and an old entry's text carries the rechecker's
    verdict.

    Args:
        groups: The groups from the deterministic merge.
        citation_report: The report on the citations.
        answers: The owner's answered questions, each as the question's label, the question and
            the owner's answer.
        reviews: The list reviewers' verdicts on each entry, keyed by the label of the item that
            holds the entry. None when no reviewer gave a verdict.

    Returns:
        The prompt as markdown.
    """
    reviews = reviews or {}
    rows = []
    for group in groups:
        main = next(item for item in group.items if item.label == group.text_label)
        others = [item for item in group.items if item.label != group.text_label]
        rows.append({
            "key": group.key,
            "status": group.status,
            "old_id": group.old_id,
            "related": list(group.related),
            "text": _checked_text(main, group.text, reviews),
            "other_texts": [_checked_text(item, item.finding, reviews) for item in others],
        })
    answered = [
        {"label": label, "question": question.model_dump(mode="json"), "answer": answer}
        for label, question, answer in answers
    ]
    return "\n\n".join([
        "# The groups\n\nGive a check for every group.\n\n" + _json_block(rows),
        "# The citation report\n\n" + (citation_report.strip() or "No citation needed a change."),
        "# The owner's answers\n\n" + (_json_block(answered) if answered else "None."),
    ]) + "\n"
