"""The module contains the function that applies the checker's verdicts to the groups of an audit.
Whatever the checker decides, code keeps the old id on the entry that continues an old problem,
never merges two old problems, and falls back to the group as grouped when the checker's answer
breaks a rule.
"""

from __future__ import annotations

from dataclasses import dataclass

from ratchet.audit.agent_answers import CheckAnswer, CheckedEntry, Finding, Grade, GroupCheck, Outcome
from ratchet.audit.grouping import CODE, NEW, Group, GroupingResult

_KEPT = (Outcome.KEEP, Outcome.SPLIT)


@dataclass(frozen=True)
class FinalEntry:
    """One entry that the audit ends with, before it is given an id.

    Attributes:
        finding: The entry's final text.
        old_id: The id of the old problem that the entry continues, or "" for a new entry.
        status: "new", "open", "reopened" or "blocked".
        sources: The labels of the items that were grouped into the entry.
        area: The name of the part that holds the entry's subject.
        tests: The test files that reach the entry's code.
        notes: Sentences for the owner about how the entry came about.
    """

    finding: Finding
    old_id: str
    status: str
    sources: tuple[str, ...]
    area: str
    tests: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class DroppedEntry:
    """One group that the checker dropped.

    Attributes:
        finding: The group's main text.
        old_id: The id of the old problem in the group, or "".
        sources: The labels of the items in the group.
        reason: The checker's reason.
    """

    finding: Finding
    old_id: str
    sources: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class AppliedVerdicts:
    """What the groups come out as once the checker's verdicts are applied.

    Attributes:
        problems: The entries that go in the Open section.
        questions: The entries that go in the questions for the owner.
        bugs: The entries that go in the bugs noticed.
        dropped: The groups that were dropped.
        done: The groups that hold only the old entry of a problem that is done or gone, which the
            checker did not see.
        notes: Notes for the owner that belong to no entry.
    """

    problems: tuple[FinalEntry, ...]
    questions: tuple[FinalEntry, ...]
    bugs: tuple[FinalEntry, ...]
    dropped: tuple[DroppedEntry, ...]
    done: tuple[Group, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class _Plan:
    """What one group comes out as, before any merge into it.

    Attributes:
        outcome: The checker's outcome for the group.
        merge_into: The id of the group that this group is merged into, or "" if it is not merged.
        reason: The checker's stated reason for the outcome.
        entries: The checked entries that the group keeps.
        notes: The notes that the group adds to the audit.
    """

    outcome: Outcome
    merge_into: str
    reason: str
    entries: tuple[CheckedEntry, ...]
    notes: tuple[str, ...]


def apply_verdicts(grouping: GroupingResult, answer: CheckAnswer) -> AppliedVerdicts:
    """Applies the checker's verdict on each group.

    A group whose verdict breaks a rule is kept as grouped, with its main text and a note for the
    owner. The rules are that a kept or split group holds exactly one entry that continues its old
    problem when it has one and none otherwise, that a merge names a related group that is itself
    kept or split, and that a merge never joins two old problems.

    Args:
        grouping: The groups that went to the checker, as made by finish_groups.
        answer: The checker's answer.

    Returns:
        The final entries, with the old id on the entry that continues each old problem. A code
        finding graded stated outlives a dropped group as an entry of its own.
    """
    groups = {group.key: group for group in grouping.groups}
    notes = []
    checks: dict[str, GroupCheck] = {}
    for check in answer.checks:
        if check.key not in groups:
            notes.append(f"the checker answered for {check.key}, which is not a group")
        elif check.key in checks:
            notes.append(f"the checker answered twice for {check.key}, and the first answer was used")
        else:
            checks[check.key] = check
    reverted = _refused_merges(groups, checks)
    plans = {key: _plan(group, checks.get(key), key in reverted) for key, group in groups.items()}
    plans = _check_merges(groups, plans)
    problems, questions, bugs, dropped = [], [], [], []
    for key, group in groups.items():
        plan = plans[key]
        if plan.outcome == Outcome.MERGE_INTO:
            continue
        entries = _collect(groups, plans, key)
        if plan.outcome in _KEPT:
            problems.extend(entries)
        elif plan.outcome == Outcome.QUESTION:
            questions.extend(entries)
        elif plan.outcome == Outcome.BUG:
            bugs.extend(entries)
        else:
            dropped.append(DroppedEntry(group.text, group.old_id, _labels(group), plan.reason))
            problems.extend(_stated_code_entries(group))
    return AppliedVerdicts(
        tuple(problems), tuple(questions), tuple(bugs), tuple(dropped), grouping.done, tuple(notes)
    )


def _labels(group: Group) -> tuple[str, ...]:
    """Lists the labels of a group's items.

    Args:
        group: The group.

    Returns:
        The labels, in the order of the items.
    """
    return tuple(item.label for item in group.items)


def _refused_merges(groups: dict[str, Group], checks: dict[str, GroupCheck]) -> set[str]:
    """Finds the groups whose merges would leave two or more old problems in one group. That
    happens when an old group merges into another old group, and also when several old groups
    merge into the same new group.

    Args:
        groups: The groups by key.
        checks: The checker's answer for each group by key.

    Returns:
        The keys of every group merging into such a target, and of the target itself.
    """
    # gather the groups merging into each target
    merging: dict[str, list[str]] = {}
    for key, check in checks.items():
        if check.outcome == Outcome.MERGE_INTO and check.merge_into in groups:
            merging.setdefault(check.merge_into, []).append(key)

    # refuse every merge into a target that would end up with two or more old ids
    refused = set()
    for target, keys in merging.items():
        old_ids = [groups[key].old_id for key in (target, *keys) if groups[key].old_id]
        if len(old_ids) >= 2:
            refused |= {target, *keys}
    return refused


def _fallback(group: Group, reason: str) -> _Plan:
    """Makes the plan that keeps a group as grouped.

    Args:
        group: The group.
        reason: Why the checker's answer was not used, as a phrase that starts with "because".

    Returns:
        A keep plan with one entry holding the group's main text, which continues the group's old
        problem when it has one, and a note for the owner.
    """
    entry = CheckedEntry(continues_old=bool(group.old_id), finding=group.text)
    note = f"the checker's answer for {group.key} was not used, {reason}"
    return _Plan(Outcome.KEEP, "", reason, (entry,), (note,))


def _plan(group: Group, check: GroupCheck | None, reverted: bool) -> _Plan:
    """Makes the plan for one group from the checker's answer, or the fallback when the answer
    breaks a rule.

    Args:
        group: The group.
        check: The checker's answer for the group, or None.
        reverted: True when the group is part of a refused merge of two old problems.

    Returns:
        The plan.
    """
    if reverted:
        return _fallback(group, "because it would have joined two old problems")
    if check is None:
        return _fallback(group, "because the checker gave no answer")
    wrong = _broken_rule(group, check)
    if wrong:
        return _fallback(group, wrong)
    return _Plan(check.outcome, check.merge_into, check.reason, tuple(check.entries), ())


def _broken_rule(group: Group, check: GroupCheck) -> str:
    """Tells which rule, if any, a checker's answer breaks, apart from the choice of merge target.

    Args:
        group: The group.
        check: The checker's answer for the group.

    Returns:
        A short phrase that names the broken rule, or "" when the answer is sound.
    """
    outcome = check.outcome
    if outcome == Outcome.DROP:
        return ""
    if outcome in (Outcome.QUESTION, Outcome.BUG) and group.old_id:
        return f"because it would end {group.old_id} as something other than a problem"
    if not check.entries:
        return "because it gave no entry"
    if outcome in (Outcome.QUESTION, Outcome.BUG):
        return ""
    if outcome == Outcome.KEEP and len(check.entries) != 1:
        return "because a kept group must come out as one entry"
    if outcome == Outcome.SPLIT and len(check.entries) < 2:
        return "because a split group must come out as two or more entries"
    if outcome == Outcome.MERGE_INTO and not check.merge_into:
        return "because it names no group to merge into"
    continuing = sum(entry.continues_old for entry in check.entries)
    if group.old_id and continuing != 1:
        return f"because {continuing} entries continue {group.old_id} and exactly one must"
    if not group.old_id and continuing:
        return "because an entry continues an old problem that the group does not hold"
    return ""


def _check_merges(groups: dict[str, Group], plans: dict[str, _Plan]) -> dict[str, _Plan]:
    """Replaces the plan of each group whose merge target is not acceptable.

    Args:
        groups: The groups by key.
        plans: The plan for each group by key.

    Returns:
        The plans, with a fallback in place of each merge into a group that is not related or is
        not kept or split.
    """
    checked = dict(plans)
    for key, plan in plans.items():
        if plan.outcome != Outcome.MERGE_INTO:
            continue
        target = plan.merge_into
        if target not in groups[key].related:
            checked[key] = _fallback(groups[key], f"because {target} is not a related group")
        elif plans[target].outcome not in _KEPT:
            checked[key] = _fallback(groups[key], f"because {target} is not kept or split")
    return checked


def _collect(groups: dict[str, Group], plans: dict[str, _Plan], key: str) -> list[FinalEntry]:
    """Builds the final entries of a group, with the entries of the groups merged into it.

    Args:
        groups: The groups by key.
        plans: The plan for each group by key.
        key: The key of the group.

    Returns:
        The entries, with the old id on each entry that continues an old problem.
    """
    sources = [key] + [
        other for other, plan in plans.items()
        if plan.outcome == Outcome.MERGE_INTO and plan.merge_into == key
    ]
    entries = []
    for source in sources:
        group, plan = groups[source], plans[source]
        group_notes = tuple(group.notes) + plan.notes
        for number, checked in enumerate(plan.entries):
            continuing = checked.continues_old and bool(group.old_id)
            entries.append(FinalEntry(
                finding=checked.finding,
                old_id=group.old_id if continuing else "",
                status=group.status if continuing or not group.old_id else NEW,
                sources=_labels(group),
                area=group.area,
                tests=group.tests,
                notes=group_notes if number == 0 else (),
            ))
    return entries


def _stated_code_entries(group: Group) -> list[FinalEntry]:
    """Makes an entry of its own for each code finding graded stated in a dropped group.

    Args:
        group: The dropped group.

    Returns:
        The entries, each with a note that says the checker dropped the group.
    """
    status = NEW if group.old_id else group.status
    note = f"kept although the checker dropped {group.key}, because code found it from a stated rule"
    return [
        FinalEntry(item.finding, "", status, (item.label,), group.area, group.tests, (note,))
        for item in group.items
        if item.source == CODE and item.finding.grade == Grade.STATED
    ]
