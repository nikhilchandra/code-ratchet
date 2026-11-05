"""Tests for reading an agent's turn log."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ratchet.audit.turn_logs import (
    ToolCall,
    absolute_paths_in,
    paths_outside,
    read_tool_calls,
    unopened_files,
)


def write_log(path: Path, calls: list[tuple[str, dict]]) -> Path:
    """Writes a turn log that holds one assistant message with the given tool calls."""
    blocks = [{"id": f"t{i}", "name": name, "input": args} for i, (name, args) in enumerate(calls)]
    messages = [
        {"type": "AssistantMessage", "content": [{"text": "looking"}, *blocks]},
        {"type": "ResultMessage", "result": "done"},
    ]
    lines = ["--- options ---", "{}", "", "--- prompt ---", "hello", "", "--- messages ---"]
    lines += [json.dumps(message) for message in messages]
    lines += ["", "--- stderr ---", '{"name": "Read", "input": {"file_path": "/etc/passwd"}}']
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_read_tool_calls_keeps_order_and_skips_other_sections(tmp_path):
    log = write_log(
        tmp_path / "turn.log",
        [("Read", {"file_path": "a.py"}), ("Bash", {"command": "git log"})],
    )

    assert read_tool_calls(log) == [
        ToolCall("Read", {"file_path": "a.py"}),
        ToolCall("Bash", {"command": "git log"}),
    ]


def test_read_tool_calls_skips_lines_that_are_not_json(tmp_path):
    log = tmp_path / "turn.log"
    log.write_text("--- messages ---\n{broken\nplain text\n", encoding="utf-8")

    assert read_tool_calls(log) == []


def test_paths_inside_the_copy_are_clean(tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()
    log = write_log(
        tmp_path / "turn.log",
        [
            ("Read", {"file_path": str(copy / "a.py")}),
            ("Read", {"file_path": "src/b.py"}),
            ("Grep", {"pattern": "/not/a/path", "path": "."}),
            ("Bash", {"command": f"cat {copy}/a.py > /dev/null"}),
        ],
    )

    assert paths_outside(log, copy) == []


def test_paths_outside_the_copy_are_listed_once(tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()
    other = tmp_path / "project" / ".ratchet"
    log = write_log(
        tmp_path / "turn.log",
        [
            ("Read", {"file_path": str(other / "problems.md")}),
            ("Read", {"file_path": str(other / "problems.md")}),
            ("Read", {"file_path": "../project/x.py"}),
            ("Glob", {"pattern": str(other / "*.md")}),
            ("Bash", {"command": f"ls '{other}'"}),
        ],
    )

    outside = paths_outside(log, copy)

    assert outside[0] == str(other / "problems.md")
    assert "../project/x.py" in outside
    assert str(other / "*.md") in outside
    assert str(other) in outside
    assert outside.count(str(other / "problems.md")) == 1


def test_absolute_paths_in_a_command():
    assert absolute_paths_in("cat /home/me/a.py | grep x") == ["/home/me/a.py"]
    assert absolute_paths_in(r"type C:\Users\me\a.py") == [r"C:\Users\me\a.py"]
    assert absolute_paths_in("git log --format=%h/%s src/a.py 2>/dev/null") == []
    assert absolute_paths_in('git grep -n "https://example.com" src') == []


def test_unopened_files(tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()
    log = write_log(
        tmp_path / "turn.log",
        [
            ("Read", {"file_path": str(copy / "src" / "a.py")}),
            ("Read", {"file_path": "src/b.py"}),
            ("Bash", {"command": "git blame src/c.py"}),
        ],
    )

    files = ["src/a.py", "src/b.py", "src/c.py", "src/d.py"]

    assert unopened_files(log, copy, files) == ["src/d.py"]


def outside_of(tmp_path: Path, copy: Path, name: str, arguments: dict) -> list[str]:
    """Writes a turn log with one tool call and lists the paths outside the copy that it names."""
    log = write_log(tmp_path / "turn.log", [(name, arguments)])
    return paths_outside(log, copy)


def git_bash_form(path: Path) -> str:
    """Writes a Windows path the way Git Bash names it, such as /c/Users/me for C:/Users/me."""
    drive, rest = os.path.splitdrive(str(path))
    return "/" + drive[0].lower() + rest.replace("\\", "/")


def test_a_relative_path_that_climbs_out_and_a_home_path_are_found_in_a_command():
    command = "git diff --no-index ../../../../code/x/.ratchet/problems-x.md /dev/null"

    assert absolute_paths_in(command) == ["../../../../code/x/.ratchet/problems-x.md"]
    assert absolute_paths_in("cat ~/code/x/.ratchet/p.md") == ["~/code/x/.ratchet/p.md"]


def test_bash_reads_through_dotdot_and_home_are_outside(tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()
    log = write_log(
        tmp_path / "turn.log",
        [
            ("Bash", {"command": "git diff --no-index ../../outside/p.md /dev/null"}),
            ("Bash", {"command": "cat ~/code/x/p.md"}),
        ],
    )

    outside = paths_outside(log, copy)

    assert "../../outside/p.md" in outside
    assert "~/code/x/p.md" in outside


def test_a_grep_path_of_home_is_outside(tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()

    assert outside_of(tmp_path, copy, "Grep", {"pattern": "problems", "path": "~"}) != []


def test_glob_and_grep_patterns_that_climb_out_are_outside(tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()

    assert outside_of(tmp_path, copy, "Glob", {"pattern": "../../*.md"}) != []
    assert outside_of(tmp_path, copy, "Grep", {"pattern": "x", "glob": "../other/**"}) != []
    assert outside_of(tmp_path, copy, "Grep", {"pattern": "x", "path": ".."}) != []


def test_search_strings_are_not_paths(tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()

    assert absolute_paths_in('grep -rn "/api/" src') == []
    assert outside_of(tmp_path, copy, "Bash", {"command": 'grep -rn "/api/" src'}) == []
    assert outside_of(tmp_path, copy, "Grep", {"pattern": "/api/"}) == []


@pytest.mark.skipif(os.name != "nt", reason="Git Bash paths name Windows drives")
def test_a_git_bash_path_inside_the_copy_is_clean(tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()
    inside = git_bash_form(copy / "a.py")
    other = git_bash_form(tmp_path / "project" / "p.md")

    assert outside_of(tmp_path, copy, "Bash", {"command": f"cat {inside}"}) == []
    assert outside_of(tmp_path, copy, "Bash", {"command": f"cat {other}"}) == [other]


def test_a_dotdot_that_stays_inside_the_copy_is_clean(tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()

    assert outside_of(tmp_path, copy, "Bash", {"command": "cat src/../a.py"}) == []
    assert outside_of(tmp_path, copy, "Glob", {"pattern": "src/../*.py"}) == []
    # one '..' more climbs out of the copy, so the check must count the '..' segments
    assert outside_of(tmp_path, copy, "Bash", {"command": "cat src/../../a.py"}) != []
    assert outside_of(tmp_path, copy, "Glob", {"pattern": "src/../../*.py"}) != []


def test_a_command_with_unbalanced_quotes_is_still_searched_for_paths():
    assert absolute_paths_in('cat "/home/me/a.py" "oops') == ["/home/me/a.py"]


def test_a_pattern_is_taken_relative_to_the_path_of_its_call(tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()

    assert outside_of(tmp_path, copy, "Glob", {"pattern": "*.py", "path": "src"}) == []
    assert outside_of(tmp_path, copy, "Glob", {"pattern": "../*.py", "path": "src"}) == []
    assert outside_of(tmp_path, copy, "Glob", {"pattern": "../../*.py", "path": "src"}) != []
    assert outside_of(tmp_path, copy, "Grep", {"pattern": "x", "path": "src", "glob": "../../*.py"}) != []
