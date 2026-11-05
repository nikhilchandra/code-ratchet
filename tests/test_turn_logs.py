"""Tests for reading an agent's turn log."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import ratchet.audit.turn_logs as turn_logs
from ratchet.audit.turn_logs import (
    ToolCall,
    absolute_paths_in,
    output_commands,
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
    # a one-segment search string that names nothing on disk is not a path
    assert absolute_paths_in('grep -rn "/apix" src') == []
    assert outside_of(tmp_path, copy, "Bash", {"command": 'grep -rn "/apix" src'}) == []
    assert outside_of(tmp_path, copy, "Bash", {"command": 'grep -rn "/api/" src'}) == []
    assert outside_of(tmp_path, copy, "Grep", {"pattern": "/api/"}) == []


def test_cd_to_the_root_or_a_root_folder_is_a_path():
    assert absolute_paths_in("cd / && cat c/Users/me/code/x/.ratchet/p.md") == ["/"]
    # the target of cd counts whether or not /home exists on this machine
    assert absolute_paths_in("cd /home && cat me/x/.ratchet/p.md") == ["/home"]
    assert absolute_paths_in("pushd /home && cat a.md") == ["/home"]


def test_a_one_segment_absolute_path_that_exists_is_a_path():
    # the first folder below the root of the current folder, such as /Users on Windows
    segment = "/" + Path.cwd().resolve().parts[1]

    assert absolute_paths_in(f"git diff --no-index {segment} /dev/null") == [segment]


def test_bash_cd_to_the_root_is_outside(tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()
    cd_root = "cd / && cat c/Users/me/x/.ratchet/p.md"
    cd_home = "cd /home && cat me/x/.ratchet/p.md"

    assert outside_of(tmp_path, copy, "Bash", {"command": cd_root}) == ["/"]
    assert outside_of(tmp_path, copy, "Bash", {"command": cd_home}) == ["/home"]


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


# The ways a shell command can name the home folder through a variable.
HOME_VARIABLES = ("$HOME", "${HOME}", "$USERPROFILE", "${USERPROFILE}", "%USERPROFILE%", "%userprofile%")


def test_home_variable_paths_are_found_in_a_command():
    command = "git diff --no-index $HOME/code/x/.ratchet/problems-x.md /dev/null"

    assert absolute_paths_in(command) == ["$HOME/code/x/.ratchet/problems-x.md"]
    for home in HOME_VARIABLES:
        assert absolute_paths_in(f"cat {home}/code/x/.ratchet/p.md") == [f"{home}/code/x/.ratchet/p.md"]


def test_bash_reads_through_home_variables_are_outside(tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()

    for home in HOME_VARIABLES:
        path = f"{home}/code/x/.ratchet/p.md"
        assert path in outside_of(tmp_path, copy, "Bash", {"command": f"cat {path}"}), home


def test_home_variables_are_expanded_before_the_inside_check(tmp_path, monkeypatch):
    copy = tmp_path / "copy"
    copy.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    # the home folder holds the copy, so a path through home into the copy stays inside
    assert outside_of(tmp_path, copy, "Bash", {"command": "cat $HOME/copy/a.py"}) == []
    assert outside_of(tmp_path, copy, "Bash", {"command": "cat %USERPROFILE%/copy/a.py"}) == []
    assert outside_of(tmp_path, copy, "Bash", {"command": "cat ${HOME}/p.md"}) == ["${HOME}/p.md"]


def test_other_leading_variables_are_outside(tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()
    # shlex keeps the backslash inside single quotes
    quoted = r"%APPDATA%\p.md"

    for path in ("$TMP/p.md", "${APPDATA}/p.md", "%APPDATA%/p.md", "$HOMEDIR/x"):
        assert absolute_paths_in(f"cat {path}") == [path]
        assert outside_of(tmp_path, copy, "Bash", {"command": f"cat {path}"}) == [path]
    assert absolute_paths_in(f"cat '{quoted}'") == [quoted]
    assert outside_of(tmp_path, copy, "Bash", {"command": f"cat '{quoted}'"}) == [quoted]


def test_shell_variables_and_format_strings_that_are_not_paths_are_clean(tmp_path):
    copy = tmp_path / "copy"
    copy.mkdir()
    commands = (
        'for f in $(git ls-files); do wc -l "$f"; done',
        "echo $PWD $?",
        "git log --format=%H%n%s",
        "echo $HOMEDIR",
    )

    for command in commands:
        assert absolute_paths_in(command) == [], command
        assert outside_of(tmp_path, copy, "Bash", {"command": command}) == [], command


def test_a_glob_pattern_that_starts_with_a_variable_is_not_joined_to_the_base(tmp_path):
    copy = tmp_path / "copy"
    (copy / "src").mkdir(parents=True)
    assert outside_of(tmp_path, copy, "Glob", {"pattern": "$TMP/*.md", "path": "src"}) == ["$TMP/*.md"]


def test_an_unset_home_variable_falls_back_to_the_home_folder(tmp_path, monkeypatch):
    monkeypatch.delenv("USERPROFILE", raising=False)
    copy = tmp_path / "copy"
    copy.mkdir()
    assert outside_of(tmp_path, copy, "Bash", {"command": "cat $USERPROFILE/p.md"}) != []


def test_a_one_segment_search_string_that_does_not_exist_is_not_a_path():
    assert absolute_paths_in('grep -rn "/apix" src') == []


@pytest.mark.parametrize("error", [OSError, ValueError])
def test_a_word_that_cannot_be_a_path_on_disk_is_not_a_path(monkeypatch, error):
    def fail(token):
        raise error("bad path")

    monkeypatch.setattr("ratchet.audit.turn_logs._git_bash_to_windows", fail)
    assert absolute_paths_in("awk /^def /{print}") == []


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
