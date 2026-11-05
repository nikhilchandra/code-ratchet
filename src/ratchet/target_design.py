"""The module contains the functions that read the target design from a project's rules.md, which
means the template marker and the optional machine-readable TOML block.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass

TEMPLATE_MARKER = (
    "<!-- ratchet: template. Delete this line once the essential questions are answered. -->"
)

# a fenced block of TOML, with the fence lines left out
_BLOCK = re.compile(r"^```toml[ \t]*\n(.*?)^```[ \t]*$", re.DOTALL | re.MULTILINE)


class TargetDesignError(ValueError):
    """Signals a TOML block in rules.md that cannot be read as a target design."""


@dataclass(frozen=True)
class DesignException:
    """A deliberate import that the target design allows although may_use does not.

    Attributes:
        source: The module or part that imports.
        target: The module or part that is imported.
        why: The reason the owner gave.
    """

    source: str
    target: str
    why: str


@dataclass(frozen=True)
class TargetDesign:
    """The rules of a project that a script can check without an agent.

    Attributes:
        parts: Each part's name mapped to its dotted module prefixes.
        may_use: Each part's name mapped to the other parts it may use.
        outside_parts: The parts that may touch the outside world.
        outside_modules: Modules added to the built-in list of outside-world imports.
        exceptions: The deliberate exceptions to may_use.
        skip_modules: The module prefixes of generated or vendored code.
    """

    parts: dict[str, tuple[str, ...]]
    may_use: dict[str, tuple[str, ...]]
    outside_parts: tuple[str, ...]
    outside_modules: tuple[str, ...]
    exceptions: tuple[DesignException, ...]
    skip_modules: tuple[str, ...]


def is_template(text: str) -> bool:
    """Answers whether rules.md still has the template marker on its first line.

    Args:
        text: The contents of rules.md.

    Returns:
        True when the first line is the template marker.
    """
    lines = text.lstrip("﻿").splitlines()
    return bool(lines) and lines[0].strip() == TEMPLATE_MARKER


def read_target_design(text: str) -> TargetDesign | None:
    """Reads the TOML block of rules.md as a target design.

    Args:
        text: The contents of rules.md.

    Returns:
        The target design, or None when the text has no TOML block.

    Raises:
        TargetDesignError: The text has more than one TOML block, the block is not valid TOML,
            or its parts, may_use, outside-world parts, exceptions or skip list do not agree.
    """
    blocks = _BLOCK.findall(text)
    if not blocks:
        return None
    if len(blocks) > 1:
        raise TargetDesignError("rules.md has more than one toml block, and it may have only one")
    try:
        data = tomllib.loads(blocks[0])
    except tomllib.TOMLDecodeError as error:
        raise TargetDesignError(f"the toml block in rules.md is not valid: {error}") from error

    known = {"parts", "may_use", "may_touch_outside_world", "exceptions", "skip"}
    unknown = sorted(set(data) - known)
    if unknown:
        raise TargetDesignError(f"the toml block has unknown sections: {', '.join(unknown)}")

    parts = _read_parts(data.get("parts", {}))
    may_use = _read_may_use(data.get("may_use", {}), parts)
    outside = _table(data, "may_touch_outside_world")
    outside_parts = _strings(outside.get("parts", []), "may_touch_outside_world.parts")
    for name in outside_parts:
        if name not in parts:
            raise TargetDesignError(f"may_touch_outside_world names {name!r}, which is not a part")
    outside_modules = _strings(outside.get("modules", []), "may_touch_outside_world.modules")
    skip_modules = _strings(_table(data, "skip").get("modules", []), "skip.modules")
    exceptions = _read_exceptions(data.get("exceptions", []))
    return TargetDesign(parts, may_use, outside_parts, outside_modules, exceptions, skip_modules)


def part_of(design: TargetDesign, module: str) -> str | None:
    """Finds the part that a module belongs to, using the most specific prefix.

    Args:
        design: The target design.
        module: A dotted module name.

    Returns:
        The name of the part, or None when no prefix matches.
    """
    best_name = None
    best_length = -1
    for name, prefixes in design.parts.items():
        for prefix in prefixes:
            if (module == prefix or module.startswith(prefix + ".")) and len(prefix) > best_length:
                best_name = name
                best_length = len(prefix)
    return best_name


def _table(data: dict, name: str) -> dict:
    """Returns the named table of the block, or an empty table when it is missing.

    Args:
        data: The parsed block.
        name: The name of the table.

    Returns:
        The table.

    Raises:
        TargetDesignError: The entry is not a table.
    """
    table = data.get(name, {})
    if not isinstance(table, dict):
        raise TargetDesignError(f"{name} must be a table")
    return table


def _strings(value: object, label: str) -> tuple[str, ...]:
    """Returns a list of non-empty strings as a tuple.

    Args:
        value: The parsed value.
        label: The name of the value, used in the error.

    Returns:
        The strings.

    Raises:
        TargetDesignError: The value is not a list of non-empty strings.
    """
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise TargetDesignError(f"{label} must be a list of strings")
    return tuple(value)


def _read_parts(table: object) -> dict[str, tuple[str, ...]]:
    """Reads the parts table, where each part is one prefix or a list of prefixes.

    Args:
        table: The parsed parts table.

    Returns:
        Each part's name mapped to its prefixes.

    Raises:
        TargetDesignError: A part has no prefix, or two parts share a prefix.
    """
    if not isinstance(table, dict):
        raise TargetDesignError("parts must be a table")
    parts = {}
    owners: dict[str, str] = {}
    for name, value in table.items():
        prefixes = (value,) if isinstance(value, str) and value else _strings(value, f"parts.{name}")
        if not prefixes:
            raise TargetDesignError(f"part {name!r} needs at least one module prefix")
        for prefix in prefixes:
            if prefix in owners and owners[prefix] != name:
                raise TargetDesignError(
                    f"parts {owners[prefix]!r} and {name!r} both use the prefix {prefix!r}")
            owners[prefix] = name
        parts[name] = prefixes
    return parts


def _read_may_use(
    table: object, parts: dict[str, tuple[str, ...]]
) -> dict[str, tuple[str, ...]]:
    """Reads the may_use table, which must name every part and only parts.

    Args:
        table: The parsed may_use table.
        parts: The parts of the design.

    Returns:
        Each part's name mapped to the parts it may use.

    Raises:
        TargetDesignError: A part is missing, or a name is not a part.
    """
    if not isinstance(table, dict):
        raise TargetDesignError("may_use must be a table")
    missing = [name for name in parts if name not in table]
    if missing:
        raise TargetDesignError(f"may_use does not name the parts: {', '.join(missing)}")
    may_use = {}
    for name, value in table.items():
        if name not in parts:
            raise TargetDesignError(f"may_use names {name!r}, which is not a part")
        allowed = _strings(value, f"may_use.{name}")
        for other in allowed:
            if other not in parts:
                raise TargetDesignError(f"may_use.{name} names {other!r}, which is not a part")
        may_use[name] = allowed
    return may_use


def _read_exceptions(entries: object) -> tuple[DesignException, ...]:
    """Reads the exceptions tables, each of which needs from, to and why.

    Args:
        entries: The parsed list of exception tables.

    Returns:
        The exceptions.

    Raises:
        TargetDesignError: An entry is not a table or lacks one of the three fields.
    """
    if not isinstance(entries, list):
        raise TargetDesignError("exceptions must be a list of tables")
    found = []
    for entry in entries:
        fields = ("from", "to", "why")
        if not isinstance(entry, dict) or not all(
            isinstance(entry.get(field), str) and entry[field] for field in fields
        ):
            raise TargetDesignError("each exception needs from, to and why as text")
        found.append(DesignException(entry["from"], entry["to"], entry["why"]))
    return tuple(found)
