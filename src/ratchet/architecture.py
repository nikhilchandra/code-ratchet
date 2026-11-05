"""The module contains the checker that reads a project's Python files without an agent and finds the
faults that follow from the module graph and the syntax tree, which means imports that break the
target design, files that belong to no part, import cycles, imports hidden inside functions, touches
of the outside world, global statements, mutable default arguments and careless exception handlers.
"""

from __future__ import annotations

import ast
import json
import os
from dataclasses import dataclass
from pathlib import Path

from ratchet.project import Project, is_test_file
from ratchet.target_design import TargetDesign, part_of

# folders that never hold the project's own code
IGNORED_FOLDERS = {"__pycache__", "node_modules"}

# top-level names of the modules whose import counts as touching the outside world
_EFFECT_IMPORTS = {
    "subprocess", "socket", "http", "urllib", "requests", "httpx", "sqlite3", "shutil",
    "threading", "tkinter", "PySide6", "PyQt5", "PyQt6",
}

# names of the builtins whose call counts as touching the outside world
_EFFECT_BUILTINS = {"open", "print", "input"}

# names of the methods that read the clock when called on any object
_CLOCK_METHODS = {"now", "today"}

# names of the pathlib methods that read or change the file system
_PATH_METHODS = {
    "read_text", "write_text", "read_bytes", "write_bytes", "open", "mkdir", "unlink", "rmdir",
    "touch", "exists", "is_file", "is_dir", "iterdir", "glob", "rglob",
}

# names of the calls that make a new mutable object
_MUTABLE_CALLS = {"list", "dict", "set", "bytearray", "defaultdict", "deque", "Counter", "OrderedDict"}


@dataclass(frozen=True)
class Module:
    """A Python file of the project, with the dotted name that other files import it by.

    Attributes:
        name: The dotted module name, such as "app.core.model".
        path: The file's path relative to the project, with forward slashes.
        is_package: True when the file is an __init__.py, so that the name is a package.
    """

    name: str
    path: str
    is_package: bool


@dataclass(frozen=True)
class Import:
    """One imported name found in a module.

    Attributes:
        source: The name of the module that holds the import.
        target: The dotted name that is imported. A project module is named in full, as in
            "app.core.model", and anything else is named as written, such as "os.path".
        line: The 1-based line of the import statement.
        text: The source text of that line.
        hidden: True when the import sits inside a function.
    """

    source: str
    target: str
    line: int
    text: str
    hidden: bool


@dataclass(frozen=True)
class Citation:
    """A place in the code that shows a fault.

    Attributes:
        file: The file's path relative to the project.
        line: The first line, counted from 1.
        end_line: The last line.
        quote: The exact source line.
        shows: What the line shows.
    """

    file: str
    line: int
    end_line: int
    quote: str
    shows: str


@dataclass(frozen=True)
class CodeFinding:
    """A fault found by code alone, written in the same fields as an agent's finding.

    Attributes:
        kind: One of "may_use", "no_part", "cycle", "hidden_import", "outside_world",
            "global_statement", "mutable_default", "bare_except" or "silent_except".
        subject: The module, class or function the fault is about.
        title: A short name for the fault.
        what_is_wrong: One or two sentences that say what is wrong.
        grade: "stated" when the fault breaks a rule in rules.md, or "standard" when it breaks
            a check of the general standard.
        breaks: The rule quoted from the target design, or the id of the standard's check.
        evidence: The places that show the fault.
        simpler_option: The change that would remove the fault.
        done_when: The conditions that show the fault is gone.
        source: The label of the code checker, which is always "C".
    """

    kind: str
    subject: str
    title: str
    what_is_wrong: str
    grade: str
    breaks: str
    evidence: tuple[Citation, ...]
    simpler_option: str
    done_when: tuple[str, ...]
    source: str = "C"


