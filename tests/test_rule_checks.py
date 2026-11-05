"""Checks the rules that code checks after the implementer finishes, on a scratch project."""

from __future__ import annotations

from dataclasses import replace

from conftest import PROBLEM_LIST, PROBLEMS
from ratchet.git import Repo
from ratchet.project import Project
from ratchet.refactor.agent_answers import ImplementerAnswer, Status
from ratchet.refactor.rule_checks import check_rules, find_violations, snapshot_ratchet_files


def answer(status: Status = Status.DONE) -> ImplementerAnswer:
    """Returns an implementer's answer with the given status."""
    return ImplementerAnswer(status=status, summary="", deviations=[], tests_changed=[],
                             blocked_reason="")


def test_an_allowed_change_breaks_no_rule(scratch: tuple[Project, Repo]) -> None:
    """Proves an edit to an editable file passes."""
    project, repo = scratch
    inputs = snapshot_ratchet_files(project)
    (project.root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")

    assert check_rules(project, repo, answer(), inputs) == ""


def test_a_file_outside_editable_and_a_frozen_file_are_named(
    scratch: tuple[Project, Repo]
) -> None:
    """Proves a change outside the editable patterns and one to a frozen file are both reported."""
    project, repo = scratch
    inputs = snapshot_ratchet_files(project)
    (project.root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (project.root / "tests" / "login.feature").write_text("Feature: x\n", encoding="utf-8")

    report = check_rules(project, repo, answer(), inputs)

    assert report.startswith("# The step broke a rule that the script checks")
    assert "- pyproject.toml is not among the files that a step may change" in report
    assert "- tests/login.feature is not among the files that a step may change" in report


def test_a_changed_ratchet_file_is_put_back_and_named(scratch: tuple[Project, Repo]) -> None:
    """Proves a change to the problem list and the deletion of rules.md are undone and reported."""
    project, repo = scratch
    inputs = snapshot_ratchet_files(project)
    (project.folder / PROBLEMS).write_text("## P1. done, trust me\n", encoding="utf-8")
    (project.folder / "rules.md").unlink()
    (project.root / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")

    report = check_rules(project, repo, answer(), inputs)

    assert f".ratchet/{PROBLEMS} was changed, and has been put back" in report
    assert ".ratchet/rules.md was changed, and has been put back" in report
    assert (project.folder / PROBLEMS).read_text(encoding="utf-8") == PROBLEM_LIST
    assert (project.folder / "rules.md").is_file()


def test_an_added_skip_is_named_even_in_a_new_test_file(scratch: tuple[Project, Repo]) -> None:
    """Proves a skip added to an existing test and an expected failure in a new test file are
    both reported."""
    project, repo = scratch
    inputs = snapshot_ratchet_files(project)
    test_app = project.root / "tests" / "test_app.py"
    test_app.write_text("import pytest\n\n\n@pytest.mark.skip\n" + test_app.read_text(), encoding="utf-8")
    (project.root / "tests" / "test_new.py").write_text(
        "import pytest\n\n\n@pytest.mark.xfail\ndef test_x():\n    pass\n", encoding="utf-8")

    report = check_rules(project, repo, answer(), inputs)

    assert "adds a skip or an expected failure: @pytest.mark.skip" in report
    assert "adds a skip or an expected failure: @pytest.mark.xfail" in report


def test_a_skip_word_outside_tests_is_not_a_rule(scratch: tuple[Project, Repo]) -> None:
    """Proves the word skip in source code is not taken for a skipped test."""
    project, repo = scratch
    inputs = snapshot_ratchet_files(project)
    (project.root / "src" / "app.py").write_text("def skip():\n    pass\n", encoding="utf-8")

    assert check_rules(project, repo, answer(), inputs) == ""


def test_done_with_nothing_changed_is_named(scratch: tuple[Project, Repo]) -> None:
    """Proves an implementer that claims to be done without a change is caught, while a blocked
    one with no change is not."""
    project, repo = scratch
    inputs = snapshot_ratchet_files(project)

    assert "says that it is done, but nothing changed" in check_rules(project, repo, answer(), inputs)
    assert check_rules(project, repo, answer(Status.BLOCKED), inputs) == ""


RULES = """\
```toml
[parts]
core = "app.core"
cli = "app.cli"

[may_use]
core = []
cli = ["core"]
```
"""


def design_project(scratch: tuple[Project, Repo], core_text: str) -> tuple[Project, Repo]:
    """Writes a two-part package whose core module has the given text into the scratch project, and
    returns the project with a rules.md that has a target design."""
    project, repo = scratch
    for name, text in {
        "src/app/__init__.py": "",
        "src/app/core/__init__.py": "",
        "src/app/core/model.py": core_text,
        "src/app/cli/__init__.py": "",
        "src/app/cli/main.py": "from app.core import model\n",
    }.items():
        path = project.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return replace(project, rules=RULES), repo


def test_a_new_violation_and_a_new_cycle_are_named(scratch: tuple[Project, Repo]) -> None:
    """Proves an added import from core into cli is reported as a violation and as a cycle."""
    project, repo = design_project(scratch, "")
    before = find_violations(project)
    (project.root / "src/app/core/model.py").write_text("from app.cli import main\n", encoding="utf-8")

    report = check_rules(project, repo, answer(Status.BLOCKED), {}, before)

    assert not any(name.startswith(("may_use", "cycle")) for name in before)
    assert "- the step adds a violation, may_use: src/app/core/model.py: from app.cli import main" in report
    assert "- the step adds a violation, cycle: app.cli.main and app.core.model" in report


def test_an_old_violation_that_moves_is_not_new(scratch: tuple[Project, Repo]) -> None:
    """Proves a violation that was there before the step passes, even on a different line."""
    project, repo = design_project(scratch, "from app.cli import main\n")
    before = find_violations(project)
    (project.root / "src/app/core/model.py").write_text(
        "x = 1\nfrom app.cli import main\n", encoding="utf-8")

    assert check_rules(project, repo, answer(Status.BLOCKED), {}, before) == ""
    assert check_rules(project, repo, answer(Status.BLOCKED), {}) == ""


def test_a_step_that_shrinks_a_cycle_adds_no_violation(scratch: tuple[Project, Repo]) -> None:
    """Proves a step that takes one module out of a three-module cycle passes."""
    project, repo = design_project(scratch, "")
    for name, text in {"a": "from app.core import b\n", "b": "from app.core import c\n",
                       "c": "from app.core import a\n"}.items():
        (project.root / f"src/app/core/{name}.py").write_text(text, encoding="utf-8")
    before = find_violations(project)
    (project.root / "src/app/core/c.py").write_text("from app.core import b\n", encoding="utf-8")

    assert any(name.startswith("cycle") for name in before)
    assert check_rules(project, repo, answer(Status.BLOCKED), {}, before) == ""


def test_editing_the_first_line_of_a_module_in_no_part_is_not_new(scratch: tuple[Project, Repo]) -> None:
    """Proves a module that was already outside every part may have its first line edited."""
    project, repo = design_project(scratch, "")
    extra = project.root / "src/app/extra.py"
    extra.write_text('"""Old."""\n', encoding="utf-8")
    before = find_violations(project)
    extra.write_text('"""New."""\n', encoding="utf-8")

    assert "no_part: app.extra" in before
    assert check_rules(project, repo, answer(Status.BLOCKED), {}, before) == ""
