"""The module contains the functions that read facts about a project's code without an agent, which
means the inventory of its modules and classes, the function or class that holds a line, the tests
that reach a set of modules and the area a module belongs to.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass
from pathlib import Path

from ratchet.architecture import IGNORED_FOLDERS, Module, find_modules, parse_module, read_imports
from ratchet.project import Project, is_test_file
from ratchet.target_design import TargetDesign, part_of

# area given to a module that belongs to no part
NO_AREA = "other"


@dataclass(frozen=True)
class InventoryItem:
    """A module or class that the audit must give a verdict on.

    Attributes:
        name: The dotted name, such as "app.core.model" for a module or "app.core.model.Order"
            for a class.
        kind: "module" or "class".
        file: The file's path relative to the project, with forward slashes.
        line: The 1-based line where the item starts, which is 1 for a module.
        size: The number of lines the item spans.
    """

    name: str
    kind: str
    file: str
    line: int
    size: int


def build_inventory(project: Project, design: TargetDesign | None) -> list[InventoryItem]:
    """Lists every module in scope and every class defined at the top level or inside another class.

    Args:
        project: The project, as read from its .ratchet folder.
        design: The target design, or None when rules.md has no TOML block.

    Returns:
        The items, module by module in path order, each module followed by its classes in source
        order. A file that cannot be read or parsed gives its module alone, with size 0.
    """
    items: list[InventoryItem] = []
    for module in find_modules(project, design):
        parsed = parse_module(project.root, module)
        if parsed is None:
            items.append(InventoryItem(module.name, "module", module.path, 1, 0))
            continue
        tree, lines = parsed
        items.append(InventoryItem(module.name, "module", module.path, 1, len(lines)))
        items.extend(_class_items(module, tree.body, module.name))
    return items


def enclosing_name(project: Project, module: Module, line: int) -> str:
    """Finds the innermost class or function that holds a line.

    Args:
        project: The project, as read from its .ratchet folder.
        module: The module whose file holds the line.
        line: A 1-based line number.

    Returns:
        The dotted name, such as "app.core.model.Order.total". It is the module's own name when
        the line sits outside every class and function, or when the file cannot be parsed.
    """
    parsed = parse_module(project.root, module)
    if parsed is None:
        return module.name
    names = [module.name]
    body = parsed[0].body
    while True:
        inner = next(
            (
                node for node in body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and _first_line(node) <= line <= (node.end_lineno or node.lineno)
            ),
            None,
        )
        if inner is None:
            return ".".join(names)
        names.append(inner.name)
        body = inner.body


def tests_reaching(project: Project, names: set[str]) -> list[str]:
    """Finds the test files that import any of the given modules.

    Args:
        project: The project, as read from its .ratchet folder.
        names: The dotted names of the modules the tests must reach.

    Returns:
        The paths of the test files, relative to the project and sorted. A file counts when one
        of its imports names one of the modules.
    """
    known = {module.name for module in find_modules(project, None)} | names
    found = []
    for path in _test_files(project.root):
        module = Module(Path(path).stem, path, False)
        targets = {item.target for item in read_imports(project.root, module, known)}
        if targets & names:
            found.append(path)
    return sorted(found)


def area_of(design: TargetDesign | None, subject: str) -> str:
    """Names the area that a subject belongs to.

    Args:
        design: The target design, or None when rules.md has no TOML block.
        subject: The dotted name of a module, class or function.

    Returns:
        The name of the part that holds the subject's module, or NO_AREA when there is no design
        or no part matches.
    """
    if design is None:
        return NO_AREA
    return part_of(design, subject) or NO_AREA


def _first_line(node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """Finds the first line of a definition, which is its first decorator when it has one.

    Args:
        node: The class or function definition.

    Returns:
        The 1-based line of the first decorator, or of the definition itself when it has none.
    """
    return min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])


def _class_items(module: Module, body: list[ast.stmt], prefix: str) -> list[InventoryItem]:
    """Makes the items for the classes in a body, then those nested inside them.

    Args:
        module: The module that holds the body.
        body: The statements to search for class definitions.
        prefix: The dotted name of the module or class that holds the body.

    Returns:
        The class items in source order, each class followed by the classes nested inside it.
    """
    items = []
    for node in body:
        if not isinstance(node, ast.ClassDef):
            continue
        name = f"{prefix}.{node.name}"
        first = _first_line(node)
        size = (node.end_lineno or first) - first + 1
        items.append(InventoryItem(name, "class", module.path, first, size))
        items.extend(_class_items(module, node.body, name))
    return items


def _test_files(root: Path) -> list[str]:
    """Finds the paths of the test files under the project, skipping hidden and generated folders.

    Args:
        root: The project's folder.

    Returns:
        The paths of the test files, relative to the project with forward slashes, in walk order.
    """
    found = []
    for folder, subfolders, files in os.walk(root):
        subfolders[:] = [
            name for name in subfolders if not name.startswith(".") and name not in IGNORED_FOLDERS
        ]
        for file in files:
            relative = Path(folder, file).relative_to(root).as_posix()
            if is_test_file(relative):
                found.append(relative)
    return found
