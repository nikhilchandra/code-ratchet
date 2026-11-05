"""Tests for reading an agent's turn log."""

from __future__ import annotations

import json
from pathlib import Path

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
