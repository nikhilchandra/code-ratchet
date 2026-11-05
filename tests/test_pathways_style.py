"""Checks that src/ratchet/pathways.py follows the house comment style and line width."""

from __future__ import annotations

import ast
import io
import tokenize
from pathlib import Path

import ratchet.pathways

# The source file that these tests read.
SOURCE = Path(ratchet.pathways.__file__)

# The widest line, in characters, that the house style allows.
WIDTH = 100

# The public functions whose docstrings must document their arguments and results.
PUBLIC = [
    "build_map",
    "upstream_paths",
    "downstream",
    "file_links",
    "describe",
    "functions_of_diff",
]


def source_text() -> str:
    """Returns the text of src/ratchet/pathways.py."""
    return SOURCE.read_text(encoding="utf-8")


def is_dataclass(node: ast.ClassDef) -> bool:
    """Tells whether a class is decorated with @dataclass, bare or called with arguments."""
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name) and target.id == "dataclass":
            return True
        if isinstance(target, ast.Attribute) and target.attr == "dataclass":
            return True
    return False


def test_no_line_is_longer_than_100_columns():
    lines = source_text().splitlines()
    long_lines = [
        f"{number}: {len(line)}" for number, line in enumerate(lines, 1) if len(line) > WIDTH
    ]
    assert long_lines == []


def test_no_comment_trails_code():
    trailing = []
    tokens = tokenize.generate_tokens(io.StringIO(source_text()).readline)
    for token in tokens:
        # a comment trails code when its line holds something other than spaces before the '#'
        if token.type == tokenize.COMMENT and token.line[: token.start[1]].strip():
            trailing.append(f"{token.start[0]}: {token.line.strip()}")
    assert trailing == []


def test_dataclass_docstrings_start_with_holds():
    tree = ast.parse(source_text())
    classes = [
        node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and is_dataclass(node)
    ]
    names = {node.name for node in classes}
    expected = {
        "FunctionNode", "UnresolvedCall", "FileUse", "PathwayMap", "_Info", "_Class", "_Scope"
    }
    assert expected <= names
    wrong = [
        node.name for node in classes if not (ast.get_docstring(node) or "").startswith("Holds ")
    ]
    assert wrong == []


def test_public_functions_document_args_and_returns():
    tree = ast.parse(source_text())
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    missing = []
    for name in PUBLIC:
        docstring = ast.get_docstring(functions[name]) or ""
        if "Args:" not in docstring or "Returns:" not in docstring:
            missing.append(name)
    assert missing == []
