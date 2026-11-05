"""Checks how the audit agents' instructions and turn prompts are built."""

from __future__ import annotations

from pathlib import Path

import pytest

from ratchet.architecture import Citation as CodeCitation
from ratchet.architecture import CodeFinding
from ratchet.audit.agent_answers import (
    Citation,
    EntryReview,
    Finding,
    Grade,
    OldVerdict,
    Question,
    QuestionKind,
    ReviewVerdict,
)
from ratchet.audit.agent_profiles import (
    FRESH,
    Role,
    build_profile,
    format_auditor_prompt,
    format_check_prompt,
    format_pair_prompt,
    format_recheck_prompt,
    format_review_prompt,
    make_agents,
    write_profile,
)
from ratchet.audit.code_facts import InventoryItem
from ratchet.audit.grouping import Group, Item, PairCandidate
from ratchet.audit.settings import COMMAND, load_settings
from ratchet.project import Project, load_project

CONFIG = 'test_command = ["pytest"]\neditable = ["src/**"]\n'


def make_project(root: Path) -> Project:
    """Writes a .ratchet folder into 'root' and returns the project read from it."""
    folder = root / ".ratchet"
    folder.mkdir()
    (folder / "config.toml").write_text(CONFIG, encoding="utf-8")
    (folder / "rules.md").write_text("1. Views never import models.\n", encoding="utf-8")
    return load_project(root, commands=[COMMAND])


def make_finding(title: str = "Copy of the strip code") -> Finding:
    """Returns a standard-grade finding with one citation."""
    place = Citation(file="src/app/views.py", line=3, end_line=4, quote="x = 1", shows="the copy")
    return Finding(
        subject="app.views", title=title, what_is_wrong="wrong", grade=Grade.STANDARD, breaks="S7",
        inferred_from=[], history="", evidence=[place], simpler_option="", done_when=[],
        waits_on=[], tests=[],
    )


def make_item(label: str, title: str) -> Item:
    """Returns an item that holds a finding with the given title."""
    return Item(label, label[0], make_finding(title), "app.views", (), "", None, ())


def make_group(old_id: str = "") -> Group:
    """Returns a group of two items, whose main text is the first."""
    first, second = make_item("L1", "Main text"), make_item("A1", "Other text")
    return Group(
        key="G1", items=(first, second), related=("G2",), status="open", text_label="L1",
        text=first.finding, old_id=old_id, area="views", tests=(),
        notes=(
            "a note",
            "P3 was found done, but the fresh audit found a problem in the same place",
        ),
    )


@pytest.mark.parametrize("role", list(Role))
def test_a_profile_is_filled_in(tmp_path: Path, role: Role) -> None:
    """Proves every profile holds the shared prompt with the rules and the standard, followed by
    the role's own prompt, with nothing left to fill in."""
    profile = build_profile(make_project(tmp_path), role)

    assert profile.startswith("# The audit")
    assert "1. Views never import models." in profile
    assert "S21." in profile
    assert "# Your role: the " in profile
    assert "$" not in profile


def test_the_rechecker_asks_for_current_citations_for_every_id(tmp_path: Path) -> None:
    """Proves the rechecker's instructions and prompt ask for current citations and a current
    subject for every old id, and that a retry names the ids that are missing."""
    profile = build_profile(make_project(tmp_path), Role.RECHECKER)
    prompt = format_recheck_prompt(
        "old list", {"P1": "history one", "P2": "history two"}, ["abc1234 publish"], "log", "hints"
    )
    retry = format_recheck_prompt("old list", {"P1": "h"}, [], "log", "hints", missing=["P1"])

    assert "current citations" in profile and "current subject" in profile
    assert "the done and gone ones included" in profile
    assert "every old id: P1, P2" in prompt
    assert "current citations and a current subject" in prompt
    assert "# A retry" not in prompt
    assert "# A retry" in retry and "P1" in retry.split("# A retry")[1]
    assert "Answer for every old id again: P1" in retry


def test_the_checker_describes_its_outcomes(tmp_path: Path) -> None:
    """Proves the checker's instructions name every outcome and continues_old."""
    profile = build_profile(make_project(tmp_path), Role.CHECKER)

    for outcome in ("keep", "split", "merge_into", "drop", "question", "bug"):
        assert f"`{outcome}`" in profile
    assert "`continues_old`" in profile


def test_the_checker_lets_a_merge_give_entries_and_keeps_old_ids_out_of_questions(
    tmp_path: Path,
) -> None:
    """Proves the checker is told a merge gives entries, and that a group with an old id never
    comes out as a question or a bug."""
    profile = build_profile(make_project(tmp_path), Role.CHECKER)
    merge_bullet = profile.split("- `merge_into`")[1].split("- `drop`")[0]

    assert "Give no entries for `drop`." in profile
    assert "Give no entries for `drop` and `merge_into`" not in profile
    assert "final entries" in merge_bullet
    assert "never turned into a `question`" in profile.replace("\n", " ")


