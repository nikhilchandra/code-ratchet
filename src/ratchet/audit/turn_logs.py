"""The module contains the functions that read an agent's turn log and report which paths the
agent's tool calls touched.

The audit uses them to tell whether an agent stayed inside its copy of the code, and to list the
inventory files that the agent never opened.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

MESSAGES_MARK = "--- messages ---"
STDERR_MARK = "--- stderr ---"

# The input fields of a tool call that hold one path.
PATH_FIELDS = ("file_path", "path", "notebook_path")

# A Windows path with a drive letter, or a POSIX path that starts a word.
WINDOWS_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|;&()]*")
POSIX_PATH = re.compile(r"(?:^|(?<=[\s\"'=(]))/[^\s\"'<>|;&()]+")

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
        The path fields of every call, the pattern of a Glob call when it is an absolute path,
        and every absolute path written in the command text of a Bash call. Relative paths are
        kept as written.
    """
    paths: list[str] = []
    for call in calls:
        for field in PATH_FIELDS:
            value = call.arguments.get(field)
            if isinstance(value, str) and value:
                paths.append(value)
        pattern = call.arguments.get("pattern")
        if call.name == "Glob" and isinstance(pattern, str) and _is_absolute(pattern):
            paths.append(pattern)
        command = call.arguments.get("command")
        if call.name == "Bash" and isinstance(command, str):
            paths.extend(absolute_paths_in(command))
    return paths


def absolute_paths_in(command: str) -> list[str]:
    """Finds the absolute paths written in the text of a shell command.

    Args:
        command: The command text.

    Returns:
        Each Windows path with a drive letter and each POSIX path that starts a word, in the
        order they appear. The null device is not counted.
    """
    found = WINDOWS_PATH.findall(command) + POSIX_PATH.findall(command)
    return [path for path in found if path not in ALLOWED_PATHS]


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
        True when it starts with a slash, or with a drive letter and a separator.
    """
    return path.startswith(("/", "\\")) or bool(re.match(r"[A-Za-z]:[\\/]", path))


def _resolved(copy: Path, path: str) -> Path:
    """Resolves a path against the copy.

    Args:
        copy: The folder of the copy, which a relative path is taken from.
        path: The path as written.

    Returns:
        The absolute path with links and '..' resolved.
    """
    return (copy / path).resolve()


def _inside(path: str, copy: Path) -> bool:
    """Tells whether a path lies in the copy.

    Args:
        path: The path as written.
        copy: The folder of the copy.

    Returns:
        True when the path, resolved against the copy, is the copy or below it.
    """
    return _resolved(copy, path).is_relative_to(copy.resolve())
