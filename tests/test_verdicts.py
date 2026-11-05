"""Checks how the checker's verdicts are applied to hand-made groups."""

from __future__ import annotations

from ratchet.audit.agent_answers import (
    CheckAnswer,
    CheckedEntry,
    Citation,
    Finding,
    Grade,
    GroupCheck,
    OldVerdict,
    Outcome,
)
from ratchet.audit.grouping import CODE, FRESH, LISTED, OLD, Group, GroupingResult, Item
from ratchet.audit.verdicts import apply_verdicts


def make_finding(title: str, grade: Grade = Grade.STANDARD) -> Finding:
    """Returns a finding with the given title and grade."""
    return Finding(
        subject="app.core", title=title, what_is_wrong="wrong", grade=grade, breaks="S7",
        inferred_from=[], history="",
        evidence=[Citation(file="a.py", line=1, end_line=1, quote="x", shows="shows")],
        simpler_option="", done_when=[], waits_on=[], tests=[],
    )


def make_item(label: str, source: str, found: Finding, old_id: str = "") -> Item:
    """Returns an item that cites a.py line 1."""
    verdict = OldVerdict.REOPEN if old_id else None
    return Item(label, source, found, found.subject, (("a.py", 1, 1),), old_id, verdict, ())


def make_group(key: str, *items: Item, related: tuple[str, ...] = (), status: str = "open") -> Group:
    """Returns a group of the given items, with the first item's text."""
    old_id = next((item.old_id for item in items if item.old_id), "")
    return Group(
        key, items, related, status, items[0].label, items[0].finding, old_id, "core", ("t.py",), ()
    )


def entry(title: str, continues: bool = False) -> CheckedEntry:
    """Returns a checked entry with the given title."""
    return CheckedEntry(continues_old=continues, finding=make_finding(title))


def check(key: str, outcome: Outcome, *entries: CheckedEntry, merge_into: str = "") -> GroupCheck:
    """Returns the checker's decision on one group."""
    return GroupCheck(
        key=key, outcome=outcome, merge_into=merge_into, reason="because", entries=list(entries)
    )


def apply(groups: list[Group], *checks: GroupCheck):
    """Applies the checks to the groups."""
    return apply_verdicts(GroupingResult(tuple(groups), ()), CheckAnswer(checks=list(checks), decisions=[]))


def old_group(key: str = "G1", related: tuple[str, ...] = ()) -> Group:
    """Returns a group that holds the old problem P7 and a fresh finding."""
    return make_group(
        key,
        make_item("O P7", OLD, make_finding("old"), "P7"),
        make_item("F1", FRESH, make_finding("fresh")),
        related=related, status="reopened",
    )


def titles(entries) -> list[str]:
    """Returns the titles of the final entries."""
    return [found.finding.title for found in entries]


def test_keep_gives_one_corrected_entry() -> None:
    """Proves a kept group comes out as the checker's text, with the group's status and tests."""
    group = make_group("G1", make_item("L1", LISTED, make_finding("listed")))

    result = apply([group], check("G1", Outcome.KEEP, entry("corrected")))

    assert titles(result.problems) == ["corrected"]
    assert result.problems[0].status == "open"
    assert result.problems[0].sources == ("L1",)
    assert result.problems[0].tests == ("t.py",)
    assert result.problems[0].old_id == ""


def test_split_gives_the_old_id_to_the_continuing_entry_only() -> None:
    """Proves a split keeps the old id and status on one entry and makes the others new."""
    result = apply(
        [old_group()],
        check("G1", Outcome.SPLIT, entry("part one"), entry("part two", True)),
    )

    assert titles(result.problems) == ["part one", "part two"]
    assert [found.old_id for found in result.problems] == ["", "P7"]
    assert [found.status for found in result.problems] == ["new", "reopened"]


def test_merge_into_adds_the_entries_to_the_related_group() -> None:
    """Proves a merge joins the entries and the result keeps the only old id."""
    fresh = make_group("G1", make_item("F1", FRESH, make_finding("fresh")), related=("G2",))
    old = old_group("G2", related=("G1",))

    result = apply(
        [fresh, old],
        check("G1", Outcome.MERGE_INTO, entry("moved"), merge_into="G2"),
        check("G2", Outcome.KEEP, entry("old now", True)),
    )

    assert titles(result.problems) == ["old now", "moved"]
    assert [found.old_id for found in result.problems] == ["P7", ""]
    assert result.problems[1].status == "open"


def test_merge_of_a_group_with_the_old_id_into_a_new_group_keeps_the_id() -> None:
    """Proves the old id survives when the group that holds it is the one merged away."""
    fresh = make_group("G1", make_item("F1", FRESH, make_finding("fresh")), related=("G2",))
    old = old_group("G2", related=("G1",))

    result = apply(
        [fresh, old],
        check("G1", Outcome.KEEP, entry("whole")),
        check("G2", Outcome.MERGE_INTO, entry("old part", True), merge_into="G1"),
    )

    assert titles(result.problems) == ["whole", "old part"]
    assert [found.old_id for found in result.problems] == ["", "P7"]
    assert result.problems[1].status == "reopened"


