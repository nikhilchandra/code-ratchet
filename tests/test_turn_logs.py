"""Tests for reading an agent's turn log."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import ratchet.audit.turn_logs as turn_logs
from ratchet.audit.turn_logs import (
    ToolCall,
    output_commands,
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


def test_unopened_files(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    log = write_log(
        tmp_path / "turn.log",
        [
            ("Read", {"file_path": str(root / "src" / "a.py")}),
            ("Read", {"file_path": "src/b.py"}),
            ("Bash", {"command": "git blame src/c.py"}),
        ],
    )

    files = ["src/a.py", "src/b.py", "src/c.py", "src/d.py"]

    assert unopened_files(log, root, files) == ["src/d.py"]


def test_unopened_files_are_resolved_against_the_project_root(tmp_path, monkeypatch):
    """Proves a Glob pattern joined to its path field, a Read under $HOME and a Read whose path
    climbs out and back in each count as opening a file of the project at 'root'."""
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    log = write_log(
        tmp_path / "turn.log",
        [
            ("Glob", {"pattern": "a.py", "path": "src"}),
            ("Read", {"file_path": "$HOME/root/src/b.py"}),
            ("Read", {"file_path": "src/../src/c.py"}),
        ],
    )

    files = ["src/a.py", "src/b.py", "src/c.py", "src/d.py"]

    assert unopened_files(log, root, files) == ["src/d.py"]


def test_git_commands_that_pass_output_are_found(tmp_path):
    writing = [
        "git log -1 --output=C:/x/.ratchet/rules.md",
        "git diff --output out.txt",
        "git show --outp=y.txt",
    ]
    log = write_log(
        tmp_path / "turn.log",
        [
            ("Bash", {"command": "git log --oneline"}),
            ("Bash", {"command": writing[0]}),
            ("Read", {"file_path": "--output=a.txt"}),
            ("Bash", {"command": "git diff --stat"}),
            ("Bash", {"command": writing[1]}),
            ("Bash", {"command": "git diff --ours"}),
            ("Bash", {"command": writing[2]}),
        ],
    )

    assert output_commands(log) == writing


def test_a_command_with_unbalanced_quotes_that_passes_output_is_found(tmp_path):
    command = 'git log --output=out.txt --format="%h'
    log = write_log(tmp_path / "turn.log", [("Bash", {"command": command})])

    assert output_commands(log) == [command]


# The text that Claude Code gives as the result of a call that its permission rules refuse.
CLI_DENIAL = ("Permission to use Bash has been denied because Claude Code is running in don't ask "
              "mode. Only use tools that are already allowed.")
# The reason that rooster's hook gives for a Bash command that uses a forbidden option.
HOOK_DENIAL = "the option --no-index is forbidden here, because it reaches outside the project"

# The git options that reach outside the project.
OPTIONS = ("--no-index", "--contents", "--output", "-C", "--git-dir", "--work-tree", "-c",
           "--exec-path")


def write_results_log(path: Path, calls: list[tuple[str, dict, dict | None]]) -> Path:
    """Writes a turn log in which each call is an assistant message with one tool use block,
    followed by a user message with the call's tool result block when the call has one.

    Each call is a tool's name, its input and its result, which is a dict of the result block's
    'content' and 'is_error' or None for a call with no result.
    """
    lines = ["--- messages ---"]
    for number, (name, arguments, result) in enumerate(calls):
        use = {"id": f"toolu_{number}", "name": name, "input": arguments}
        lines.append(json.dumps({"type": "AssistantMessage", "content": [use]}))
        if result is not None:
            block = {"tool_use_id": f"toolu_{number}"} | result
            lines.append(json.dumps({"type": "UserMessage", "content": [block]}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_a_call_with_a_permission_denial_result_is_marked_denied(tmp_path):
    log = write_results_log(
        tmp_path / "turn.log",
        [
            ("Bash", {"command": "cat .ratchet/p.md"}, {"content": CLI_DENIAL, "is_error": True}),
            ("Bash", {"command": "git diff --no-index a b"},
             {"content": [{"type": "text", "text": HOOK_DENIAL}], "is_error": True}),
            ("Bash", {"command": "git show nope"},
             {"content": "fatal: invalid object name 'nope'.", "is_error": True}),
            ("Read", {"file_path": "src/a.py"}, {"content": "1\tx = 1", "is_error": False}),
            ("Read", {"file_path": "src/b.py"}, None),
        ],
    )

    calls = read_tool_calls(log)

    assert [call.denied for call in calls] == [True, True, False, False, False]
    assert [call.name for call in calls] == ["Bash", "Bash", "Bash", "Read", "Read"]


def test_forbidden_calls_find_ratchet_paths_and_forbidden_options_only(tmp_path):
    found = [
        ("Read", {"file_path": r"C:\work\proj\.ratchet\problems.md"}),
        ("Read", {"file_path": "/home/me/proj/.ratchet/problems.md"}),
        ("Glob", {"pattern": ".ratchet/**"}),
        ("Grep", {"pattern": "P1", "path": ".ratchet"}),
        ("Grep", {"pattern": "P1", "glob": ".ratchet/*.md"}),
        ("Bash", {"command": "cat .ratchet/problems.md"}),
        ("Bash", {"command": "git show HEAD:.ratchet/x"}),
        ("Bash", {"command": "git diff --no-index a b"}),
        ("Bash", {"command": "git log --outp=x"}),
        ("Bash", {"command": "git -C .. log"}),
    ]
    clean = [
        ("Grep", {"pattern": "\\.ratchet", "path": "src"}),
        ("Bash", {"command": 'git log -S "/api/v1"'}),
        ("Bash", {"command": "git log --oneline"}),
        ("Read", {"file_path": "not.ratchet.txt"}),
    ]
    # mix the clean calls in among the found ones, whose order must be kept
    calls = clean[:2] + found[:5] + clean[2:] + found[5:]
    log = write_results_log(tmp_path / "turn.log", [(name, args, None) for name, args in calls])

    forbidden = turn_logs.forbidden_calls(log, OPTIONS, tmp_path)

    assert [(call.name, call.arguments) for call in forbidden] == found
    assert not any(call.denied for call in forbidden)


def test_forbidden_calls_keep_the_denied_flag(tmp_path):
    log = write_results_log(
        tmp_path / "turn.log",
        [
            ("Read", {"file_path": ".ratchet/problems.md"},
             {"content": "Permission to use Read has been denied.", "is_error": True}),
            ("Bash", {"command": "cat .ratchet/problems.md"},
             {"content": "# Problems", "is_error": False}),
        ],
    )

    forbidden = turn_logs.forbidden_calls(log, OPTIONS, tmp_path)

    assert [(call.name, call.denied) for call in forbidden] == [("Read", True), ("Bash", False)]


def test_read_tool_calls_skips_content_blocks_that_are_not_objects(tmp_path):
    log = tmp_path / "turn.log"
    message = {"type": "AssistantMessage",
               "content": ["plain text", {"name": "Read", "input": {"file_path": "a.py"}}]}
    log.write_text("--- messages ---\n" + json.dumps(message) + "\n", encoding="utf-8")

    assert read_tool_calls(log) == [ToolCall("Read", {"file_path": "a.py"})]


def short_name(path: Path) -> str:
    """Returns the Windows 8.3 short form of an existing path, such as C:\\X\\RATCHE~1 for
    C:\\X\\.ratchet, or "" when Windows gives none."""
    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    return buffer.value


def found_by(tmp_path: Path, root: Path, name: str, arguments: dict) -> bool:
    """Writes a turn log with one allowed tool call and tells whether forbidden_calls finds it,
    for a project at 'root'."""
    log = write_results_log(tmp_path / "turn.log", [(name, arguments, None)])
    return turn_logs.forbidden_calls(log, OPTIONS, root) != []


@pytest.mark.skipif(os.name != "nt", reason="8.3 short names exist only on Windows")
def test_forbidden_calls_find_an_allowed_read_of_a_short_name_path_into_ratchet(tmp_path):
    folder = tmp_path / ".ratchet"
    folder.mkdir()
    (folder / "problems.md").write_text("# Problems", encoding="utf-8")
    short = short_name(folder)
    if not short or Path(short).name == ".ratchet":
        pytest.skip("this volume makes no short name for .ratchet")

    assert found_by(tmp_path, tmp_path, "Read", {"file_path": f"{Path(short).name}/problems.md"})
    assert found_by(tmp_path, tmp_path, "Read", {"file_path": f"{short}\\problems.md"})


def test_forbidden_calls_find_allowed_file_tool_calls_that_land_outside_or_in_git(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / ".git").mkdir()
    (tmp_path / "x.md").write_text("x", encoding="utf-8")
    found = [
        ("Grep", {"pattern": "P1", "path": ".."}),
        ("Read", {"file_path": str(tmp_path / "x.md")}),
        ("Glob", {"pattern": f"{tmp_path.as_posix()}/**/*.md"}),
        ("Read", {"file_path": ".git/config"}),
        ("Read", {"file_path": "src/../../x"}),
    ]
    clean = [
        ("Read", {"file_path": "src/a.py"}),
        ("Grep", {"pattern": "P1"}),
        ("Glob", {"pattern": "**/*.py"}),
        ("Read", {"file_path": "not.ratchet.txt"}),
    ]

    for name, arguments in found:
        assert found_by(tmp_path, root, name, arguments), (name, arguments)
    for name, arguments in clean:
        assert not found_by(tmp_path, root, name, arguments), (name, arguments)


@pytest.mark.parametrize(
    ("content", "is_error", "denied"),
    [
        (CLI_DENIAL, True, True),
        ([{"type": "text", "text": HOOK_DENIAL}], True, True),
        ("the path ../x is outside C:/work/proj, so it is refused", True, True),
        ("<tool_use_error>File is in a directory that is denied by your permission settings."
         "</tool_use_error>", True, True),
        ("<tool_use_error>File is covered by a Read deny rule in your permission settings and "
         "cannot be read.</tool_use_error>", True, True),
        ("Hook PreToolUse:Read denied this tool", True, True),
        ("Error: the option -C is forbidden here, because it reaches outside the project",
         True, True),
        ("fatal: path has been denied this tool", True, False),
        ("grep: x: Permission to use was refused", True, False),
        ("grep: PreToolUse:Read hook error: x", True, False),
        (CLI_DENIAL, False, False),
    ],
)
def test_only_a_result_that_opens_with_denial_wording_is_a_denial(
    tmp_path, content, is_error, denied
):
    log = write_results_log(
        tmp_path / "turn.log",
        [("Read", {"file_path": "../x"}, {"content": content, "is_error": is_error})],
    )

    assert [call.denied for call in read_tool_calls(log)] == [denied]


# The text that the CLI logs for a Bash call that rooster's hook refused for a forbidden option.
BASH_HOOK_ERROR = ("PreToolUse:Bash hook error: the option --no-index is forbidden here, because "
                   "it reaches outside the project")
# The text that the CLI logs for a Read call that rooster's hook refused for its path.
READ_HOOK_ERROR = "PreToolUse:Read hook error: the path ../x is outside C:/p, so it is refused"


@pytest.mark.parametrize(
    ("name", "arguments", "text"),
    [
        ("Bash", {"command": "git diff --no-index a b"}, BASH_HOOK_ERROR),
        ("Read", {"file_path": "../x"}, READ_HOOK_ERROR),
    ],
)
@pytest.mark.parametrize("as_blocks", [False, True])
def test_a_hook_refusal_logged_by_the_cli_is_a_denial(
    tmp_path, name, arguments, text, as_blocks
):
    """Proves a call whose error result opens with the CLI's 'PreToolUse:<Tool> hook error: '
    prefix, given as a plain string or as a list of text blocks, is marked denied."""
    content = [{"type": "text", "text": text}] if as_blocks else text
    log = write_results_log(
        tmp_path / "turn.log", [(name, arguments, {"content": content, "is_error": True})]
    )

    assert [call.denied for call in read_tool_calls(log)] == [True]


def test_forbidden_calls_find_an_allowed_read_of_a_hidden_path(tmp_path):
    """Proves an allowed Read of a plain hidden path, and of a file below a 'folder/**' hidden
    pattern, is found when the hidden paths are given, and that a Read of any other file is not.
    With no hidden paths given, the Read of .env is not found."""
    root = tmp_path / "proj"
    root.mkdir()
    hidden = (".env", ".venv", ".venv/**")
    calls = [
        ("Read", {"file_path": ".env"}, None),
        ("Read", {"file_path": ".venv/lib/x.py"}, None),
        ("Read", {"file_path": "src/a.py"}, None),
    ]
    log = write_results_log(tmp_path / "turn.log", calls)

    forbidden = turn_logs.forbidden_calls(log, (), root, hidden)

    assert [(call.arguments, call.denied) for call in forbidden] == [
        ({"file_path": ".env"}, False),
        ({"file_path": ".venv/lib/x.py"}, False),
    ]
    assert turn_logs.forbidden_calls(log, (), root) == []
