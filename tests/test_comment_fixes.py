"""Checks how the reviewer's comment fixes are made, and when they are refused."""

from __future__ import annotations

from ratchet.git import Repo
from ratchet.project import Project
from ratchet.refactor.agent_answers import CommentFix, Criteria, Criterion, Review, Verdict
from ratchet.refactor.comment_fixes import apply_comment_fixes


def review(*fixes: CommentFix, failing: tuple[str, ...] = ("comments",)) -> Review:
    """Returns a review that fails the named criteria and holds the given fixes."""
    criteria = {name: Criterion(passed=name not in failing, findings=[])
                for name in Criteria.model_fields}
    return Review(verdict=Verdict.ACCEPT_AFTER_COMMENT_FIXES, summary="", criteria=Criteria(**criteria),
                  plan_checks=[], comment_fixes=list(fixes), notes=[])


def fix(old: str, new: str, file: str = "src/app.py") -> CommentFix:
    """Returns one comment fix."""
    return CommentFix(file=file, old=old, new=new)


def test_comment_and_docstring_fixes_are_made(scratch: tuple[Project, Repo]) -> None:
    """Proves fixes to a comment and to a docstring are written to the file."""
    project, _ = scratch

    failure = apply_comment_fixes(project, review(
        fix("    # add them\n", "    # adds a to b\n"),
        fix('"""Returns the sum."""', '"""Returns the sum of a and b."""'),
    ))

    text = (project.root / "src" / "app.py").read_text(encoding="utf-8")
    assert failure == ""
    assert "# adds a to b" in text and "Returns the sum of a and b." in text


def test_a_fix_that_changes_code_is_refused_and_nothing_is_written(
    scratch: tuple[Project, Repo]
) -> None:
    """Proves one fix that changes code stops every fix, so the file stays as it was."""
    project, repo = scratch

    failure = apply_comment_fixes(project, review(
        fix("    # add them\n", "    # adds a to b\n"),
        fix("return a + b", "return a - b"),
    ))

    assert failure == "a fix to src/app.py changes code, not only comments and docstrings"
    assert repo.changed_paths() == []


def test_a_fix_that_breaks_the_syntax_is_refused(scratch: tuple[Project, Repo]) -> None:
    """Proves a fix that leaves the file unparseable counts as a change to code."""
    project, _ = scratch

    failure = apply_comment_fixes(project, review(fix("    # add them\n", "    add them\n")))

    assert "changes code" in failure


def test_text_that_is_not_found_once_is_refused(scratch: tuple[Project, Repo]) -> None:
    """Proves the text a fix replaces must occur exactly once."""
    project, _ = scratch

    assert "occurs 0 times" in apply_comment_fixes(project, review(fix("# missing", "# x")))
    assert "occurs 4 times" in apply_comment_fixes(project, review(fix('"""', "'''")))


def test_a_file_agents_may_not_change_is_refused(scratch: tuple[Project, Repo]) -> None:
    """Proves a fix may only touch a Python file that matches the editable patterns."""
    project, _ = scratch
    (project.root / "setup.py").write_text("# x\n", encoding="utf-8")

    failure = apply_comment_fixes(project, review(fix("# x", "# y", file="setup.py")))

    assert failure == "setup.py is not a Python file that a step may change"


def test_fixes_are_refused_when_another_criterion_failed(scratch: tuple[Project, Repo]) -> None:
    """Proves only a review whose sole failure is comments may have its fixes made by code."""
    project, _ = scratch

    failure = apply_comment_fixes(project, review(
        fix("    # add them\n", "    # adds\n"), failing=("comments", "scope", "tests")))

    assert failure == "the reviewer also failed scope, tests, which only the implementer may fix"


def test_windows_line_endings_are_kept(scratch: tuple[Project, Repo]) -> None:
    """Proves a file with Windows line endings is matched without them and written back with them."""
    project, _ = scratch
    path = project.root / "src" / "app.py"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))

    failure = apply_comment_fixes(project, review(fix("    # add them\n", "    # adds\n")))

    assert failure == ""
    assert b"    # adds\r\n" in path.read_bytes() and b"\n" not in path.read_bytes().replace(b"\r\n", b"")
