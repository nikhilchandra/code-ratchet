"""Checks the walk over a unified diff that names each hunk line with its file paths and its line
numbers in the old and the new file."""

from __future__ import annotations

from ratchet.unified_diff import DiffLine, diff_lines

# A diff that changes src/a.py with context lines, removed lines and added lines.
MODIFY = """\
diff --git a/src/a.py b/src/a.py
index 1111111..2222222 100644
--- a/src/a.py
+++ b/src/a.py
@@ -3,4 +3,5 @@ def f():
 x = 1
-y = 2
+y = 3
+z = 4
 w = 5
-v = 6
+v = 7
"""

# A diff that adds src/new.py and deletes src/old.py.
NEW_AND_DELETED = """\
diff --git a/src/new.py b/src/new.py
new file mode 100644
--- /dev/null
+++ b/src/new.py
@@ -0,0 +1,2 @@
+A = 1
+B = 2
diff --git a/src/old.py b/src/old.py
deleted file mode 100644
--- a/src/old.py
+++ /dev/null
@@ -1,2 +0,0 @@
-C = 1
-D = 2
"""


def rows(diff: str) -> list[tuple]:
    """Returns each line that diff_lines() yields for 'diff', as a tuple of its fields."""
    return [(line.old_path, line.new_path, line.old_line, line.new_line, line.marker, line.text)
            for line in diff_lines(diff)]


def test_a_modify_hunk_gives_each_line_its_old_and_new_numbers() -> None:
    """Proves a context line counts in both files, a removed line only in the old file and an
    added line only in the new file, and that the a/ and b/ prefixes are stripped."""
    assert rows(MODIFY) == [
        ("src/a.py", "src/a.py", 3, 3, " ", " x = 1"),
        ("src/a.py", "src/a.py", 4, 4, "-", "-y = 2"),
        ("src/a.py", "src/a.py", 5, 4, "+", "+y = 3"),
        ("src/a.py", "src/a.py", 5, 5, "+", "+z = 4"),
        ("src/a.py", "src/a.py", 5, 6, " ", " w = 5"),
        ("src/a.py", "src/a.py", 6, 7, "-", "-v = 6"),
        ("src/a.py", "src/a.py", 7, 7, "+", "+v = 7"),
    ]


def test_each_line_is_a_diff_line() -> None:
    """Proves the walk yields DiffLine values."""
    assert all(isinstance(line, DiffLine) for line in diff_lines(MODIFY))


def test_a_new_file_has_no_old_path_and_a_deleted_file_has_no_new_path() -> None:
    """Proves /dev/null reads as None on either side, and that two files in one diff are each
    named on their own lines."""
    assert rows(NEW_AND_DELETED) == [
        (None, "src/new.py", 0, 1, "+", "+A = 1"),
        (None, "src/new.py", 0, 2, "+", "+B = 2"),
        ("src/old.py", None, 1, 0, "-", "-C = 1"),
        ("src/old.py", None, 2, 0, "-", "-D = 2"),
    ]


def test_a_no_newline_note_is_skipped() -> None:
    """Proves the '\\ No newline at end of file' note is not yielded and does not move the line
    numbers of the lines after it."""
    diff = ("--- a/src/a.py\n+++ b/src/a.py\n@@ -1,2 +1,2 @@\n x = 1\n-y = 2\n"
            "\\ No newline at end of file\n+y = 3\n\\ No newline at end of file\n")

    assert rows(diff) == [
        ("src/a.py", "src/a.py", 1, 1, " ", " x = 1"),
        ("src/a.py", "src/a.py", 2, 2, "-", "-y = 2"),
        ("src/a.py", "src/a.py", 3, 2, "+", "+y = 3"),
    ]


def test_header_lines_are_never_yielded_as_content() -> None:
    """Proves the '---' and '+++' file headers are not yielded, while a removed line whose text
    starts with '--' and an added line whose text starts with '++' are."""
    diff = ("--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n--- old\n+++ new\n"
            "--- a/src/b.py\n+++ b/src/b.py\n@@ -1 +1 @@\n-p\n+q\n")

    assert rows(diff) == [
        ("src/a.py", "src/a.py", 1, 1, "-", "--- old"),
        ("src/a.py", "src/a.py", 2, 1, "+", "+++ new"),
        ("src/b.py", "src/b.py", 1, 1, "-", "-p"),
        ("src/b.py", "src/b.py", 2, 1, "+", "+q"),
    ]


def test_a_path_after_a_tab_is_cut_and_an_unprefixed_path_is_kept() -> None:
    """Proves a header's path ends at a tab, and that a path with no a/ or b/ prefix is kept as
    it is."""
    diff = "--- src/a.py\t2026-01-01\n+++ src/a.py\t2026-01-02\n@@ -1 +1 @@\n-p\n+q\n"

    assert [(line.old_path, line.new_path) for line in diff_lines(diff)] == [
        ("src/a.py", "src/a.py"), ("src/a.py", "src/a.py")]


def test_an_empty_line_inside_a_hunk_counts_as_context() -> None:
    """Proves an empty line inside a hunk, which git writes in place of ' ' under
    diff.suppressBlankEmpty, counts in both files, so the lines after it keep the right numbers."""
    diff = "--- a/src/a.py\n+++ b/src/a.py\n@@ -1,3 +1,4 @@\n x = 1\n\n+y = 2\n z = 3\n"

    added = [line for line in diff_lines(diff) if line.marker == "+"]
    after = [line for line in diff_lines(diff) if line.text == " z = 3"]

    assert [(line.old_line, line.new_line) for line in added] == [(3, 3)]
    assert [(line.old_line, line.new_line) for line in after] == [(3, 4)]


def test_an_empty_diff_yields_nothing() -> None:
    """Proves an empty diff gives no lines."""
    assert list(diff_lines("")) == []
