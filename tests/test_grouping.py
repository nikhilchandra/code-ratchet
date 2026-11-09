"""Checks the deterministic merge on small packages and hand-made findings."""

from __future__ import annotations

from pathlib import Path

import pytest

import ratchet.audit.grouping as grouping
from ratchet.architecture import Citation as CodeCitation
from ratchet.architecture import CodeFinding
from ratchet.audit.agent_answers import (
    Citation,
    Finding,
    Grade,
    OldEntryCheck,
    OldVerdict,
    PairVerdict,
    Question,
    QuestionKind,
)
from ratchet.audit.grouping import (
    BLOCKED,
    DONE,
    NEW,
    OPEN,
    REOPENED,
    Item,
    finish_groups,
    group_items,
    join_pairs,
    make_items,
    pair_candidates,
)
from ratchet.project import Project
from test_architecture import make_design, make_project, package

MODEL = "\n".join(f"x{number} = {number}" for number in range(1, 41)) + "\n"
MODEL_FILE = "src/app/core/model.py"
RULES_FILE = "src/app/core/rules.py"


def project_with_files(tmp_path: Path) -> Project:
    """Returns a project with a core part holding model.py and rules.py, and a cli part."""
    return make_project(tmp_path, {
        **package("core", "cli"),
        MODEL_FILE: MODEL,
        RULES_FILE: MODEL,
        "src/app/cli/main.py": "from app.core import model\n",
        "tests/test_model.py": "from app.core import model\n",
    })


def cite(file: str, line: int, end: int | None = None) -> Citation:
    """Returns a citation of the lines from line to end."""
    return Citation(file=file, line=line, end_line=end or line, quote="x", shows="shows")


def finding(subject: str, title: str, *places: Citation) -> Finding:
    """Returns a standard-grade finding that cites the given places."""
    return Finding(
        subject=subject, title=title, what_is_wrong="wrong", grade=Grade.STANDARD, breaks="S7",
        inferred_from=[], history="", evidence=list(places), simpler_option="", done_when=[],
        waits_on=[], tests=[],
    )


def old_check(old_id: str, verdict: OldVerdict, found: Finding) -> OldEntryCheck:
    """Returns the old list checker's check of one old problem."""
    return OldEntryCheck(id=old_id, verdict=verdict, what_is_left="left", finding=found)


def build(project: Project, **sources: list) -> list[Item]:
    """Makes the items of the given sources, with the other sources empty."""
    empty = {
        "code": [], "listed": [], "added_a": [], "added_b": [], "old": [], "from_notes": [],
        "blind_auditor": [],
    }
    return make_items(project, make_design(), **{**empty, **sources})


def labels(groups: list[list[Item]]) -> list[list[str]]:
    """Returns the labels of each group."""
    return [[item.label for item in group] for group in groups]


def test_overlap_groups_and_distant_ranges_stay_apart(tmp_path: Path) -> None:
    """Proves overlapping ranges join, distant ranges stay apart and labels count from 1."""
    project = project_with_files(tmp_path)
    items = build(
        project,
        listed=[
            finding("app.core.model", "one", cite(MODEL_FILE, 5, 9)),
            finding("app.core.rules", "two", cite(RULES_FILE, 1)),
        ],
        blind_auditor=[
            finding("app.core.model.other", "three", cite(MODEL_FILE, 8, 12)),
            finding("app.core.model.far", "four", cite(MODEL_FILE, 30)),
        ],
    )

    assert labels(group_items(items)) == [["L1", "F1"], ["L2"], ["F2"]]


def test_the_same_subject_groups_without_overlap(tmp_path: Path) -> None:
    """Proves two findings about one subject join when their lines are apart."""
    project = project_with_files(tmp_path)
    items = build(
        project,
        listed=[finding("app.core.model", "one", cite(MODEL_FILE, 1))],
        added_a=[finding("app.core.model", "two", cite(MODEL_FILE, 30))],
    )

    assert labels(group_items(items)) == [["L1", "A1"]]


