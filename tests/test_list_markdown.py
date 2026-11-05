"""Checks that a draft is rendered as a problem list, loses its owner notes, and is checked."""

from __future__ import annotations

from pathlib import Path

from ratchet.audit.agent_answers import Citation
from ratchet.audit.list_markdown import (
    Draft,
    DraftDropped,
    DraftProblem,
    DraftQuestion,
    check_draft,
    render_draft,
    strip_owner_notes,
)
from ratchet.project import load_project
from ratchet.refactor.problem_list import find_latest_problem_list, split_entries


def problem(number: int, **fields: object) -> DraftProblem:
    """Builds a problem with the given id number and any field overridden."""
    values: dict[str, object] = dict(
        id=f"P{number}", title=f"title {number}", status="open", sources=["L1"], grade="stated",
        breaks="no copies", subject="app.views", what_is_wrong="It is repeated.")
    values.update(fields)
    return DraftProblem(**values)  # type: ignore[arg-type]


def make_draft() -> Draft:
    """Builds a draft with two problems, a question, a dropped entry and a bug."""
    first = problem(7, carried_from="problems-20261102-14h05m P7", notes=["check this one"],
                    evidence=[Citation(file="a.py", line=3, end_line=4,
                                       quote="# a comment\nx = 1", shows="the copy")],
                    simpler_option="Use one function.", done_when=["one copy remains"],
                    tests=[("tests/test_a.py", "imports a")])
    second = problem(8, status="new", after=["P7"])
    return Draft(problems=[first, second],
                 questions=[DraftQuestion("Q1", "Is it deliberate?", "It may be.")],
                 dropped=[DraftDropped("D1", "rounding copies", "They differ.")],
                 bugs=["parse drops the last row"], notes=["HEAD was at abc"])


def test_a_rendered_list_gives_exactly_the_problem_ids(tmp_path: Path) -> None:
    """Proves the newest-list reader finds the P ids and not the Q or D ids."""
    folder = tmp_path / ".ratchet"
    folder.mkdir()
    (folder / "config.toml").write_text('test_command = ["pytest"]\neditable = ["src/**"]\n',
                                        encoding="utf-8")
    (folder / "rules.md").write_text("rules\n", encoding="utf-8")
    (folder / "problems-20261102-14h05m.md").write_text(render_draft(make_draft()),
                                                        encoding="utf-8")
    project = load_project(tmp_path, commands=[])

    found = find_latest_problem_list(project)

    assert found.ids == ["P7", "P8"]
    assert [entry.id for entry in found.entries] == ["P7", "P8", "Q1", "D1"]


def test_rendered_entries_hold_their_field_lines() -> None:
    """Proves each field line is read back from the rendered entry."""
    entries = split_entries(render_draft(make_draft()))

    assert entries[0].fields == {
        "Status": "open", "Sources": "L1", "Grade": "stated (Breaks: no copies)",
        "Subject": "app.views", "Carried from": "problems-20261102-14h05m P7"}
    assert entries[1].fields["After"] == "P7"
    assert entries[2].section == "Questions for the owner"
    assert entries[3].section == "Dropped"


def test_a_rendered_draft_passes_the_check() -> None:
    """Proves a draft made by render_draft has no faults."""
    assert check_draft(render_draft(make_draft()), done_ids=[]) == []


def test_owner_notes_are_removed() -> None:
    """Proves both the per-entry notes and the notes section are removed."""
    text = render_draft(make_draft())
    assert "check this one" in text and "HEAD was at abc" in text

    stripped = strip_owner_notes(text)

    assert "check this one" not in stripped
    assert "HEAD was at abc" not in stripped
    assert "Owner notes" not in stripped
    assert "## Open" in stripped
    assert "\n\n\n" not in stripped
    assert check_draft(stripped, done_ids=[]) == []


def test_multi_line_owner_notes_are_removed() -> None:
    """Proves a note with several lines leaves nothing behind, on an entry or in the draft."""
    draft = Draft(problems=[problem(1, notes=["line one\nsecret two"])],
                  notes=["a\n# secret heading"])

    stripped = strip_owner_notes(render_draft(draft))

    assert "secret" not in stripped
    assert "line one" not in stripped
    assert "# secret heading" not in stripped


def test_a_repeated_id_is_named() -> None:
    """Proves a repeated P id is reported."""
    text = render_draft(Draft(problems=[problem(7), problem(7)]))

    assert check_draft(text, done_ids=[]) == ["P7 appears more than once"]