def test_the_blind_audits_prompt_takes_no_list(tmp_path: Path) -> None:
    """Proves the auditor's prompt builder takes only the request, the code findings and the
    inventory, and holds nothing else."""
    found = CodeFinding(
        kind="cycle", subject="app.a", title="a cycle", what_is_wrong="w", grade="standard",
        breaks="S2", evidence=(CodeCitation("src/app/a.py", 1, 1, "import b", "shows"),),
        simpler_option="s", done_when=("d",),
    )
    inventory = [InventoryItem("app.a", "module", "src/app/a.py", 1, 10)]

    prompt = format_auditor_prompt("look at views", [found], inventory)

    assert "look at views" in prompt
    assert "a cycle" in prompt
    assert '"name": "app.a"' in prompt
    assert "ledger" not in prompt.lower()
    assert "# The list" not in prompt
    assert format_auditor_prompt("", [], inventory).startswith("# The faults that code found")


def test_a_review_prompt_labels_the_list() -> None:
    """Proves the list reviewers are given the list as JSON under its labels."""
    prompt = format_review_prompt({"L1": make_finding("Listed title")}, [])

    assert '"L1"' in prompt
    assert "Listed title" in prompt


def test_a_pair_prompt_numbers_the_pairs() -> None:
    """Proves the pair judge is given each pair under its number."""
    pair = PairCandidate(7, make_item("L1", "First"), make_item("A1", "Second"), 0.5)

    prompt = format_pair_prompt([pair])

    assert '"pair": 7' in prompt
    assert "First" in prompt and "Second" in prompt
    assert "src/app/views.py:3-4" in prompt


def test_a_check_prompt_hides_the_sources() -> None:
    """Proves the checker is given each group's text, other texts and related groups, but no
    label that names a source."""
    prompt = format_check_prompt([make_group("P3")], "a report", [])

    assert "Main text" in prompt and "Other text" in prompt
    assert '"related": [\n      "G2"' in prompt
    assert '"old_id": "P3"' in prompt
    assert "a report" in prompt
    assert '"L1"' not in prompt and '"A1"' not in prompt
    assert "fresh audit" not in prompt


def test_a_check_prompt_shows_the_verdicts_without_their_sources() -> None:
    """Proves the checker is given a dispute reason next to the text it disputes and the
    rechecker's verdict on an old entry, and that no label names a source."""
    old = Item(
        "R1", "R", make_finding("Old text"), "app.views", (), "P3", OldVerdict.OPEN, ()
    )
    other = make_item("L1", "Disputed text")
    group = Group(
        key="G1", items=(old, other), related=(), status="open", text_label="R1",
        text=old.finding, old_id="P3", area="views", tests=(), notes=(),
    )
    review = EntryReview(
        label="L1", verdict=ReviewVerdict.DISPUTE, reason="the copies differ on empty input",
        finding=other.finding,
    )

    prompt = format_check_prompt([group], "", [], {"L1": [review]})

    assert "the copies differ on empty input" in prompt
    assert '"verdict": "dispute"' in prompt
    assert '"old_verdict": "open"' in prompt
    assert '"L1"' not in prompt and '"R1"' not in prompt


def test_a_check_prompt_shows_a_corrected_text_without_its_source() -> None:
    """Proves the checker is given the corrected finding next to a `correct` verdict, and that
    no label names a source."""
    item = make_item("L1", "Listed title")
    group = Group(
        key="G1", items=(item,), related=(), status="open", text_label="L1",
        text=item.finding, old_id="", area="views", tests=(), notes=(),
    )
    review = EntryReview(
        label="L1", verdict=ReviewVerdict.CORRECT, reason="the title was too broad",
        finding=make_finding("Corrected title"),
    )

    prompt = format_check_prompt([group], "", [], {"L1": [review]})

    assert '"verdict": "correct"' in prompt
    assert "Corrected title" in prompt
    assert '"L1"' not in prompt
    assert '"L"' not in prompt


def test_a_check_prompt_shows_each_question_with_its_answer() -> None:
    """Proves the checker is given the text of each answered question next to the owner's answer."""
    place = Citation(file="src/app/views.py", line=3, end_line=4, quote="x = 1", shows="the copy")
    question = Question(
        kind=QuestionKind.DESIGN, title="Why two stores", evidence=[place],
        why_it_may_be_deliberate="a migration", what_would_settle="the owner's plan",
    )

    prompt = format_check_prompt([make_group()], "", [("Q1", question, "yes, keep both")])

    assert '"label": "Q1"' in prompt
    assert "Why two stores" in prompt
    assert "a migration" in prompt
    assert "yes, keep both" in prompt


def test_written_profiles_are_accepted_by_agents(tmp_path: Path) -> None:
    """Proves make_agents gives an agent for each role and one for the blind audit, with the
    profiles written, without running them."""
    project = make_project(tmp_path)
    settings = load_settings(project)
    copy = tmp_path / "copy"
    copy.mkdir()

    agents = make_agents(project, settings, copy, tmp_path / "audit")

    assert set(agents) == {str(role) for role in Role} | {FRESH}
    path = write_profile(project, Role.AUDITOR, tmp_path / "audit" / "profiles")
    assert path.read_text(encoding="utf-8") == build_profile(project, Role.AUDITOR)