def find_modules(project: Project, design: TargetDesign | None) -> list[Module]:
    """Finds the project's Python files that are in scope and names each one as a module.

    A file is in scope when it is not a test file, is not in the skip list, and either matches
    an editable pattern or belongs to a part.

    Args:
        project: The project, as read from its .ratchet folder.
        design: The target design, or None when rules.md has no TOML block.

    Returns:
        The modules, sorted by path. When two files get the same name, the one that matches an
        editable pattern is kept, and otherwise the first one is kept.
    """
    found: dict[str, Module] = {}
    editable_names: set[str] = set()
    for folder, subfolders, files in os.walk(project.root):
        # drop hidden and generated folders from the walk
        subfolders[:] = sorted(
            name for name in subfolders if not name.startswith(".") and name not in IGNORED_FOLDERS
        )
        for file in sorted(files):
            if not file.endswith(".py"):
                continue
            path = Path(folder, file)
            relative = path.relative_to(project.root).as_posix()
            if is_test_file(relative):
                continue
            module = Module(_module_name(project.root, path), relative, file == "__init__.py")
            in_part = design is not None and part_of(design, module.name) is not None
            editable = project.may_change(relative)
            if not (in_part or editable):
                continue
            if design is not None and any(_under(module.name, p) for p in design.skip_modules):
                continue
            # keep the first file of a name, unless a later editable file replaces one that is not
            if module.name not in found or (editable and module.name not in editable_names):
                found[module.name] = module
                if editable:
                    editable_names.add(module.name)
    return sorted(found.values(), key=lambda module: module.path)


def read_imports(root: Path, module: Module, known: set[str]) -> list[Import]:
    """Reads the imports of one module, including those inside functions.

    Imports under an "if TYPE_CHECKING:" test are left out, and relative imports are resolved
    to full names.

    Args:
        root: The project's folder.
        module: The module to read.
        known: The names of all the project's modules, used to tell which name an import reaches.

    Returns:
        The imports in source order. A file that cannot be read or parsed gives none.
    """
    parsed = parse_module(root, module)
    if parsed is None:
        return []
    tree, lines = parsed
    imports: list[Import] = []

    def visit(node: ast.AST, hidden: bool) -> None:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for target in _targets(module, node, known):
                line = lines[node.lineno - 1]
                imports.append(Import(module.name, target, node.lineno, line, hidden))
            return
        # only the else branch of a type checking test is run
        if isinstance(node, ast.If) and _is_type_checking(node.test):
            for child in node.orelse:
                visit(child, hidden)
            return
        inside = hidden or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for child in ast.iter_child_nodes(node):
            visit(child, inside)

    visit(tree, False)
    return imports


def check_architecture(project: Project, design: TargetDesign | None) -> list[CodeFinding]:
    """Finds the faults in the project's modules and imports that code can find alone.

    Args:
        project: The project, as read from its .ratchet folder.
        design: The target design, or None when rules.md has no TOML block. Without a design,
            only import cycles and hidden imports are found.

    Returns:
        The findings, one for each pair of parts, each module, or each cycle.
    """
    modules = find_modules(project, design)
    known = {module.name for module in modules}
    by_name = {module.name: module for module in modules}
    imports = [item for module in modules for item in read_imports(project.root, module, known)]

    findings: list[CodeFinding] = []
    if design is not None:
        findings.extend(_may_use_findings(design, imports, by_name))
        if design.parts:
            for module in modules:
                if part_of(design, module.name) is None:
                    findings.append(_no_part_finding(project.root, module))
    findings.extend(_cycle_findings(imports, known, by_name))
    findings.extend(_hidden_import_findings(imports, by_name))

    # run the checks that read each module's syntax tree
    for module in modules:
        parsed = parse_module(project.root, module)
        if parsed is None:
            continue
        tree, lines = parsed
        if design is not None:
            part = part_of(design, module.name)
            if part is not None and part not in design.outside_parts:
                own = [item for item in imports if item.source == module.name]
                findings.extend(_outside_world_findings(design, part, module, tree, lines, own))
        findings.extend(_syntax_findings(module, tree, lines))
    return findings