def test_findings_with_no_subject_in_other_files_stay_apart(tmp_path: Path) -> None:
    """Proves two findings with no subject do not join because both subjects are empty."""
    project = project_with_files(tmp_path)
    items = build(
        project,
        listed=[finding("", "one", cite("README.md", 1))],
        blind_auditor=[finding("", "two", cite("tests/test_model.py", 1))],
    )

    assert labels(group_items(items)) == [["L1"], ["F1"]]


def test_one_source_never_groups_with_itself(tmp_path: Path) -> None:
    """Proves two findings of one source stay apart even when they overlap and share a subject."""
    project = project_with_files(tmp_path)
    items = build(
        project,
        listed=[
            finding("app.core.model", "one", cite(MODEL_FILE, 1, 5)),
            finding("app.core.model", "two", cite(MODEL_FILE, 3, 8)),
        ],
    )

    assert labels(group_items(items)) == [["L1"], ["L2"]]


def test_an_old_entry_is_placed_by_the_old_list_checker_citations(tmp_path: Path) -> None:
    """Proves the old entry joins the finding at its current place, not at its old lines."""
    project = project_with_files(tmp_path)
    moved = finding("app.core.model.moved", "old title", cite(MODEL_FILE, 30, 33))
    items = build(
        project,
        old=[old_check("P7", OldVerdict.OPEN, moved)],
        blind_auditor=[
            finding("app.core.model.near", "at the old place", cite(MODEL_FILE, 1, 4)),
            finding("app.core.model.here", "at the new place", cite(MODEL_FILE, 31)),
        ],
    )

    groups = group_items(items)

    assert labels(groups) == [["F1"], ["F2", "O P7"]]
    assert items[-1].old_id == "P7"
    assert items[-1].ranges == ((MODEL_FILE, 30, 33),)


def test_a_blind_auditor_finding_over_two_old_entries_makes_them_related(tmp_path: Path) -> None:
    """Proves a blind auditor finding joins one old entry and is related to the other."""
    project = project_with_files(tmp_path)
    items = build(
        project,
        old=[
            old_check("P1", OldVerdict.OPEN, finding("app.core.model.a", "a", cite(MODEL_FILE, 1, 5))),
            old_check("P2", OldVerdict.OPEN, finding("app.core.model.b", "b", cite(MODEL_FILE, 8, 12))),
        ],
        blind_auditor=[finding("app.core.model.c", "big", cite(MODEL_FILE, 3, 10))],
    )

    result = finish_groups(project, make_design(), items, group_items(items), reaudit=True)

    by_old = {group.old_id: group for group in result.groups}
    assert [[item.label for item in group.items] for group in result.groups] == [
        ["F1", "O P1"], ["O P2"],
    ]
    assert by_old["P1"].related == (by_old["P2"].key,)
    assert by_old["P2"].related == (by_old["P1"].key,)


def test_pairs_are_capped_and_the_judge_joins_the_same_ones(tmp_path: Path) -> None:
    """Proves candidates are ranked and capped, and only the pairs called the same are joined."""
    project = project_with_files(tmp_path)
    items = build(
        project,
        listed=[
            finding("app.core.model.a", "vote tally written twice", cite(MODEL_FILE, 1)),
            finding("app.core.rules.b", "unrelated", cite(RULES_FILE, 1)),
        ],
        blind_auditor=[
            finding("app.core.model.c", "vote tally repeated", cite(MODEL_FILE, 20)),
            finding("app.core.rules.d", "other", cite(RULES_FILE, 30)),
        ],
    )
    groups = group_items(items)

    candidates = pair_candidates(groups, max_pairs=2)
    verdicts = [
        PairVerdict(pair=1, same=True, reason="same tally"),
        PairVerdict(pair=2, same=False, reason="no"),
    ]
    joined, records = join_pairs(groups, candidates, verdicts)

    assert [candidate.pair for candidate in candidates] == [1, 2]
    assert len(pair_candidates(groups, max_pairs=1)) == 1
    assert (records[0].first, records[0].second) == ("L1", "F1")
    assert [record.joined for record in records] == [True, False]
    assert labels(joined) == [["L1", "F1"], ["L2"], ["F2"]]


