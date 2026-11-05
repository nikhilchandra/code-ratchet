"""Checks that the evidence of an old list is followed into the current code."""

from __future__ import annotations

import subprocess
from pathlib import Path

from ratchet.audit.evidence_hints import build_evidence_hints, old_citations, renamed_files
from ratchet.git import Repo
from ratchet.refactor.problem_list import split_entries

OLD_LIST = """# Problems

## P1. strip handling written twice

Status: open

Evidence:

- a.py:3-4: the copy
      def f():
          return 1
- gone.py:1-1: the other copy
      x = 1
- old.py:1-1: a renamed file
      y = 2
- a.py:9-9: not there
      missing line

## P2. nothing cited

Some words.

## Questions for the owner

### Q1. Is it deliberate?

Evidence:

- a.py:3-3: not a problem
      def f():
"""


def git(path: Path, *arguments: str) -> None:
    """Runs one git command in a folder."""
    subprocess.run(["git", *arguments], cwd=path, check=True, capture_output=True)


def test_citations_are_read_with_their_quotes() -> None:
    """Proves each evidence line gives a citation with its indented quote."""
    entry = split_entries(OLD_LIST)[0]

    citations = old_citations(entry)

    assert [(c.file, c.line, c.end_line) for c in citations][:2] == [("a.py", 3, 4), ("gone.py", 1, 1)]
    assert citations[0].quote == "def f():\n    return 1"
    assert citations[0].shows == "the copy"


def test_hints_say_where_each_citation_went(tmp_path: Path) -> None:
    """Proves moved, missing, renamed and absent evidence each get their own wording."""
    (tmp_path / "a.py").write_text("# new\n\n\ndef f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "new.py").write_text("\ny = 2\n", encoding="utf-8")

    text = build_evidence_hints(tmp_path, split_entries(OLD_LIST), {"old.py": "new.py"})

    assert "## P1. strip handling written twice" in text
    assert "- a.py:3-4 is now at a.py:4-5" in text
    assert "- gone.py:1-1: the file no longer exists" in text
    assert "- old.py:1-1 is now at new.py:2-2, the file was renamed" in text
    assert "- a.py:9-9: the quote is not found in the file" in text
    assert "## P2. nothing cited\n- no evidence lines in the old list" in text
    assert "Q1" not in text


def test_unchanged_citation(tmp_path: Path) -> None:
    """Proves a quote still at its lines is reported as unchanged."""
    (tmp_path / "a.py").write_text("x\n\ndef f():\n    return 1\n", encoding="utf-8")

    text = build_evidence_hints(tmp_path, split_entries(OLD_LIST))

    assert "- a.py:3-4 is unchanged" in text


def test_renames_come_from_git(tmp_path: Path) -> None:
    """Proves a file moved after a commit is found as a rename."""
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "a@example.com")
    git(tmp_path, "config", "user.name", "a")
    git(tmp_path, "config", "core.autocrlf", "false")
    (tmp_path / "old.py").write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "start")
    git(tmp_path, "mv", "old.py", "new.py")
    git(tmp_path, "commit", "-q", "-m", "move")

    assert renamed_files(Repo(tmp_path), "HEAD~1") == {"old.py": "new.py"}
    assert renamed_files(Repo(tmp_path), "nonsense") == {}