def parse_module(root: Path, module: Module) -> tuple[ast.Module, list[str]] | None:
    """Reads and parses one module's file.

    Args:
        root: The project's folder.
        module: The module to read.

    Returns:
        The syntax tree and the lines of the source, or None when the file cannot be read or parsed.
    """
    try:
        text = (root / module.path).read_text(encoding="utf-8")
        return ast.parse(text), text.splitlines()
    except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
        return None


def _module_name(root: Path, path: Path) -> str:
    """Names a file by walking up through the folders that hold an __init__.py.

    Args:
        root: The project's folder, where the walk stops.
        path: The file.

    Returns:
        The dotted name. An __init__.py is named for its package.
    """
    names = [] if path.name == "__init__.py" else [path.stem]
    folder = path.parent
    while folder != root and (folder / "__init__.py").is_file():
        names.insert(0, folder.name)
        folder = folder.parent
    return ".".join(names)


def _under(name: str, prefix: str) -> bool:
    """Answers whether a dotted name is the prefix or sits below it.

    Args:
        name: A dotted module name.
        prefix: A dotted module name.

    Returns:
        True when the two are equal or the name starts with the prefix and a dot.
    """
    return name == prefix or name.startswith(prefix + ".")


def _is_type_checking(test: ast.expr) -> bool:
    """Answers whether an if test is TYPE_CHECKING, with or without its module.

    Args:
        test: The test expression.

    Returns:
        True for TYPE_CHECKING and for typing.TYPE_CHECKING.
    """
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _known_prefix(name: str, known: set[str]) -> str:
    """Finds the longest project module that a dotted name starts with.

    Args:
        name: A dotted name.
        known: The names of the project's modules.

    Returns:
        The longest module name that is the name or one of its leading parts, or the name
        itself when none is a project module.
    """
    parts = name.split(".")
    for count in range(len(parts), 0, -1):
        prefix = ".".join(parts[:count])
        if prefix in known:
            return prefix
    return name


def _targets(module: Module, node: ast.Import | ast.ImportFrom, known: set[str]) -> list[str]:
    """Works out the names that one import statement reaches.

    Args:
        module: The module that holds the statement.
        node: The statement.
        known: The names of the project's modules.

    Returns:
        One name for each imported name. A name imported from a module is the submodule when the
        project has one by that name, and otherwise the module.
    """
    if isinstance(node, ast.Import):
        return [_known_prefix(alias.name, known) for alias in node.names]
    base = node.module or ""
    if node.level:
        package = module.name.split(".") if module.is_package else module.name.split(".")[:-1]
        if node.level - 1 > len(package):
            return []
        package = package[: len(package) - (node.level - 1)]
        base = ".".join([*package, *([node.module] if node.module else [])])
    found = []
    for alias in node.names:
        submodule = f"{base}.{alias.name}" if base else alias.name
        if submodule in known:
            found.append(submodule)
        elif base:
            found.append(_known_prefix(base, known))
    return found


def _citation(by_name: dict[str, Module], item: Import, shows: str) -> Citation:
    """Makes the citation for one import.

    Args:
        by_name: The project's modules by name.
        item: The import.
        shows: What the import shows.

    Returns:
        A citation of the import's line.
    """
    return Citation(by_name[item.source].path, item.line, item.line, item.text, shows)


def _matches(spec: str, module: str, part: str) -> bool:
    """Answers whether an exception's from or to names a module, by module prefix or part.

    Args:
        spec: The text of the exception's from or to.
        module: A dotted module name.
        part: The part that the module belongs to.

    Returns:
        True when the spec is the part's name, the module, or a prefix of the module.
    """
    return spec == part or _under(module, spec)