def test_a_done_id_is_named() -> None:
    """Proves a P id that is already done is reported."""
    text = render_draft(Draft(problems=[problem(7)]))

    assert check_draft(text, done_ids={"P7"}) == ["P7 reuses the id of a problem that is done"]


def test_a_carried_problem_may_keep_a_done_id() -> None:
    """Proves a done P id is allowed on an entry carried from that same id, and is still reported
    on an entry carried from a different id."""
    kept = render_draft(Draft(problems=[problem(7, carried_from="problems-20261102-14h05m P7")]))
    moved = render_draft(Draft(problems=[problem(7, carried_from="problems-20261102-14h05m P3")]))

    assert check_draft(kept, done_ids={"P7"}) == []
    assert check_draft(moved, done_ids={"P7"}) == ["P7 reuses the id of a problem that is done"]


def test_a_bad_status_is_named() -> None:
    """Proves an invalid Status is reported, and so is a missing one."""
    text = render_draft(Draft(problems=[problem(7, status="done")]))
    errors = check_draft(text, done_ids=[])
    assert len(errors) == 1 and errors[0].startswith("P7 has Status 'done'")

    missing = "## Open\n\n### P7. x\n\ntext\n"
    assert check_draft(missing, done_ids=[])[0].startswith("P7 has Status ''")


def test_an_after_that_names_no_problem_is_named() -> None:
    """Proves an After line naming a P id that the draft lacks is reported."""
    text = render_draft(Draft(problems=[problem(7, after=["P9"])]))

    assert check_draft(text, done_ids=[]) == ["P7 comes After P9, which is not in the draft"]


def test_a_problem_outside_open_is_named() -> None:
    """Proves a P heading under another section is reported."""
    text = render_draft(Draft(problems=[problem(7)])) + "\n## Later\n\n### P9. x\n\nStatus: open\n"

    assert check_draft(text, done_ids=[]) == ["P9 is under 'Later' and must be under Open"]


def test_the_done_list_is_rendered_as_bullets_the_readers_ignore(tmp_path: Path) -> None:
    """Proves the done problems are listed under Done, one bullet each, and that the draft check,
    the note removal and the list readers leave them alone."""
    from ratchet.audit.list_markdown import DraftDone

    draft = Draft(problems=[problem(7)],
                  done=[DraftDone("P4", "title 4", "done", "nothing\n is left"),
                        DraftDone("P5", "title 5", "gone", "")])

    text = render_draft(draft)

    done = text.split("## Done\n", 1)[1].split("\n## ", 1)[0]
    bullets = [line for line in done.splitlines() if line.strip()]
    assert bullets == ["- P4. title 4 (done): nothing is left", "- P5. title 5 (gone)"]
    assert check_draft(text, done_ids={"P4", "P5"}) == []
    assert "## Done" in strip_owner_notes(text)
    assert "- P4. title 4 (done): nothing is left" in strip_owner_notes(text)
    assert [entry.id for entry in split_entries(text)] == ["P7"]
    folder = tmp_path / ".ratchet"
    folder.mkdir()
    (folder / "config.toml").write_text('test_command = ["pytest"]\neditable = ["src/**"]\n',
                                        encoding="utf-8")
    (folder / "rules.md").write_text("rules\n", encoding="utf-8")
    (folder / "problems-20261102-14h05m.md").write_text(text, encoding="utf-8")
    assert find_latest_problem_list(load_project(tmp_path, commands=[])).ids == ["P7"]


def test_the_not_blind_line_survives_strip_owner_notes() -> None:
    """Proves a draft whose not_blind is set has a "Not blind:" line right under "# Problems",
    and that removing the owner notes keeps that line while it removes an owner note of the same
    draft."""
    note = ("The blind audit was skipped. .ratchet was committed to git history in a1b2c3d, so "
            "the old problem lists can still be read there.")
    draft = Draft(problems=[problem(7, notes=["check this one"])], notes=["HEAD was at abc"],
                  not_blind=note)

    text = render_draft(draft)

    assert text.startswith(f"# Problems\n\nNot blind: {note}\n\n")
    stripped = strip_owner_notes(text)
    assert f"Not blind: {note}" in stripped
    assert "check this one" not in stripped and "HEAD was at abc" not in stripped
    assert check_draft(stripped, done_ids=[]) == []
