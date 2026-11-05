"""The module contains the deterministic merge of an audit, which labels every finding from every
source, places each one in the code, joins the findings that are about the same problem into groups,
and gives each group its text, status, area and tests. The only judgment it asks for is the
duplicate judge's, on the few pairs that its rules cannot settle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from ratchet import architecture
from ratchet.architecture import CodeFinding, Module, find_modules
from ratchet.audit.agent_answers import Citation, Finding, Grade, OldEntryCheck, OldVerdict, PairVerdict
from ratchet.audit.code_facts import area_of, enclosing_name, tests_reaching
from ratchet.project import Project
from ratchet.target_design import TargetDesign

# the source letters, which are also the first letters of the labels
CODE = "C"
LISTED = "L"
ADDED_A = "A"  # the findings that the rules reviewer added
ADDED_B = "B"  # the findings that the standard reviewer added
OLD = "O"
NOTES = "R"
BLIND_AUDITOR = "F"

# the order in which a source's text is chosen for a group
TEXT_ORDER = (OLD, NOTES, LISTED, ADDED_A, ADDED_B, BLIND_AUDITOR, CODE)

# the statuses a group can have
NEW = "new"
OPEN = "open"
REOPENED = "reopened"
BLOCKED = "blocked"
DONE = "done"

_OLD_STATUS = {
    OldVerdict.REOPEN: REOPENED,
    OldVerdict.OPEN: OPEN,
    OldVerdict.CORRECTED: OPEN,
    OldVerdict.BLOCKED: BLOCKED,
    OldVerdict.DONE: DONE,
    OldVerdict.GONE: DONE,
}

_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]{3,}")


@dataclass(frozen=True)
class Item:
    """One finding from one source, placed in the code.

    Attributes:
        label: The item's label, such as "C1", "L3", "A2", "B1", "O P7", "R1" or "F4".
        source: The source letter, which is the first letter of the label.
        finding: The finding as the source gave it. For an old entry it is the entry as the
            old list checker reads it now.
        subject: The module, class or function that the finding is about.
        ranges: The cited places as (file, first line, last line), taken from the finding's
            evidence. For an old entry these are the old list checker's current citations.
        old_id: The old problem's id such as "P7" for an old entry, and "" otherwise.
        old_verdict: The old list checker's verdict for an old entry, and None otherwise.
        modules: The names of the project modules whose files the finding cites.
    """

    label: str
    source: str
    finding: Finding
    subject: str
    ranges: tuple[tuple[str, int, int], ...]
    old_id: str
    old_verdict: OldVerdict | None
    modules: tuple[str, ...]


@dataclass(frozen=True)
class Group:
    """A set of findings that are about the same problem.

    Attributes:
        key: The group's key, such as "G3".
        items: The findings in the group, at most one from each source.
        related: The keys of the groups that share cited lines with this group but could not be
            joined to it.
        status: "open" or "new" for a group in a first audit or a re-audit, "reopened", "blocked",
            or "done" for a group whose old entry is finished.
        text_label: The label of the item whose text is the group's main text.
        text: The group's main text.
        old_id: The id of the old problem in the group, or "" when it holds none.
        area: The name of the part that holds the text's subject.
        tests: The test files that import the modules the group cites.
        notes: Sentences for the owner about how the group came about.
    """

    key: str
    items: tuple[Item, ...]
    related: tuple[str, ...]
    status: str
    text_label: str
    text: Finding
    old_id: str
    area: str
    tests: tuple[str, ...]
    notes: tuple[str, ...]


@dataclass(frozen=True)
class PairCandidate:
    """Two ungrouped findings that might be about the same problem.

    Attributes:
        pair: The number the duplicate judge answers with, counted from 1.
        first: The first finding.
        second: The second finding.
        score: How likely the pair is to be the same, where larger is likelier.
    """

    pair: int
    first: Item
    second: Item
    score: float


@dataclass(frozen=True)
class PairRecord:
    """What became of one candidate pair, as written to pairs.json.

    Attributes:
        pair: The candidate's number.
        first: The label of the first finding.
        second: The label of the second finding.
        same: The duplicate judge's answer, or None when the duplicate judge gave none.
        reason: The duplicate judge's reason, or the reason that code refused the join.
        joined: True when the two findings ended up in one group.
    """

    pair: int
    first: str
    second: str
    same: bool | None
    reason: str
    joined: bool


@dataclass(frozen=True)
class GroupingResult:
    """The groups that the merge ends with.

    Attributes:
        groups: The groups that go on to the final judge.
        done: The groups that hold only an old entry that is done or gone, which go to the done
            list.
    """

    groups: tuple[Group, ...]
    done: tuple[Group, ...]


def make_items(
    project: Project,
    design: TargetDesign | None,
    *,
    code: list[CodeFinding],
    listed: list[Finding],
    added_a: list[Finding],
    added_b: list[Finding],
    old: list[OldEntryCheck],
    from_notes: list[Finding],
    blind_auditor: list[Finding],
) -> list[Item]:
    """Labels every finding from every source and places it in the code.

    Args:
        project: The project, as read from its .ratchet folder.
        design: The target design, or None when rules.md has no TOML block.
        code: The code checker's findings.
        listed: The auditor's list.
        added_a: The findings that the rules reviewer added.
        added_b: The findings that the standard reviewer added.
        old: The old list checker's check of each old problem. The finding of each is its current text.
        from_notes: The findings that the old list checker took from reviewer notes.
        blind_auditor: The findings of the blind auditor.

    Returns:
        The items, source by source in the order of the arguments, each labeled in order from 1.
        An old entry is labeled "O" and its id.
    """
    modules = {module.path: module for module in find_modules(project, design)}
    items = []
    for source, findings in (
        (CODE, [_from_code(finding) for finding in code]),
        (LISTED, listed),
        (ADDED_A, added_a),
        (ADDED_B, added_b),
        (NOTES, from_notes),
        (BLIND_AUDITOR, blind_auditor),
    ):
        for number, finding in enumerate(findings, start=1):
            items.append(_make_item(project, modules, f"{source}{number}", source, finding, "", None))
    for check in old:
        label = f"{OLD} {check.id}"
        items.append(_make_item(project, modules, label, OLD, check.finding, check.id, check.verdict))
    return items


def group_items(items: list[Item]) -> list[list[Item]]:
    """Joins the items that are certainly about the same problem.

    Items are joined in three passes. The first joins items with the same subject and overlapping
    ranges, the second joins overlapping ranges in the same file, and the third joins the same
    subject. A group never holds two items from the same source.

    Args:
        items: The items to group, as made by make_items.

    Returns:
        The groups, each in the order of the items, ordered by the first item of each.
    """
    members = {item.label: [item] for item in items}
    owner = {item.label: item.label for item in items}
    tests = (
        lambda a, b: bool(a.subject) and a.subject == b.subject and _overlap(a, b),
        _overlap,
        lambda a, b: bool(a.subject) and a.subject == b.subject,
    )
    for test in tests:
        for index, first in enumerate(items):
            for second in items[index + 1 :]:
                if test(first, second):
                    _join(members, owner, first.label, second.label)
    return _in_order(items, members)


def pair_candidates(groups: list[list[Item]], max_pairs: int) -> list[PairCandidate]:
    """Picks the pairs of ungrouped items that the duplicate judge should decide.

    Two items are a candidate when they come from different sources, neither has been joined to
    another item, and they share a file, share a parent class or module, or have titles that share
    most of their identifiers.

    Args:
        groups: The groups, as made by group_items.
        max_pairs: The most pairs to return.

    Returns:
        The candidates, likeliest first, numbered from 1.
    """
    singles = [group[0] for group in groups if len(group) == 1]
    scored = []
    for index, first in enumerate(singles):
        for second in singles[index + 1 :]:
            if first.source == second.source:
                continue
            score = _closeness(first, second)
            if score > 0:
                scored.append((score, first, second))
    scored.sort(key=lambda entry: -entry[0])
    return [
        PairCandidate(number, first, second, score)
        for number, (score, first, second) in enumerate(scored[:max_pairs], start=1)
    ]


def join_pairs(
    groups: list[list[Item]], candidates: list[PairCandidate], verdicts: list[PairVerdict]
) -> tuple[list[list[Item]], list[PairRecord]]:
    """Joins the pairs that the duplicate judge called the same.

    A join that would put two items of one source in a group is refused.

    Args:
        groups: The groups, as made by group_items.
        candidates: The pairs that were put to the duplicate judge.
        verdicts: The duplicate judge's decisions, matched to the candidates by their numbers.

    Returns:
        The groups after the joins, and one record for each candidate.
    """
    items = [item for group in groups for item in group]
    members = {group[0].label: list(group) for group in groups}
    owner = {item.label: group[0].label for group in groups for item in group}
    answers = {verdict.pair: verdict for verdict in verdicts}
    records = []
    for candidate in candidates:
        verdict = answers.get(candidate.pair)
        first, second = candidate.first.label, candidate.second.label
        if verdict is None:
            records.append(PairRecord(candidate.pair, first, second, None, "no answer", False))
            continue
        joined = verdict.same and _join(members, owner, first, second)
        reason = verdict.reason
        if verdict.same and not joined:
            reason = f"refused, a group would hold two items of one source. {reason}"
        records.append(PairRecord(candidate.pair, first, second, verdict.same, reason, joined))
    return _in_order(items, members), records


def finish_groups(
    project: Project,
    design: TargetDesign | None,
    items: list[Item],
    groups: list[list[Item]],
    reaudit: bool,
) -> GroupingResult:
    """Gives each group its key, main text, status, area, tests and related groups.

    Args:
        project: The project, as read from its .ratchet folder.
        design: The target design, or None when rules.md has no TOML block.
        items: Every item that went into the merge.
        groups: The groups, after the duplicate judge's joins.
        reaudit: True for a re-audit, where a group with no old entry is new.

    Returns:
        The groups for the final judge and the groups for the done list. The other items of a group
        whose old entry is done or gone come back as a new group for the final judge, with a note that
        names the old id, unless the blind auditor's item reopens the group.

    Raises:
        ValueError: If a label is used twice, or if the groups do not hold every item exactly once.
    """
    labels = [item.label for item in items]
    grouped = sorted(item.label for group in groups for item in group)
    if len(set(labels)) != len(labels) or sorted(labels) != grouped:
        raise ValueError("the groups must hold every labeled item exactly once")
    built = []
    reach: dict[frozenset[str], tuple[str, ...]] = {}
    for number, (members, split_from) in enumerate(_split_done(groups), start=1):
        names = frozenset(name for item in members for name in item.modules)
        if names not in reach:
            reach[names] = tuple(tests_reaching(project, set(names)))
        group = _make_group(f"G{number}", members, reaudit, design, reach[names])
        if split_from:
            group = replace(group, notes=(*group.notes, split_from))
        built.append(group)
    live = [group for group in built if group.status != DONE]
    related = _related(live)
    live = [_with_related(group, related.get(group.key, ())) for group in live]
    done = [group for group in built if group.status == DONE]
    return GroupingResult(tuple(live), tuple(done))


def _split_done(groups: list[list[Item]]) -> list[tuple[list[Item], str]]:
    """Takes the other items out of each group whose old entry is done or gone.

    Args:
        groups: The groups, after the duplicate judge's joins.

    Returns:
        The groups in order, each with a note for the owner, which is empty except on a group that
        was split off. A done or gone group with no blind auditor's item is replaced by the old item
        alone, followed by the rest of its items as a group of their own. A group that holds a blind
        auditor's item stays whole, since it is reopened.
    """
    split: list[tuple[list[Item], str]] = []
    for members in groups:
        old = next((item for item in members if item.source == OLD), None)
        finished = old is not None and _OLD_STATUS[old.old_verdict] == DONE
        if finished and len(members) > 1 and all(item.source != BLIND_AUDITOR for item in members):
            split.append(([old], ""))
            rest = [item for item in members if item is not old]
            split.append((rest, f"{old.old_id} was found {old.old_verdict.value}, so the findings "
                                "grouped with it were split off"))
        else:
            split.append((members, ""))
    return split


def _from_code(found: CodeFinding) -> Finding:
    """Writes a code finding in the fields of an agent's finding.

    Args:
        found: The code checker's finding.

    Returns:
        The same finding, with the fields that the code checker cannot fill left empty.
    """
    return Finding(
        subject=found.subject,
        title=found.title,
        what_is_wrong=found.what_is_wrong,
        grade=Grade(found.grade),
        breaks=found.breaks,
        inferred_from=[],
        history="",
        evidence=[_citation(place) for place in found.evidence],
        simpler_option=found.simpler_option,
        done_when=list(found.done_when),
        waits_on=[],
        tests=[],
    )


def _citation(place: architecture.Citation) -> Citation:
    """Writes a code checker's citation as an agent citation.

    Args:
        place: The code checker's citation.

    Returns:
        The agent citation with the same fields.
    """
    return Citation(
        file=place.file, line=place.line, end_line=place.end_line, quote=place.quote, shows=place.shows
    )


def _make_item(
    project: Project,
    modules: dict[str, Module],
    label: str,
    source: str,
    finding: Finding,
    old_id: str,
    old_verdict: OldVerdict | None,
) -> Item:
    """Places one finding in the code.

    Args:
        project: The project, as read from its .ratchet folder.
        modules: The project's modules by file path.
        label: The item's label.
        source: The source letter.
        finding: The finding to place.
        old_id: The old problem's id, or "" for a finding that is not an old entry.
        old_verdict: The old list checker's verdict for an old entry, or None.

    Returns:
        The item. Its subject is the finding's subject, or the function or class that holds the
        first cited line when the finding names none.
    """
    ranges = tuple(
        (place.file, place.line, max(place.line, place.end_line)) for place in finding.evidence
    )
    cited = [modules[file] for file, _, _ in ranges if file in modules]
    subject = finding.subject.strip()
    if not subject and ranges and ranges[0][0] in modules:
        subject = enclosing_name(project, modules[ranges[0][0]], ranges[0][1])
    names = tuple(dict.fromkeys(module.name for module in cited))
    return Item(label, source, finding, subject, ranges, old_id, old_verdict, names)


def _overlap(first: Item, second: Item) -> bool:
    """Tells whether two items cite overlapping lines of one file.

    Args:
        first: One item.
        second: The other item.

    Returns:
        True when some range of the first and some range of the second share a line.
    """
    return any(
        file == other_file and start <= other_end and other_start <= end
        for file, start, end in first.ranges
        for other_file, other_start, other_end in second.ranges
    )


def _join(members: dict[str, list[Item]], owner: dict[str, str], first: str, second: str) -> bool:
    """Moves the group of one label into the group of another, unless that breaks the source rule.

    Args:
        members: The items of each group, by the group's id.
        owner: The id of the group that holds each label.
        first: A label.
        second: Another label.

    Returns:
        True when the two labels are now in one group, and False when the join was refused.
    """
    keep, move = owner[first], owner[second]
    if keep == move:
        return True
    if {item.source for item in members[keep]} & {item.source for item in members[move]}:
        return False
    for item in members[move]:
        owner[item.label] = keep
    members[keep].extend(members.pop(move))
    return True


def _in_order(items: list[Item], members: dict[str, list[Item]]) -> list[list[Item]]:
    """Lists the groups with their items, and the groups themselves, in the order of the items.

    Args:
        items: Every item, in order.
        members: The items of each group, by the group's id.

    Returns:
        The groups.
    """
    order = {item.label: index for index, item in enumerate(items)}
    groups = [sorted(group, key=lambda item: order[item.label]) for group in members.values()]
    return sorted(groups, key=lambda group: order[group[0].label])


def _closeness(first: Item, second: Item) -> float:
    """Scores how likely two items are to be about the same problem.

    Args:
        first: One item.
        second: The other item.

    Returns:
        0 when the items have nothing in common, and a larger score the more they share. A shared
        file adds 2, a shared parent adds 1, and the share of identifiers in the shorter title
        that the other title also holds adds up to 3, when that share is over half.
    """
    score = 0.0
    if {file for file, _, _ in first.ranges} & {file for file, _, _ in second.ranges}:
        score += 2
    parent = first.subject.rpartition(".")[0]
    if parent and parent == second.subject.rpartition(".")[0]:
        score += 1
    words = _words(first.finding.title), _words(second.finding.title)
    if min(len(words[0]), len(words[1])) >= 2:
        share = len(words[0] & words[1]) / min(len(words[0]), len(words[1]))
        if share > 0.5:
            score += 3 * share
    return score


def _words(title: str) -> set[str]:
    """Picks the identifier-like words out of a title.

    Args:
        title: A finding's title.

    Returns:
        The lowercase words of four or more characters.
    """
    return {word.lower() for word in _WORD.findall(title)}


def _make_group(
    key: str, members: list[Item], reaudit: bool, design: TargetDesign | None, tests: tuple[str, ...]
) -> Group:
    """Builds one group from its items.

    Args:
        key: The group's key.
        members: The group's items.
        reaudit: True for a re-audit.
        design: The target design, or None.
        tests: The test files that reach the group's modules.

    Returns:
        The group, with its main text taken from the first source in TEXT_ORDER that it holds.
    """
    by_source = {item.source: item for item in members}
    chosen = next(by_source[source] for source in TEXT_ORDER if source in by_source)
    old = by_source.get(OLD)
    notes = []
    if old is not None and old.old_verdict is not None:
        status = _OLD_STATUS[old.old_verdict]
        if status == DONE and BLIND_AUDITOR in by_source:
            status = REOPENED
            notes.append(
                f"{old.old_id} was found done, but the blind auditor found a problem in the same place"
            )
    else:
        status = NEW if reaudit else OPEN
    return Group(
        key=key,
        items=tuple(members),
        related=(),
        status=status,
        text_label=chosen.label,
        text=chosen.finding,
        old_id=old.old_id if old is not None else "",
        area=area_of(design, chosen.subject),
        tests=tests,
        notes=tuple(notes),
    )


def _related(live: list[Group]) -> dict[str, tuple[str, ...]]:
    """Finds the groups that cite shared lines but are separate.

    Args:
        live: The groups that go to the final judge.

    Returns:
        The keys of the related groups for each group key that has any.
    """
    found: dict[str, list[str]] = {}
    for index, first in enumerate(live):
        for second in live[index + 1 :]:
            if any(_overlap(a, b) for a in first.items for b in second.items):
                found.setdefault(first.key, []).append(second.key)
                found.setdefault(second.key, []).append(first.key)
    return {key: tuple(keys) for key, keys in found.items()}


def _with_related(group: Group, related: tuple[str, ...]) -> Group:
    """Returns the group with its related keys set.

    Args:
        group: The group.
        related: The keys of the related groups.

    Returns:
        A copy of the group that names the related groups.
    """
    return Group(
        group.key, group.items, related, group.status, group.text_label, group.text,
        group.old_id, group.area, group.tests, group.notes,
    )
