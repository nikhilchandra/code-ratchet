"""Checks the text that `ratchet problems` shows, on scratch projects."""

from __future__ import annotations

from pathlib import Path

import pytest

from ratchet.problems_view import describe_event, show_history, show_lists, show_problem
from ratchet.project import Project, ProjectError, load_project
from ratchet.refactor.ledger import Ledger, ProblemEvent, ProblemRecord, ProblemStatus, save_ledger

FIRST = "problems-20261102-14h05m"
SECOND = "problems-20261105-10h11m"

FIRST_LIST = """\
# Problems

## Open

### P7. strip handling written twice

Status: open
Grade: stated (Breaks: no copies)

The strip code is repeated.

### P8. models imports cli

Grade: standard
"""

SECOND_LIST = """\
# Problems

## Open

### P7. strip handling written twice

Status: reopened
Grade: inferred
Carried from: problems-20261102-14h05m P7

Still repeated.

## Questions for the owner

### Q2. Is the tax rounding deliberate?

Answer:

## Dropped

### D1. rounding copies
"""


def make_project(root: Path, **lists: str) -> Project:
    """Writes a .ratchet folder with the given lists, keyed by list name, and loads it."""
    folder = root / ".ratchet"
    folder.mkdir()
    (folder / "config.toml").write_text('test_command = ["pytest"]\neditable = ["src/**"]\n',
                                        encoding="utf-8")
    (folder / "rules.md").write_text("rules\n", encoding="utf-8")
    for name, text in lists.items():
        (folder / f"{name}.md").write_text(text, encoding="utf-8")
    return load_project(root, commands=[])


def event(when: str, kind: str, by: str = "audit", **fields: str) -> ProblemEvent:
    """Returns an event."""
    return ProblemEvent(when=when, kind=kind, by=by, **fields)


def test_the_latest_list_shows_id_status_grade_and_title(tmp_path: Path) -> None:
    """Proves the newest list is shown with the ledger's status and the Grade line."""
    project = make_project(tmp_path, **{FIRST: FIRST_LIST})
    save_ledger(Ledger(problem_list=FIRST, problems={
        "P8": ProblemRecord(status=ProblemStatus.DONE)}),
        project.state_dir / FIRST / "ledger.json")
    assert show_lists(project, every_list=False).splitlines() == [
        "P7  open  stated    strip handling written twice",
        "P8  done  standard  models imports cli",
        "0 questions, 0 dropped entries",
    ]


def test_a_reopened_problem_shows_reopened(tmp_path: Path) -> None:
    """Proves an open problem whose latest event is a reopen shows as reopened."""
    project = make_project(tmp_path, **{FIRST: FIRST_LIST})
    save_ledger(Ledger(problem_list=FIRST, problems={"P7": ProblemRecord(events=[
        event("2026-11-03 10:00", "reopened", "owner")])}),
        project.state_dir / FIRST / "ledger.json")
    assert show_lists(project, every_list=False).splitlines()[0].startswith("P7  reopened")


def test_all_shows_every_list_oldest_first_with_counts(tmp_path: Path) -> None:
    """Proves --all gives a heading per list in order, and counts questions and dropped."""
    project = make_project(tmp_path, **{SECOND: SECOND_LIST, FIRST: FIRST_LIST})
    lines = show_lists(project, every_list=True).splitlines()
    assert lines[0] == f"{FIRST}  2026-11-02 14:05"
    assert lines.index(f"{SECOND}  2026-11-05 10:11") > 0
    assert lines[-1] == "1 question, 1 dropped entry"


def test_one_problem_shows_its_full_entry_from_the_newest_list_that_holds_it(
    tmp_path: Path,
) -> None:
    """Proves P8 comes from the older list and P7 from the newer one."""
    project = make_project(tmp_path, **{FIRST: FIRST_LIST, SECOND: SECOND_LIST})
    assert show_problem(project, "P7").startswith(f"{SECOND}\n\n### P7.")
    assert "Still repeated." in show_problem(project, "P7")
    assert show_problem(project, "P8").startswith(f"{FIRST}\n\n### P8.")


def test_the_history_prints_one_exact_line_per_event(tmp_path: Path) -> None:
    """Proves the history lines follow the fixed wording, across two lists."""
    project = make_project(tmp_path, **{FIRST: FIRST_LIST, SECOND: SECOND_LIST})
    save_ledger(Ledger(problem_list=FIRST, problems={"P7": ProblemRecord(events=[
        event("2026-11-02 14:05", "opened", note="audit-20261102-13h40m"),
        event("2026-11-02 16:20", "done", "J03", job="J03", commit="6918463"),
    ])}), project.state_dir / FIRST / "ledger.json")
    save_ledger(Ledger(problem_list=SECOND, problems={"P7": ProblemRecord(
        carried_from=f"{FIRST} P7", events=[
            event("2026-11-05 10:11", "reopened", "re-audit",
                  note="views/strip.py still repeats the vote")])}),
        project.state_dir / SECOND / "ledger.json")
    assert show_history(project, "P7").splitlines() == [
        "P7  strip handling written twice",
        f"2026-11-02 14:05  opened in {FIRST} (audit-20261102-13h40m)",
        "2026-11-02 16:20  J03 committed 6918463, marked done",
        '2026-11-05 10:11  reopened by a re-audit: "views/strip.py still repeats the vote"',
    ]


@pytest.mark.parametrize(("kind", "by", "fields", "expected"), [
    ("opened", "audit", {"list": "L1"}, "opened in L1"),
    ("carried", "re-audit", {"list": "L2", "note": "audit-x"}, "carried into L2 (audit-x)"),
    ("job_started", "J01", {"job": "J01"}, "J01 started"),
    ("committed", "J01", {"job": "J01", "commit": "abc"}, "J01 committed abc"),
    ("done", "J01", {"job": "J01", "commit": "abc"}, "J01 committed abc, marked done"),
    ("sent_back", "J01", {"job": "J01", "note": "tests fail"}, "J01 sent back: tests fail"),
    ("abandoned", "J01", {"job": "J01", "note": "gave up"}, "J01 abandoned: gave up"),
    ("abandoned", "re-audit", {"note": "blocked"}, "abandoned by a re-audit: blocked"),
    ("reopened", "owner", {"note": "no"}, 'reopened by the owner: "no"'),
])
def test_each_kind_has_a_fixed_wording(kind: str, by: str, fields: dict, expected: str) -> None:
    """Proves the wording of every event kind."""
    assert describe_event(event("2026-11-02 14:05", kind, by, **fields)) == expected


def test_an_unknown_id_is_refused(tmp_path: Path) -> None:
    """Proves an id that no list holds raises."""
    project = make_project(tmp_path, **{FIRST: FIRST_LIST})
    with pytest.raises(ProjectError, match="no problem list holds P99"):
        show_problem(project, "P99")


def test_a_question_id_names_its_section(tmp_path: Path) -> None:
    """Proves Q2 is refused with the section it sits in and the list."""
    project = make_project(tmp_path, **{SECOND: SECOND_LIST})
    with pytest.raises(ProjectError) as error:
        show_history(project, "Q2")
    assert str(error.value) == (
        f'Q2 is not a problem, it is under "Questions for the owner" in {SECOND}')


def test_no_lists_is_refused(tmp_path: Path) -> None:
    """Proves a project with no problem list raises."""
    project = make_project(tmp_path)
    with pytest.raises(ProjectError, match="no problem list"):
        show_lists(project, every_list=False)