def test_a_join_that_would_hold_two_of_one_source_is_refused(tmp_path: Path) -> None:
    """Proves the judge cannot put two items of one source in a group."""
    project = project_with_files(tmp_path)
    items = build(
        project,
        listed=[
            finding("app.core.model.a", "a", cite(MODEL_FILE, 1)),
            finding("app.core.model.b", "b", cite(MODEL_FILE, 20)),
        ],
        blind_auditor=[finding("app.core.model.c", "c", cite(MODEL_FILE, 30))],
    )
    groups = group_items(items)
    candidates = pair_candidates(groups, max_pairs=5)
    verdicts = [PairVerdict(pair=item.pair, same=True, reason="same") for item in candidates]

    joined, records = join_pairs(groups, candidates, verdicts)

    assert labels(joined) == [["L1", "F1"], ["L2"]]
    assert [record.joined for record in records] == [True, False]
    assert "refused" in records[1].reason


def test_a_missing_judge_answer_is_recorded(tmp_path: Path) -> None:
    """Proves a pair the judge did not answer stays apart and is recorded."""
    project = project_with_files(tmp_path)
    items = build(
        project,
        listed=[finding("app.core.model.a", "a", cite(MODEL_FILE, 1))],
        blind_auditor=[finding("app.core.model.b", "b", cite(MODEL_FILE, 30))],
    )
    groups = group_items(items)

    joined, records = join_pairs(groups, pair_candidates(groups, 5), [])

    assert labels(joined) == [["L1"], ["F1"]]
    assert records[0].same is None


def test_status_follows_the_old_list_checker(tmp_path: Path) -> None:
    """Proves each verdict gives its status, done goes to the done list, and no old entry is new."""
    project = project_with_files(tmp_path)
    verdicts = [
        OldVerdict.REOPEN, OldVerdict.CORRECTED, OldVerdict.BLOCKED, OldVerdict.DONE,
        OldVerdict.GONE,
    ]
    old = [
        old_check(
            f"P{number}", verdict,
            finding(f"app.core.rules.s{number}", "t", cite(RULES_FILE, number * 4)),
        )
        for number, verdict in enumerate(verdicts, start=1)
    ]
    items = build(project, old=old, blind_auditor=[finding("app.core.model.n", "n", cite(MODEL_FILE, 1))])

    result = finish_groups(project, make_design(), items, group_items(items), reaudit=True)

    assert {group.old_id or "blind_auditor": group.status for group in result.groups} == {
        "P1": REOPENED, "P2": OPEN, "P3": BLOCKED, "blind_auditor": NEW,
    }
    assert sorted(group.old_id for group in result.done) == ["P4", "P5"]
    assert {group.status for group in result.done} == {DONE}


def test_a_done_entry_with_a_blind_auditor_finding_is_reopened(tmp_path: Path) -> None:
    """Proves done with a blind auditor finding in the group gives reopened and an owner note."""
    project = project_with_files(tmp_path)
    items = build(
        project,
        old=[old_check("P3", OldVerdict.DONE, finding("app.core.model.a", "a", cite(MODEL_FILE, 1, 5)))],
        blind_auditor=[finding("app.core.model.b", "b", cite(MODEL_FILE, 4))],
    )

    result = finish_groups(project, make_design(), items, group_items(items), reaudit=True)

    assert [group.status for group in result.groups] == [REOPENED]
    assert result.done == ()
    assert result.groups[0].notes[0] == (
        "P3 was found done, but the blind auditor found a problem in the same place")