def test_a_merge_of_two_old_ids_is_refused_and_both_are_kept() -> None:
    """Proves two old problems stay as grouped, each with its id, and the owner is told."""
    first = old_group("G1", related=("G2",))
    second = make_group(
        "G2", make_item("O P8", OLD, make_finding("other"), "P8"), related=("G1",), status="open"
    )

    result = apply(
        [first, second],
        check("G1", Outcome.MERGE_INTO, entry("joined", True), merge_into="G2"),
        check("G2", Outcome.KEEP, entry("target", True)),
    )

    assert titles(result.problems) == ["old", "other"]
    assert [found.old_id for found in result.problems] == ["P7", "P8"]
    assert all(found.notes for found in result.problems)


def test_two_old_ids_merging_into_one_new_group_are_refused() -> None:
    """Proves two old problems that both merge into the same new group stay as grouped, each with
    its id, and the owner is told."""
    whole = make_group("G1", make_item("F1", FRESH, make_finding("whole")), related=("G2", "G3"))
    first = old_group("G2", related=("G1",))
    second = make_group(
        "G3", make_item("O P8", OLD, make_finding("other"), "P8"), related=("G1",), status="open"
    )

    result = apply(
        [whole, first, second],
        check("G1", Outcome.KEEP, entry("whole")),
        check("G2", Outcome.MERGE_INTO, entry("p7", True), merge_into="G1"),
        check("G3", Outcome.MERGE_INTO, entry("p8", True), merge_into="G1"),
    )

    assert sorted(found.old_id for found in result.problems if found.old_id) == ["P7", "P8"]
    assert titles(result.problems) == ["whole", "old", "other"]
    assert all(found.notes for found in result.problems if found.old_id)


def test_a_broken_answer_keeps_the_group_as_grouped_with_a_note() -> None:
    """Proves each way of breaking a rule gives the group's main text and a note."""
    two_continue = check("G1", Outcome.SPLIT, entry("a", True), entry("b", True))
    none_continue = check("G1", Outcome.KEEP, entry("a"))
    unrelated = check("G1", Outcome.MERGE_INTO, entry("a", True), merge_into="G9")
    no_answer = None

    for answer in (two_continue, none_continue, unrelated, no_answer):
        checks = [answer] if answer else []
        result = apply([old_group()], *checks)

        assert titles(result.problems) == ["old"]
        assert result.problems[0].old_id == "P7"
        assert result.problems[0].notes


def test_a_merge_into_a_group_that_is_not_kept_is_not_followed() -> None:
    """Proves a merge into a group that is dropped keeps the merging group as grouped."""
    first = make_group("G1", make_item("L1", LISTED, make_finding("one")), related=("G2",))
    second = make_group("G2", make_item("F1", FRESH, make_finding("two")), related=("G1",))

    result = apply(
        [first, second],
        check("G1", Outcome.MERGE_INTO, entry("moved"), merge_into="G2"),
        check("G2", Outcome.DROP),
    )

    assert titles(result.problems) == ["one"]
    assert [dropped.finding.title for dropped in result.dropped] == ["two"]


def test_an_old_problem_cannot_become_a_question() -> None:
    """Proves a question outcome on a group with an old id is refused."""
    result = apply([old_group()], check("G1", Outcome.QUESTION, entry("why")))

    assert titles(result.problems) == ["old"]
    assert result.questions == ()


def test_drop_question_and_bug_leave_the_open_section() -> None:
    """Proves each of these outcomes puts the group in its own section."""
    groups = [
        make_group(key, make_item(f"L{number}", LISTED, make_finding(key)))
        for number, key in enumerate(("G1", "G2", "G3"), start=1)
    ]

    result = apply(
        groups,
        check("G1", Outcome.DROP),
        check("G2", Outcome.QUESTION, entry("ask")),
        check("G3", Outcome.BUG, entry("bug")),
    )

    assert result.problems == ()
    assert titles(result.questions) == ["ask"]
    assert titles(result.bugs) == ["bug"]
    assert [(dropped.finding.title, dropped.reason) for dropped in result.dropped] == [("G1", "because")]


def test_a_stated_code_finding_outlives_a_dropped_group() -> None:
    """Proves the stated code finding becomes its own entry and a standard one does not."""
    stated = make_item("C1", CODE, make_finding("stated", Grade.STATED))
    standard = make_item("C2", CODE, make_finding("standard"))
    listed = make_item("L1", LISTED, make_finding("listed"))
    group = make_group("G1", listed, stated, standard)

    result = apply([group], check("G1", Outcome.DROP))

    assert titles(result.problems) == ["stated"]
    assert result.problems[0].sources == ("C1",)
    assert result.problems[0].old_id == ""
    assert len(result.dropped) == 1


def test_a_stated_code_finding_in_a_dropped_old_group_is_new() -> None:
    """Proves the stated code finding is a new problem when the dropped group held an old one."""
    old = make_item("O P7", OLD, make_finding("old"), "P7")
    stated = make_item("C1", CODE, make_finding("stated", Grade.STATED))
    group = make_group("G1", old, stated, status="open")

    result = apply([group], check("G1", Outcome.DROP))

    assert titles(result.problems) == ["stated"]
    assert result.problems[0].status == "new"
    assert result.problems[0].old_id == ""


def test_an_answer_for_an_unknown_group_is_noted() -> None:
    """Proves an answer for a key that is not a group is reported, and a missing answer keeps the group."""
    group = make_group("G1", make_item("L1", LISTED, make_finding("listed")))

    result = apply([group], check("G9", Outcome.DROP))

    assert titles(result.problems) == ["listed"]
    assert "G9" in result.notes[0]