def _may_use_findings(
    design: TargetDesign, imports: list[Import], by_name: dict[str, Module]
) -> list[CodeFinding]:
    """Finds the imports that the design's may_use forbids.

    Args:
        design: The target design.
        imports: The imports of the modules in scope.
        by_name: The modules by name.

    Returns:
        One finding for each pair of parts that has a forbidden import.
    """
    pairs: dict[tuple[str, str], list[Import]] = {}
    for item in imports:
        source_part = part_of(design, item.source)
        target_part = part_of(design, item.target)
        if source_part is None or target_part is None or source_part == target_part:
            continue
        if target_part in design.may_use[source_part]:
            continue
        if any(
            _matches(rule.source, item.source, source_part)
            and _matches(rule.target, item.target, target_part)
            for rule in design.exceptions
        ):
            continue
        pairs.setdefault((source_part, target_part), []).append(item)

    findings = []
    for (source_part, target_part), items in pairs.items():
        allowed = json.dumps(list(design.may_use[source_part]))
        sources = sorted({item.source for item in items})
        noun = "import" if len(items) == 1 else "imports"
        findings.append(CodeFinding(
            kind="may_use",
            subject=sources[0] if len(sources) == 1 else source_part,
            title=f"{source_part} imports {target_part}, which it may not use",
            what_is_wrong=(
                f"{len(items)} {noun} in part {source_part} reach part {target_part}, but "
                f"may_use.{source_part} allows only {allowed}."
            ),
            grade="stated",
            breaks=f"may_use.{source_part} = {allowed}",
            evidence=tuple(
                _citation(by_name, item, f"{item.source} imports {item.target}") for item in items
            ),
            simpler_option=(
                f"Move what {source_part} needs out of {target_part} into a part that "
                f"{source_part} may use, or pass it in as an argument."
            ),
            done_when=(f"No module in {source_part} imports a module in {target_part}.",),
        ))
    return findings


def _no_part_finding(root: Path, module: Module) -> CodeFinding:
    """Makes the finding for a module that belongs to no part.

    Args:
        root: The project's folder.
        module: The module.

    Returns:
        The finding, citing the first non-blank line of the file.
    """
    lines = (root / module.path).read_text(encoding="utf-8", errors="replace").splitlines()
    number = next((index for index, text in enumerate(lines, 1) if text.strip()), 1)
    quote = lines[number - 1] if lines else ""
    return CodeFinding(
        kind="no_part",
        subject=module.name,
        title=f"{module.name} belongs to no part",
        what_is_wrong=f"No prefix in [parts] matches {module.name}, so the design says nothing about it.",
        grade="stated",
        breaks="every module belongs to a part in [parts]",
        evidence=(Citation(module.path, number, number, quote, f"{module.name} starts here"),),
        simpler_option=f"Add a prefix that covers {module.name} to a part, or list it under [skip].",
        done_when=(f"{module.name} is covered by a prefix in [parts] or by [skip].",),
    )


def _cycle_findings(
    imports: list[Import], known: set[str], by_name: dict[str, Module]
) -> list[CodeFinding]:
    """Finds the import cycles between the project's modules, at every level of import.

    Args:
        imports: The imports of the modules in scope.
        known: The names of the project's modules.
        by_name: The modules by name.

    Returns:
        One finding for each group of modules that can all reach each other.
    """
    edges: dict[tuple[str, str], Import] = {}
    for item in imports:
        if item.target in known and item.target != item.source:
            edges.setdefault((item.source, item.target), item)
    graph: dict[str, list[str]] = {name: [] for name in sorted(known)}
    for source, target in edges:
        graph[source].append(target)

    findings = []
    for group in _strongly_connected(graph):
        if len(group) < 2:
            continue
        members = sorted(group)
        evidence = tuple(
            _citation(by_name, item, f"{item.source} imports {item.target}")
            for (source, target), item in sorted(edges.items())
            if source in group and target in group
        )
        names = ", ".join(members[:-1]) + f" and {members[-1]}"
        findings.append(CodeFinding(
            kind="cycle",
            subject=members[0],
            title=f"Import cycle between {names}",
            what_is_wrong=f"The modules {names} import each other, directly or through one another.",
            grade="standard",
            breaks="S2",
            evidence=evidence,
            simpler_option=(
                "Move what the modules share into a module that none of them imports from, "
                "so that every import points one way."
            ),
            done_when=(f"The modules {names} no longer form an import cycle.",),
        ))
    return findings