def test_a_first_audit_is_open_with_text_area_and_tests(tmp_path: Path) -> None:
    """Proves the text comes from the first source in order, and area and tests are filled."""
    project = project_with_files(tmp_path)
    items = build(
        project,
        listed=[finding("app.core.model", "listed", cite(MODEL_FILE, 1, 5))],
        blind_auditor=[finding("app.core.model", "blind_auditor", cite(MODEL_FILE, 2))],
    )

    result = finish_groups(project, make_design(), items, group_items(items), reaudit=False)

    group = result.groups[0]
    assert (group.key, group.status, group.text_label, group.text.title) == (
        "G1", OPEN, "L1", "listed",
    )
    assert group.area == "core"
    assert group.tests == ("tests/test_model.py",)


def test_a_finding_with_no_subject_is_named_from_its_lines(tmp_path: Path) -> None:
    """Proves the holder of the first cited line is used when the finding names no subject."""
    source = "class Order:\n    def total(self):\n        return 1\n"
    project = make_project(tmp_path, {**package("core"), MODEL_FILE: source})

    items = build(project, listed=[finding("", "t", cite(MODEL_FILE, 3))])

    assert items[0].subject == "app.core.model.Order.total"


def test_a_code_finding_becomes_an_item_labeled_c(tmp_path: Path) -> None:
    """Proves a code finding becomes C1 with its evidence as a range and its grade kept."""
    project = project_with_files(tmp_path)
    place = CodeCitation("src/app/cli/main.py", 1, 1, "from app.core import model", "imports")
    code = CodeFinding(
        kind="may_use", subject="app.cli.main", title="t", what_is_wrong="w", grade="stated",
        breaks="rule", evidence=(place,), simpler_option="s", done_when=("d",),
    )

    items = build(project, code=[code])

    assert (items[0].label, items[0].ranges, items[0].finding.grade) == (
        "C1", (("src/app/cli/main.py", 1, 1),), Grade.STATED,
    )


def test_coverage_is_checked(tmp_path: Path) -> None:
    """Proves a group set that drops or repeats an item is refused."""
    project = project_with_files(tmp_path)
    items = build(
        project,
        listed=[finding("app.core.model.a", "a", cite(MODEL_FILE, 1))],
        blind_auditor=[finding("app.core.model.b", "b", cite(MODEL_FILE, 30))],
    )
    groups = group_items(items)

    for broken in (groups[:1], groups + groups[:1]):
        with pytest.raises(ValueError, match="exactly once"):
            finish_groups(project, make_design(), items, broken, reaudit=False)


def test_a_done_or_gone_entry_keeps_only_its_own_item_and_the_rest_go_on_as_new(
    tmp_path: Path,
) -> None:
    """Proves a done or gone old entry goes to the done list alone, and the code and note findings
    grouped with it come back as a new group for the final judge, with a note naming the old id."""
    project = project_with_files(tmp_path)

    def code_finding(subject: str, line: int, end: int) -> CodeFinding:
        place = CodeCitation(MODEL_FILE, line, end, "x", "the line")
        return CodeFinding(
            kind="may_use", subject=subject, title=f"code {line}", what_is_wrong="w",
            grade="stated", breaks="rule", evidence=(place,), simpler_option="s", done_when=("d",),
        )

    items = build(
        project,
        code=[code_finding("app.core.model.c1", 2, 4), code_finding("app.core.model.c2", 31, 32)],
        from_notes=[finding("app.core.model.r1", "from notes", cite(MODEL_FILE, 3, 6))],
        old=[
            old_check("P1", OldVerdict.DONE, finding("app.core.model.p1", "p1", cite(MODEL_FILE, 1, 5))),
            old_check("P2", OldVerdict.GONE, finding("app.core.model.p2", "p2", cite(MODEL_FILE, 30, 33))),
        ],
    )
    assert labels(group_items(items)) == [["C1", "R1", "O P1"], ["C2", "O P2"]]

    result = finish_groups(project, make_design(), items, group_items(items), reaudit=True)

    assert [[item.label for item in group.items] for group in result.done] == [["O P1"], ["O P2"]]
    assert [group.old_id for group in result.done] == ["P1", "P2"]
    assert {group.status for group in result.done} == {DONE}
    split = {tuple(item.label for item in group.items): group for group in result.groups}
    assert set(split) == {("C1", "R1"), ("C2",)}
    first, second = split[("C1", "R1")], split[("C2",)]
    assert (first.status, first.old_id, first.text_label) == (NEW, "", "R1")
    assert (second.status, second.old_id, second.text_label) == (NEW, "", "C2")
    assert any("P1" in note for note in first.notes)
    assert any("P2" in note for note in second.notes)
    keys = [group.key for group in (*result.groups, *result.done)]
    assert sorted(keys, key=lambda key: int(key[1:])) == [f"G{n}" for n in range(1, len(keys) + 1)]


