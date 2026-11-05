"""Checks that citations are verified against the code and that drifted lines are fixed."""

from __future__ import annotations

from pathlib import Path

from ratchet.audit.agent_answers import Citation
from ratchet.audit.citations import (
    MOVED,
    NO_FILE,
    NO_QUOTE,
    NOT_FOUND,
    OK,
    check_citation,
    check_citations,
    render_report,
)

SOURCE = "a = 1\n\ndef f():\n    return a\n\n\ndef g():\n    return a\n"


def cite(file: str, line: int, end_line: int, quote: str) -> Citation:
    """Builds a citation with a fixed explanation."""
    return Citation(file=file, line=line, end_line=end_line, quote=quote, shows="x")


def make_root(tmp_path: Path) -> Path:
    """Writes one source file under tmp_path and returns the root."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "m.py").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "outside.py").write_text("a = 1\n", encoding="utf-8")
    return root


def test_exact_citation_is_ok(tmp_path: Path) -> None:
    """Proves a quote at its cited line passes unchanged."""
    citation = cite("m.py", 3, 3, "def f():")

    result = check_citation(make_root(tmp_path), citation)

    assert (result.status, result.fixed) == (OK, citation)


def test_drifted_lines_are_fixed(tmp_path: Path) -> None:
    """Proves a quote found at other lines gives the corrected citation."""
    result = check_citation(make_root(tmp_path), cite("m.py", 1, 1, "def g():"))

    assert result.status == MOVED
    assert (result.fixed.line, result.fixed.end_line) == (7, 7)


def test_nearest_repeat_wins(tmp_path: Path) -> None:
    """Proves a quote that appears twice is fixed to the closer place."""
    result = check_citation(make_root(tmp_path), cite("m.py", 7, 7, "return a"))

    assert result.status == MOVED
    assert result.fixed.line == 8


def test_multi_line_quote_ignores_indentation(tmp_path: Path) -> None:
    """Proves a quote over several lines matches despite whitespace differences."""
    result = check_citation(make_root(tmp_path), cite("m.py", 3, 4, "def f():\nreturn a"))

    assert result.status == OK


def test_wrong_end_line_is_fixed(tmp_path: Path) -> None:
    """Proves a right start line with a wrong end line is corrected."""
    result = check_citation(make_root(tmp_path), cite("m.py", 3, 9, "def f():"))

    assert (result.status, result.fixed.end_line) == (MOVED, 3)


def test_untrustworthy_citations(tmp_path: Path) -> None:
    """Proves a missing quote, file or text each give a status with no fix."""
    root = make_root(tmp_path)

    assert check_citation(root, cite("m.py", 1, 1, "nope")).status == NOT_FOUND
    assert check_citation(root, cite("gone.py", 1, 1, "a = 1")).status == NO_FILE
    assert check_citation(root, cite("../outside.py", 1, 1, "a = 1")).status == NO_FILE
    assert check_citation(root, cite("m.py", 1, 1, "  ")).status == NO_QUOTE
    assert check_citation(root, cite("m.py", 1, 1, "nope")).fixed is None


def test_report_lists_only_citations_that_need_attention(tmp_path: Path) -> None:
    """Proves the report names moved and failed citations and skips fine ones."""
    results = check_citations(
        make_root(tmp_path),
        [cite("m.py", 3, 3, "def f():"), cite("m.py", 1, 1, "def g():"), cite("m.py", 1, 1, "nope")],
    )

    report = render_report(results)

    assert "3 citations, 2 not fine as given." in report
    assert "m.py:1-1 moved to lines 7-7" in report
    assert "m.py:1-1 not found" in report
    assert "m.py:3-3" not in report
