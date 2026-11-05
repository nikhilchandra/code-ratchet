"""The module contains the functions that read an agent's turn log and report which paths the
agent's tool calls touched.

The audit uses them to tell whether an agent stayed inside its copy of the code, and to list the
inventory files that the agent never opened.
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

MESSAGES_MARK = "--- messages ---"
STDERR_MARK = "--- stderr ---"

# The input fields of a tool call that hold one path.
PATH_FIELDS = ("file_path", "path", "notebook_path")

# A Windows path with a drive letter, found in the raw text because a shell would eat its backslashes.
WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|;&()]*")
# A drive letter and a separator at the start of a token.
DRIVE_START = re.compile(r"[A-Za-z]:[\\/]")
# A Git Bash drive, such as /c or /c/Users.
GIT_BASH_DRIVE = re.compile(r"/([A-Za-z])(?:/(.*))?$")
# A home folder variable, such as $HOME or %USERPROFILE%, that is the whole word or is followed by a separator.
HOME_VARIABLE = re.compile(r"^(?:\$HOME|\$\{HOME\}|\$USERPROFILE|\$\{USERPROFILE\}|(?i:%USERPROFILE%))(?=[\\/]|$)")
# Any variable, such as $TMP, ${TMP} or %TMP%, that is directly followed by a separator.
LEADING_VARIABLE = re.compile(r"^(?:\$[A-Za-z_]\w*|\$\{[A-Za-z_]\w*\}|%[A-Za-z_]\w*%)(?=[\\/])")
# A leading option name that is joined to its value, such as --file= or name=.
OPTION_PREFIX = re.compile(r"^-{0,2}[\w-]+=")

# Absolute paths that every command may name.
ALLOWED_PATHS = ("/dev/null",)


@dataclass(frozen=True)
class ToolCall:
    """One tool call that an agent made during a turn."""

    name: str
    # The call's input, such as {"file_path": "src/a.py"} for Read.
    arguments: dict


def read_tool_calls(log_path: Path) -> list[ToolCall]:
    """Reads the tool calls out of a turn's log file, in the order they were made.

    Args:
        log_path: The log file that the agent wrote for the turn.

    Returns:
        One ToolCall for each tool use block in the log's messages. Lines that are not JSON, and
        messages with no tool use, are skipped.
    """
    calls: list[ToolCall] = []
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
                if isinstance(block, dict) and "name" in block and isinstance(block.get("input"), dict):
                    calls.append(ToolCall(str(block["name"]), block["input"]))
    return calls


def paths_named(calls: list[ToolCall]) -> list[str]:
    """Collects every path that a list of tool calls names.

    Args:
        calls: The tool calls of one turn.

    Returns:
        The path fields of every call, the pattern of a Glob call, the 'glob' field of a Grep
        call, and every absolute, '~', '..' or variable path written in the command text of a Bash call. A
        relative pattern or glob is joined to the call's 'path' field. The regex 'pattern' of a
        Grep call is not a path and is left out. Relative paths are kept as written.
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
        command = call.arguments.get("command")
        if call.name == "Bash" and isinstance(command, str):
            paths.extend(absolute_paths_in(command))
    return paths


def absolute_paths_in(command: str) -> list[str]:
    """Finds the paths that are written in a shell command and can lead out of the copy.

    Args:
        command: The command text.

    Returns:
        Each Windows path with a drive letter, then each word that looks like an absolute path,
        a '~' path, a path with a '..' segment, a path through a home variable such as $HOME, and
        a path through another variable followed by a separator such as $TMP/x, in the order they
        appear. Search strings such as "/api/", URLs and the null device are not counted. A bare
        variable such as '$f' and a format such as '%H%n' are not paths.
    """
    found = WINDOWS_PATH.findall(command)
    for token in _tokens(command):
        token = OPTION_PREFIX.sub("", token)
        if not DRIVE_START.match(token) and _looks_like_path(token):
            found.append(token)
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


def _looks_like_path(token: str) -> bool:
    """Tells whether a word of a command names a place on disk.

    Args:
        token: One word of a command.

    Returns:
        True for a '~' path, a path with a '..' segment, a path that starts with a home variable,
        a path that starts with another variable and a separator, and an absolute path. A path
        that starts with a slash needs two segments, or one segment with a dot or a single letter,
        so that a search string such as "/api/" is not taken for a path.
    """
    if token in ALLOWED_PATHS or "://" in token:
        return False
    if HOME_VARIABLE.match(token) or LEADING_VARIABLE.match(token):
        return True
    if token.startswith("~") or ".." in re.split(r"[\\/]", token):
        return True
    if not token.startswith("/"):
        return False
    segments = [segment for segment in token.split("/") if segment]
    return len(segments) >= 2 or (len(segments) == 1 and ("." in segments[0] or len(segments[0]) == 1))


def paths_outside(log_path: Path, copy: Path) -> list[str]:
    """Lists the paths in a turn's tool calls that lie outside the copy of the code.

    Args:
        log_path: The log file that the agent wrote for the turn.
        copy: The folder of the copy that the agent worked in.

    Returns:
        Each path outside the copy, once, in the order first seen. A relative path is taken
        relative to the copy, so one that climbs out with '..' is outside.
    """
    outside: list[str] = []
    for path in paths_named(read_tool_calls(log_path)):
        if not _inside(path, copy) and path not in outside:
            outside.append(path)
    return outside


def unopened_files(log_path: Path, copy: Path, files: list[str]) -> list[str]:
    """Lists the files that an agent never opened during a turn.

    A file counts as opened when a tool call names it in a path field, or when a Bash command
    contains its path.

    Args:
        log_path: The log file that the agent wrote for the turn.
        copy: The folder of the copy that the agent worked in.
        files: Paths relative to the copy, such as the files of the inventory.

    Returns:
        The files that no tool call opened, in the order given.
    """
    calls = read_tool_calls(log_path)
    opened = {_resolved(copy, path) for path in paths_named(calls)}
    commands = [str(call.arguments.get("command", "")) for call in calls if call.name == "Bash"]
    return [
        file
        for file in files
        if _resolved(copy, file) not in opened
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


def _resolved(copy: Path, path: str) -> Path:
    """Resolves a path against the copy.

    Args:
        copy: The folder of the copy, which a relative path is taken from.
        path: The path as written. A leading '~' or home variable such as $HOME is the home folder
            and /c/... is a Git Bash drive.

    Returns:
        The absolute path with links and '..' resolved.
    """
    return (copy / os.path.expanduser(_git_bash_to_windows(_home_expanded(path)))).resolve()


def _inside(path: str, copy: Path) -> bool:
    """Tells whether a path lies in the copy.

    Args:
        path: The path as written.
        copy: The folder of the copy.

    Returns:
        True when the path, resolved against the copy, is the copy or below it. A path that starts
        with a variable other than a home variable is never inside, because its value is unknown.
    """
    if LEADING_VARIABLE.match(path) and not HOME_VARIABLE.match(path):
        return False
    return _resolved(copy, path).is_relative_to(copy.resolve())