def question(title: str, *places: Citation) -> Question:
    """Returns a question for the owner with the given title, citing the given places."""
    return Question(
        kind=QuestionKind.DESIGN, title=title, evidence=list(places),
        why_it_may_be_deliberate="Prices are added as plain numbers.",
        what_would_settle="Yes keeps the numbers, no moves money to Decimal.",
    )


def test_questions_citing_overlapping_lines_are_offered_as_pairs_not_joined() -> None:
    """Proves three questions from three sources that cite overlapping lines of one file, each in
    its own words, are left apart by code and offered to the duplicate judge as pairs, since
    questions about the same lines may still ask different things."""
    items = grouping.make_question_items([
        (grouping.LISTED, question("Is float arithmetic fine for money?", cite(MODEL_FILE, 6, 7))),
        (grouping.ADDED_A, question("Should prices be kept as Decimal?", cite(MODEL_FILE, 7))),
        (grouping.ADDED_B, question("Can totals lose cents when adding?", cite(MODEL_FILE, 7, 9))),
    ])

    groups = grouping.group_questions(items)
    candidates = pair_candidates(groups, max_pairs=5)

    assert labels(groups) == [["L1"], ["A1"], ["B1"]]
    assert sorted((candidate.first.label, candidate.second.label) for candidate in candidates) == [
        ("A1", "B1"), ("L1", "A1"), ("L1", "B1")]


def test_questions_with_nearly_the_same_wording_become_one_group() -> None:
    """Proves two questions with no citations, whose titles differ only by question words and
    case, are joined by code into one group."""
    items = grouping.make_question_items([
        (grouping.LISTED, question("Should total() use Decimal for money?")),
        (grouping.ADDED_A, question("would TOTAL use decimal for money")),
    ])

    assert labels(grouping.group_questions(items)) == [["L1", "A1"]]


def test_a_longer_question_is_not_joined_by_code_and_is_offered_as_a_pair() -> None:
    """Proves a question that adds words to another is not joined by code, and that the two are
    offered to the duplicate judge as the first pair."""
    items = grouping.make_question_items([
        (grouping.LISTED, question("Should total() use Decimal?")),
        (grouping.ADDED_A, question("Should total() use Decimal for tax?")),
    ])

    groups = grouping.group_questions(items)
    candidates = pair_candidates(groups, max_pairs=5)

    assert labels(groups) == [["L1"], ["A1"]]
    assert [(candidate.pair, candidate.first.label, candidate.second.label)
            for candidate in candidates] == [(1, "L1", "A1")]


def test_a_question_made_only_of_question_words_is_not_joined_and_does_not_fail() -> None:
    """Proves two titles made only of question words stay two groups, with no division by zero."""
    items = grouping.make_question_items([
        (grouping.LISTED, question("What is this?")),
        (grouping.ADDED_A, question("Which is it?")),
    ])

    assert labels(grouping.group_questions(items)) == [["L1"], ["A1"]]