def _strongly_connected(graph: dict[str, list[str]]) -> list[set[str]]:
    """Finds the groups of nodes that can all reach each other, by Tarjan's method.

    Args:
        graph: Each node mapped to the nodes it points to.

    Returns:
        The groups, each as a set of nodes. A node on no cycle is a group of one.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    groups: list[set[str]] = []

    def connect(node: str) -> None:
        index[node] = low[node] = len(index)
        stack.append(node)
        on_stack.add(node)
        for other in graph[node]:
            if other not in index:
                connect(other)
                low[node] = min(low[node], low[other])
            elif other in on_stack:
                low[node] = min(low[node], index[other])
        # a node that is the root of its group closes the group
        if low[node] == index[node]:
            group = set()
            while True:
                member = stack.pop()
                on_stack.discard(member)
                group.add(member)
                if member == node:
                    break
            groups.append(group)

    for node in graph:
        if node not in index:
            connect(node)
    return groups


def _hidden_import_findings(
    imports: list[Import], by_name: dict[str, Module]
) -> list[CodeFinding]:
    """Finds the imports that sit inside a function.

    Args:
        imports: The imports of the modules in scope.
        by_name: The modules by name.

    Returns:
        One finding for each module that has such imports.
    """
    hidden: dict[str, list[Import]] = {}
    for item in imports:
        if item.hidden:
            hidden.setdefault(item.source, []).append(item)

    findings = []
    for name, items in hidden.items():
        noun = "import" if len(items) == 1 else "imports"
        findings.append(CodeFinding(
            kind="hidden_import",
            subject=name,
            title=f"{name} imports inside functions",
            what_is_wrong=(
                f"{name} has {len(items)} {noun} inside functions, so its dependencies are not "
                "all listed at the top of the file."
            ),
            grade="standard",
            breaks="S2",
            evidence=tuple(_citation(by_name, item, f"{name} imports {item.target}") for item in items),
            simpler_option="Move the imports to the top of the file.",
            done_when=(f"Every import in {name} is at the top of the file.",),
        ))
    return findings


def _effect_call(node: ast.Call) -> str | None:
    """Names the outside-world effect that a call has, if it has one.

    Args:
        node: The call.

    Returns:
        A short phrase such as "calls open()", or None when the call is not a listed effect.
    """
    function = node.func
    if isinstance(function, ast.Name) and function.id in _EFFECT_BUILTINS:
        return f"calls {function.id}()"
    if not isinstance(function, ast.Attribute):
        return None
    if function.attr in _CLOCK_METHODS:
        return f"reads the clock with .{function.attr}()"
    if function.attr == "time" and isinstance(function.value, ast.Name) and function.value.id == "time":
        return "reads the clock with time.time()"
    if function.attr in _PATH_METHODS:
        return f"uses the file method .{function.attr}()"
    return None


def _outside_world_findings(
    design: TargetDesign,
    part: str,
    module: Module,
    tree: ast.Module,
    lines: list[str],
    imports: list[Import],
) -> list[CodeFinding]:
    """Finds the places where a module in a part that may not touch the outside world does so.

    Args:
        design: The target design.
        part: The part that the module belongs to.
        module: The module.
        tree: The module's syntax tree.
        lines: The lines of the module's source.
        imports: The imports of the module.

    Returns:
        A list of one finding, or an empty list when the module touches nothing.
    """
    hits: list[tuple[int, str]] = []
    for item in imports:
        if item.target.split(".")[0] in _EFFECT_IMPORTS or any(
            _under(item.target, name) for name in design.outside_modules
        ):
            hits.append((item.line, f"imports {item.target}"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and (shows := _effect_call(node)) is not None:
            hits.append((node.lineno, shows))
    if not hits:
        return []
    hits.sort()
    allowed = json.dumps(list(design.outside_parts))
    noun = "place" if len(hits) == 1 else "places"
    return [CodeFinding(
        kind="outside_world",
        subject=module.name,
        title=f"{module.name} touches the outside world, which part {part} may not",
        what_is_wrong=(
            f"{module.name} has {len(hits)} {noun} that touch the outside world, but only the "
            f"parts {allowed} may do so."
        ),
        grade="stated",
        breaks=f"may_touch_outside_world.parts = {allowed}",
        evidence=tuple(
            Citation(module.path, line, line, lines[line - 1], f"{module.name} {shows}")
            for line, shows in hits
        ),
        simpler_option=(
            "Move the work that touches the outside world into a part that may do so, and pass "
            f"its results into {module.name}."
        ),
        done_when=(f"{module.name} no longer touches the outside world.",),
    )]


def _is_mutable(node: ast.expr) -> bool:
    """Answers whether a default argument makes a new mutable object once, at definition.

    Args:
        node: The default's expression.

    Returns:
        True for list, dict and set displays and comprehensions, and for calls such as list().
    """
    if isinstance(node, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    if isinstance(node, ast.Call):
        function = node.func
        name = function.id if isinstance(function, ast.Name) else getattr(function, "attr", "")
        return name in _MUTABLE_CALLS
    return False


def _syntax_findings(module: Module, tree: ast.Module, lines: list[str]) -> list[CodeFinding]:
    """Finds the global statements, mutable defaults and careless handlers in a module.

    Args:
        module: The module.
        tree: The module's syntax tree.
        lines: The lines of the module's source.

    Returns:
        One finding for each kind of fault that the module has, in a fixed order.
    """
    found: dict[str, list[tuple[int, str]]] = {
        "global_statement": [], "mutable_default": [], "bare_except": [], "silent_except": [],
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):
            found["global_statement"].append((node.lineno, f"declares global {', '.join(node.names)}"))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            defaults = [*node.args.defaults, *(d for d in node.args.kw_defaults if d is not None)]
            for default in defaults:
                if _is_mutable(default):
                    found["mutable_default"].append((default.lineno, "has a mutable default argument"))
        elif isinstance(node, ast.ExceptHandler):
            if node.type is None:
                found["bare_except"].append((node.lineno, "has a bare except"))
            if all(isinstance(statement, ast.Pass) for statement in node.body):
                found["silent_except"].append((node.lineno, "ignores the error with pass"))

    kinds = {
        "global_statement": (
            "S11", "global statements",
            "Pass the value in as an argument and return the new value, or keep it on an object.",
            "has no global statement",
        ),
        "mutable_default": (
            "S11", "mutable default arguments",
            "Default the argument to None and make the new object inside the function.",
            "has no mutable default argument",
        ),
        "bare_except": (
            "S14", "bare except clauses",
            "Name the errors that can happen and catch only those.",
            "has no bare except",
        ),
        "silent_except": (
            "S14", "except clauses that only pass",
            "Handle the error, log it, or let it rise.",
            "has no except clause that only passes",
        ),
    }
    findings = []
    for kind, (breaks, label, simpler, done) in kinds.items():
        hits = sorted(found[kind])
        if not hits:
            continue
        findings.append(CodeFinding(
            kind=kind,
            subject=module.name,
            title=f"{module.name} has {label}",
            what_is_wrong=f"{module.name} has {len(hits)} of {label}, which check {breaks} forbids.",
            grade="standard",
            breaks=breaks,
            evidence=tuple(
                Citation(module.path, line, line, lines[line - 1], f"{module.name} {shows}")
                for line, shows in hits
            ),
            simpler_option=simpler,
            done_when=(f"{module.name} {done}.",),
        ))
    return findings
