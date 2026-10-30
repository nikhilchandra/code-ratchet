"""Checks the rules that code checks after the implementer finishes, on a scratch project."""

from __future__ import annotations

from conftest import PROBLEM_LIST, PROBLEMS
from ratchet.git import Repo
from ratchet.project import Project
from ratchet.refactor.agent_answers import ImplementerAnswer, Status
from ratchet.refactor.rule_checks import check_rules, snapshot_ratchet_files


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
