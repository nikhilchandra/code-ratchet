"""The module contains the functions that read an agent's turn log and report which tool calls the
agent made, which of them name .ratchet, reach into .ratchet or .git or out of the project through
a file tool, or use a git option that reaches outside the project, and which inventory files the
agent never opened.

The audit uses them as a backstop after a turn: the agents' permission rules and rooster's list of
forbidden options stop such calls, and the log shows whether any call got through.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass, replace
from pathlib import Path

MESSAGES_MARK = "--- messages ---"
STDERR_MARK = "--- stderr ---"

# The input fields of a tool call that hold one path.
PATH_FIELDS = ("file_path", "path", "notebook_path")

# A Git Bash drive, such as /c or /c/Users.
GIT_BASH_DRIVE = re.compile(r"/([A-Za-z])(?:/(.*))?$")
# A home folder variable, such as $HOME or %USERPROFILE%, that is the whole word or is followed by a separator.
HOME_VARIABLE = re.compile(r"^(?:\$HOME|\$\{HOME\}|\$USERPROFILE|\$\{USERPROFILE\}|(?i:%USERPROFILE%))(?=[\\/]|$)")
# Any variable, such as $TMP, ${TMP} or %TMP%, that is directly followed by a separator.
LEADING_VARIABLE = re.compile(r"^(?:\$[A-Za-z_]\w*|\$\{[A-Za-z_]\w*\}|%[A-Za-z_]\w*%)(?=[\\/])")

# A word that is git's --output option, written in full or cut short to at least --ou, with or without a value.
OUTPUT_OPTION = re.compile(r"^--ou(?:t(?:p(?:u(?:t)?)?)?)?(?:=|$)")

# The quotes, spaces, slashes and shell operators that a command or a path is cut at to find its pieces.
PIECE_SEPARATORS = re.compile(r"[\s\"'=:;&|<>(){},\\/]+")
# The name of the folder that holds the old problem lists.
RATCHET_FOLDER = ".ratchet"
# The wording that a tool result opens with when the permission rules or rooster's hook refused the call.
DENIAL_STARTS = (
    "Permission to use",
    "Permission denied by hook",
    "Hook PreToolUse",
    "File is in a directory that is denied",
    "File is covered by a Read deny rule",
    "the option ",
    "the path ",
)
# The tag that the CLI wraps around a refusal that the permission rules or an input check made.
ERROR_TAG = "<tool_use_error>"
# The characters that start a wildcard in a Glob pattern.
WILDCARDS = ("*", "?", "[", "{")
# The folders inside the project that no file tool may reach.
HIDDEN_FOLDERS = (RATCHET_FOLDER, ".git")


@dataclass(frozen=True)
class ToolCall:
    """One tool call that an agent made during a turn."""

    name: str
    # The call's input, such as {"file_path": "src/a.py"} for Read.
    arguments: dict
    # True when the call's result is an error that opens with the wording of a refusal.
    denied: bool = False


def read_tool_calls(log_path: Path) -> list[ToolCall]:
    """Reads the tool calls out of a turn's log file, in the order they were made.

    Args:
        log_path: The log file that the agent wrote for the turn.

    Returns:
        One ToolCall for each tool use block in the log's messages. A call is marked denied when
        a later tool result block for its id is an error that opens with the wording of a refusal. Lines
        that are not JSON, and messages with no tool use, are skipped.
    """
    calls: list[ToolCall] = []
    ids: list[str] = []
    refused: set[str] = set()
    in_messages = False
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line == MESSAGES_MARK:
            in_messages = True
        elif line == STDERR_MARK:
            break
        elif in_messages and line.startswith("{"):
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict):
                    continue
                if "name" in block and isinstance(block.get("input"), dict):
                    calls.append(ToolCall(str(block["name"]), block["input"]))
                    ids.append(str(block.get("id", "")))
                elif block.get("is_error") is True and _is_denial(block.get("content")):
                    refused.add(str(block.get("tool_use_id", "")))
    return [replace(call, denied=True) if call_id in refused else call for call, call_id in zip(calls, ids)]


def _is_denial(content: object) -> bool:
    """Tells whether the content of a tool result says that a call was refused.

    Args:
        content: The result block's content, which is a string or a list of blocks with 'text'.

    Returns:
        True when the text, after an ERROR_TAG and an "Error: " prefix are removed, opens with one
        of DENIAL_STARTS. The same wording in the middle of an error, as in a message from a
        command that was run, does not count.
    """
    if isinstance(content, list):
        content = " ".join(str(block.get("text", "")) for block in content if isinstance(block, dict))
    text = str(content).strip().removeprefix(ERROR_TAG).removeprefix("Error: ")
    return text.startswith(DENIAL_STARTS)


def forbidden_calls(log_path: Path, options: tuple[str, ...], root: Path) -> list[ToolCall]:
    """Lists the tool calls in a turn that name .ratchet, use a forbidden git option, or reach
    through a file tool into .ratchet or .git or out of the project.

    Args:
        log_path: The log file that the agent wrote for the turn.
        options: The option words that no Bash command may contain, such as ("--no-index", "-C").
        root: The project's folder, which relative paths are read from.

    Returns:
        Each such call in the order made, with its denied flag. A denied call was stopped by the
        permission rules, so it did not touch anything.
    """
    found = []
    for call in read_tool_calls(log_path):
        command = call.arguments.get("command")
        uses_option = call.name == "Bash" and isinstance(command, str) and _forbidden_option(command, options)
        if uses_option or _names_ratchet(call) or _lands_out_of_bounds(call, root):
            found.append(call)
    return found


def _names_ratchet(call: ToolCall) -> bool:
    """Tells whether a tool call names the .ratchet folder.

    Args:
        call: One tool call.

    Returns:
        True when a path field, a Glob pattern, a Grep glob or a Bash command has a piece, once cut
        at slashes, quotes, spaces, '=', ':' and shell operators, that is exactly '.ratchet'. The
        regex 'pattern' of a Grep call is never checked, and a name such as not.ratchet.txt is not
        the folder.
    """
    texts = [call.arguments.get(field) for field in PATH_FIELDS]
    texts.append(call.arguments.get("pattern") if call.name == "Glob" else call.arguments.get("glob"))
    if call.name == "Bash":
        texts.append(call.arguments.get("command"))
    return any(
        isinstance(text, str) and RATCHET_FOLDER in PIECE_SEPARATORS.split(text) for text in texts
    )


def _real_place(value: str, base: str) -> str:
    """Finds where a path really is, with links and Windows short names expanded and the case
    form that the platform compares with."""
    return os.path.normcase(os.path.realpath(os.path.join(base, os.path.expanduser(value))))


def _is_below(place: str, folder: str) -> bool:
    """Tells whether a real place is a folder itself or lies below it."""
    return place == folder or place.startswith(folder if folder.endswith(os.sep) else folder + os.sep)


def _lands_out_of_bounds(call: ToolCall, root: Path) -> bool:
    """Tells whether a file tool call reaches, by where its paths really are, into .ratchet or
    .git or out of the project.

    Args:
        call: One tool call.
        root: The project's folder, which relative paths are read from.

    Returns:
        True when a path field, or the leading folders of a Glob pattern, resolves to a place
        outside 'root' or inside its .ratchet or .git folder. A call with no path field works in
        'root', so it is in bounds, and so is a call that is not a file tool.
    """
    base = str(root)
    folder = _real_place(base, base)
    hidden = [_real_place(name, base) for name in HIDDEN_FOLDERS]
    checks = [(value, base) for field in PATH_FIELDS if isinstance(value := call.arguments.get(field), str)]
    pattern = call.arguments.get("pattern")
    if call.name == "Glob" and isinstance(pattern, str):
        # the folders before the first wildcard are read from the 'path' field, else from the project
        stops = [pattern.index(mark) for mark in WILDCARDS if mark in pattern]
        head = pattern[: min(stops)] if stops else pattern
        leading = head[: max(head.rfind("/"), head.rfind("\\")) + 1]
        path = call.arguments.get("path")
        if leading:
            checks.append((leading, _real_place(path, base) if isinstance(path, str) else base))
    for value, start in checks:
        place = _real_place(value, start)
        if not _is_below(place, folder) or any(_is_below(place, name) for name in hidden):
            return True
    return False


def _forbidden_option(command: str, options: tuple[str, ...]) -> str:
    """Finds the first forbidden option that a shell command uses, as rooster's hook does.

    Args:
        command: The shell command, which may chain several commands.
        options: The option words to look for.

    Returns:
        The first option that a word of the command matches, or "" when none does. A word matches
        an option when it is the option, the option followed by '=' and a value, or, for an option
        that starts with '--', a prefix of it that git accepts, of at least 4 characters.
    """
    for word in _tokens(command):
        head = word.split("=", 1)[0]
        for option in options:
            if word == option:
                return option
            if option.startswith("--") and (
                head == option or (len(head) >= 4 and head.startswith("--") and option.startswith(head))
            ):
                return option
    return ""


def _file_tool_paths(calls: list[ToolCall]) -> list[str]:
    """Collects the paths that the arguments of a list of tool calls name.

    Args:
        calls: The tool calls of one turn.

    Returns:
        The path fields of every call, the pattern of a Glob call and the 'glob' field of a Grep
        call. A relative pattern or glob is joined to the call's 'path' field. The regex 'pattern'
        of a Grep call is not a path and is left out. Relative paths are kept as written.
    """
    paths: list[str] = []
    for call in calls:
        for field in PATH_FIELDS:
            value = call.arguments.get(field)
            if isinstance(value, str) and value:
                paths.append(value)
        base = call.arguments.get("path")
        base = base if isinstance(base, str) else ""
        # a Glob pattern and a Grep glob are paths below the call's path field
        pattern = call.arguments.get("pattern") if call.name == "Glob" else call.arguments.get("glob")
        if call.name in ("Glob", "Grep") and isinstance(pattern, str) and pattern:
            paths.append(_joined(base, pattern))
    return paths


def output_commands(log_path: Path) -> list[str]:
    """Lists the Bash commands in a turn that pass git's --output option.

    The git commands diff, log and show write their output to the file that --output names, and
    the rules that allow those commands do not stop that write. Git also accepts the option cut
    short, such as --outp, so every spelling from --ou up counts.

    Args:
        log_path: The log file that the agent wrote for the turn.

    Returns:
        Each Bash command with a word that is the option, in the order made. A command made twice
        is listed once.
    """
    found: list[str] = []
    for call in read_tool_calls(log_path):
        command = call.arguments.get("command")
        if call.name != "Bash" or not isinstance(command, str) or command in found:
            continue
        if any(OUTPUT_OPTION.match(word) for word in _tokens(command)):
            found.append(command)
    return found


def _tokens(command: str) -> list[str]:
    """Splits a shell command into words, with the shell operators as words of their own.

    Args:
        command: The command text.

    Returns:
        The words with their quotes removed. A command with unbalanced quotes is split at spaces.
    """
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        return [word.strip("\"'") for word in command.split()]


def unopened_files(log_path: Path, root: Path, files: list[str]) -> list[str]:
    """Lists the files that an agent never opened during a turn.

    A file counts as opened when a tool call names it in a path field, or when a Bash command
    contains its path.

    Args:
        log_path: The log file that the agent wrote for the turn.
        root: The project's folder, which relative paths are read from.
        files: Paths relative to the project, such as the files of the inventory.

    Returns:
        The files that no tool call opened, in the order given.
    """
    calls = read_tool_calls(log_path)
    opened = {_resolved(root, path) for path in _file_tool_paths(calls)}
    commands = [str(call.arguments.get("command", "")) for call in calls if call.name == "Bash"]
    return [
        file
        for file in files
        if _resolved(root, file) not in opened
        and not any(Path(file).as_posix() in command.replace("\\", "/") for command in commands)
    ]


def _is_absolute(path: str) -> bool:
    """Tells whether a path is absolute on either Windows or POSIX.

    Args:
        path: The path as written.

    Returns:
        True when it starts with a slash, a '~' or a variable, or with a drive letter and a separator.
    """
    if HOME_VARIABLE.match(path) or LEADING_VARIABLE.match(path):
        return True
    return path.startswith(("/", "\\", "~")) or bool(re.match(r"[A-Za-z]:[\\/]", path))


def _joined(base: str, pattern: str) -> str:
    """Joins a Glob pattern or Grep glob to the path that the call searches in.

    Args:
        base: The call's path field, or an empty string when it has none.
        pattern: The pattern as written.

    Returns:
        The pattern alone when it is absolute, starts with '~' or has no base, and the base and
        the pattern joined by a slash otherwise.
    """
    if not base or _is_absolute(pattern):
        return pattern
    return f"{base}/{pattern}"


def _git_bash_to_windows(path: str) -> str:
    """Rewrites a Git Bash drive path such as /c/Users/me as C:/Users/me on Windows.

    Args:
        path: The path as written.

    Returns:
        The Windows form when running on Windows and the path starts with a one-letter drive,
        and the path unchanged otherwise.
    """
    match = GIT_BASH_DRIVE.match(path)
    if os.name != "nt" or not match:
        return path
    return f"{match.group(1).upper()}:/{match.group(2) or ''}"


def _home_expanded(path: str) -> str:
    """Replaces a leading home variable of a path with the home folder.

    Args:
        path: The path as written.

    Returns:
        The path with a leading $HOME, ${HOME}, $USERPROFILE, ${USERPROFILE} or %USERPROFILE%
        replaced by that variable's value. A variable that is not set is replaced by the home
        folder of the current user. A path with no home variable is returned unchanged.
    """
    match = HOME_VARIABLE.match(path)
    if not match:
        return path
    # take the variable's name out of its $, braces or percent signs
    name = match.group(0).strip("${}%").upper()
    value = os.path.expandvars("${" + name + "}")
    # an unset variable is left as written, so fall back to the home folder
    if value.startswith("$"):
        value = os.path.expanduser("~")
    return value + path[match.end():]


def _resolved(root: Path, path: str) -> Path:
    """Resolves a path against the project's folder.

    Args:
        root: The project's folder, which a relative path is taken from.
        path: The path as written. A leading '~' or home variable such as $HOME is the home folder
            and /c/... is a Git Bash drive.

    Returns:
        The absolute path with links and '..' resolved.
    """
    return (root / os.path.expanduser(_git_bash_to_windows(_home_expanded(path)))).resolve()
