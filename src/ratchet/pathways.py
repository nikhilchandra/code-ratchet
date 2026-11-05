"""The module maps the Python packages under a repository's src folder into a graph of calls.

Every function and method is named package.module.qualname, and a function defined inside another
is named outer.inner. The code at the top of a module is named package.module.<module>. The map
lists the calls it can resolve, lists every call it cannot resolve, finds the entry points, and
links the functions that write a file to the functions that read the same file name. It can be
queried for the paths that lead down to a function, for what a function reaches, and for the
functions that a unified diff changes. Running `python -m ratchet.pathways` prints these as JSON.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import itertools
import json
import re
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ratchet.git import GitError, find_program
from ratchet.unified_diff import diff_lines

# The most paths that upstream_paths lists for one function.
PATH_LIMIT = 50

# The part of a name that stands for the code at the top of a module.
MODULE_PART = "<module>"

# A path segment that reads as a file name, such as ledger.json or rules.md.
_FILE_NAME = re.compile(r"[\w.-]+\.\w+")

# The kinds of syntax that define a function or a class.
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

# The methods that read or write the file that is their receiver.
_READ_METHODS = ("read_text", "read_bytes")
_WRITE_METHODS = ("write_text", "write_bytes")

# The prefixes that mark a function as reading or writing a file, once leading underscores go.
_READ_PREFIXES = ("load", "read")
_WRITE_PREFIXES = ("save", "write")


@dataclass
class FunctionNode:
    """Holds one function, method or module body in the map, with the lines it spans."""

    name: str
    module: str
    file: Path
    # The first line, which is the first decorator when there is one.
    start: int
    end: int
    is_entry: bool = False


@dataclass
class UnresolvedCall:
    """Holds a call that the map could not resolve, with where it is written."""

    function: str
    call: str
    # The posix path relative to the root.
    file: str
    line: int


@dataclass
class FileUse:
    """Holds one place where a function reads or writes a file."""

    function: str
    # The file name, or None when the name cannot be read statically.
    file_name: str | None
    # Either 'read' or 'write'.
    mode: str
    call: str
    # The posix path relative to the root.
    file: str
    line: int
    # Why the name is undetermined.
    reason: str | None = None


@dataclass
class PathwayMap:
    """Holds the call graph of the packages under one root, and what was found while building it."""

    root: Path
    functions: dict[str, FunctionNode] = field(default_factory=dict)
    calls: dict[str, set[str]] = field(default_factory=dict)
    callers: dict[str, set[str]] = field(default_factory=dict)
    unresolved: list[UnresolvedCall] = field(default_factory=list)
    file_uses: list[FileUse] = field(default_factory=list)
    entry_points: set[str] = field(default_factory=set)
    module_files: dict[str, Path] = field(default_factory=dict)
    # Maps each posix path relative to the root to its module.
    file_modules: dict[str, str] = field(default_factory=dict)


@dataclass
class _Info:
    """Holds the syntax of one function, with the class and function it sits inside."""

    node: ast.FunctionDef | ast.AsyncFunctionDef
    module: str
    cls: str | None
    parent: str | None


@dataclass
class _Class:
    """Holds one class, with its base class expressions and the names defined in its body."""

    module: str
    bases: list[ast.expr]
    methods: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _Scope:
    """Holds where a call is written, which is the function it belongs to and the scopes that
    name lookups search."""

    module: str
    # The name of the node that owns the call.
    func: str
    cls: str | None
    # The enclosing functions from the innermost, then the module's key.
    scopes: tuple[str, ...]
    file: str


def _shallow(nodes):
    """Yields every node under nodes, including definitions but without going inside them."""
    for node in nodes:
        yield node
        if not isinstance(node, _DEFINITIONS):
            yield from _shallow(ast.iter_child_nodes(node))


def _dotted_parts(node: ast.expr) -> list[str] | None:
    """Returns the names of a.b.c as a list, or None when the chain starts at something else.

    A chain starts at a plain name when its leftmost part is a variable, not a call or a literal.
    """
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return parts[::-1]


def _bound_names(target: ast.expr) -> list[str]:
    """Returns the names that an assignment target binds. A subscript or attribute target, such
    as seen[key] or repo.name, binds no name."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _bound_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return [name for item in target.elts for name in _bound_names(item)]
    return []


def _find_modules(root: Path) -> dict[str, Path]:
    """Returns each module under root/src as a dotted name and its file.

    Only packages that have an __init__.py are searched.
    """
    found: dict[str, Path] = {}

    def walk(folder: Path, package: str) -> None:
        for path in sorted(folder.iterdir()):
            if path.is_file() and path.suffix == ".py":
                found[package if path.name == "__init__.py" else f"{package}.{path.stem}"] = path
            elif path.is_dir() and (path / "__init__.py").exists():
                walk(path, f"{package}.{path.name}")

    source = root / "src"
    if source.is_dir():
        for top in sorted(source.iterdir()):
            if top.is_dir() and (top / "__init__.py").exists():
                walk(top, top.name)
    return found


class _Builder:
    """Builds a PathwayMap from the parsed modules in two passes.

    The first pass reads definitions and the second pass reads calls.
    """

    def __init__(self, root: Path) -> None:
        self.map = PathwayMap(root=root)
        self.trees: dict[str, ast.Module] = {}
        self.infos: dict[str, _Info] = {}
        self.classes: dict[str, _Class] = {}
        self.nested: dict[str, dict[str, str]] = {}
        self.module_names: dict[str, dict[str, str]] = {}
        self.imports: dict[str, dict[str, str]] = {}
        # Maps the id of a Call node to the name it resolved to.
        self.resolved: dict[int, str] = {}
        self._stmts: dict[str, list[ast.stmt]] = {}
        self._params: dict[str, set[str]] = {}
        self._assign_cache: dict[str, dict[str, list[ast.expr | None]]] = {}
        self._typing: set[tuple[str, str]] = set()

    def build(self) -> PathwayMap:
        """Runs every pass and returns the finished map."""
        modules = _find_modules(self.map.root)
        for module, path in modules.items():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError, ValueError):
                continue
            self.trees[module] = tree
            self.map.module_files[module] = path
            self.map.file_modules[self._relative(path)] = module
        for module, tree in self.trees.items():
            self._index_module(module, tree)
        for name in self.map.functions:
            self.map.calls[name] = set()
        for module, tree in self.trees.items():
            self._read_calls(self._scope_for(f"{module}.{MODULE_PART}"), tree.body)
        for name, info in self.infos.items():
            self._read_calls(self._scope_for(name), info.node.body)
        self._find_entries()
        for caller, targets in self.map.calls.items():
            for target in targets:
                self.map.callers.setdefault(target, set()).add(caller)
        return self.map

    def _relative(self, path: Path) -> str:
        """Returns the posix path of path relative to the root."""
        return path.relative_to(self.map.root).as_posix()

    # first pass: definitions

    def _index_module(self, module: str, tree: ast.Module) -> None:
        """Records the module's imports, its module node and every function and class in it."""
        path = self.map.module_files[module]
        key = f"{module}.{MODULE_PART}"
        end = max((getattr(node, "end_lineno", 1) or 1 for node in tree.body), default=1)
        self.map.functions[key] = FunctionNode(key, module, path, 1, end)
        self._stmts[key] = tree.body
        self._params[key] = set()
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        imports = self.imports[module] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        imports[alias.asname] = alias.name
                    else:
                        head = alias.name.split(".")[0]
                        imports[head] = head
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    kept = len(package.split(".")) - (node.level - 1)
                    base = ".".join(package.split(".")[:kept])
                else:
                    base = ""
                source = ".".join(part for part in (base, node.module) if part)
                for alias in node.names:
                    if alias.name != "*":
                        imports[alias.asname or alias.name] = f"{source}.{alias.name}"
        names = self.module_names[module] = {}
        self._index_scope(tree.body, f"{module}.", names, module, None, None)

    def _index_scope(
        self, stmts: list[ast.stmt], prefix: str, names: dict[str, str],
        module: str, cls: str | None, parent: str | None,
    ) -> None:
        """Records the functions and classes defined directly in stmts, and those inside them."""
        for node in _shallow(stmts):
            if not isinstance(node, _DEFINITIONS):
                continue
            full = prefix + node.name
            names[node.name] = full
            if isinstance(node, ast.ClassDef):
                info = self.classes[full] = _Class(module, node.bases)
                self._index_scope(node.body, f"{full}.", info.methods, module, full, parent)
                continue
            start = min([node.lineno] + [item.lineno for item in node.decorator_list])
            self.map.functions[full] = FunctionNode(
                full, module, self.map.module_files[module], start, node.end_lineno or node.lineno
            )
            self.infos[full] = _Info(node, module, cls, parent)
            self._stmts[full] = node.body
            arguments = node.args
            every = arguments.posonlyargs + arguments.args + arguments.kwonlyargs
            every += [arg for arg in (arguments.vararg, arguments.kwarg) if arg]
            self._params[full] = {arg.arg for arg in every}
            self.nested[full] = {}
            self._index_scope(node.body, f"{full}.", self.nested[full], module, cls, full)

    # name resolution

    def _scope_for(self, key: str) -> _Scope:
        """Returns the scope of a function's body.

        When key ends in <module>, it returns the scope of that module's top-level code.
        """
        if key in self.infos:
            info = self.infos[key]
            chain = [key]
            parent = info.parent
            while parent:
                chain.append(parent)
                parent = self.infos[parent].parent
            scopes = tuple(chain) + (f"{info.module}.{MODULE_PART}",)
            file = self._relative(self.map.module_files[info.module])
            return _Scope(info.module, key, info.cls, scopes, file)
        module = key[: -len(MODULE_PART) - 1]
        return _Scope(module, key, None, (key,), self._relative(self.map.module_files[module]))

    def _lookup(self, name: str, scope: _Scope) -> str | None:
        """Returns the dotted name that a bare name refers to, or None when nothing defines it."""
        for key in scope.scopes:
            if name in self.nested.get(key, {}):
                return self.nested[key][name]
        if name in self.module_names[scope.module]:
            return self.module_names[scope.module][name]
        if name in self.imports[scope.module]:
            return self.imports[scope.module][name]
        if hasattr(builtins, name):
            return f"builtins.{name}"
        return None

    def _bases(self, cls: str) -> list[str]:
        """Returns the classes in src that cls names as its bases."""
        found = []
        for base in self.classes[cls].bases:
            parts = _dotted_parts(base)
            module_key = f"{self.classes[cls].module}.{MODULE_PART}"
            head = self._lookup(parts[0], self._scope_for(module_key)) if parts else None
            dotted = ".".join([head] + parts[1:]) if head else None
            # a class is taken as it is, since resolving it would look up its bases again
            if dotted in self.classes:
                target = dotted
            else:
                target = self._resolve_dotted(dotted) if dotted else None
            if target in self.classes:
                found.append(target)
        return found

    def _method(self, cls: str, name: str, seen: frozenset[str] = frozenset()) -> str | None:
        """Returns the method called name that cls defines or inherits, or None."""
        if cls in seen:
            return None
        found = self.classes[cls].methods.get(name)
        if found in self.infos:
            return found
        for base in self._bases(cls):
            found = self._method(base, name, seen | {cls})
            if found:
                return found
        return None

    def _resolve_dotted(self, dotted: str, seen: frozenset[str] = frozenset()) -> str | None:
        """Returns the function, class or outside name that a full dotted name stands for.

        It returns None when the name cannot be resolved.
        """
        if dotted in self.infos or dotted in self.map.functions:
            return dotted
        if dotted in self.classes:
            return self._method(dotted, "__init__") or dotted
        owner, _, last = dotted.rpartition(".")
        if owner in self.classes:
            return self._method(owner, last)
        parts = dotted.split(".")
        for size in range(len(parts) - 1, 0, -1):
            module = ".".join(parts[:size])
            if module in self.module_names:
                break
        else:
            return dotted
        # follow a name that the module imports, which covers a re-export
        imported = self.imports[module].get(parts[size])
        if imported and dotted not in seen and imported != dotted:
            return self._resolve_dotted(".".join([imported] + parts[size + 1:]), seen | {dotted})
        return None

    def _var_type(self, name: str, scopes: tuple[str, ...]) -> str | None:
        """Returns what a variable assigned once from a call holds.

        The answer is a src class or an outside name.
        """
        for key in scopes:
            values = self._assigns(key).get(name)
            if values is None:
                continue
            once_from_call = len(values) == 1 and isinstance(values[0], ast.Call)
            if not once_from_call or (key, name) in self._typing:
                return None
            self._typing.add((key, name))
            target = self._resolve_call(values[0].func, self._scope_for(key))
            self._typing.discard((key, name))
            if target is None or target.startswith("builtins."):
                return None
            owner, _, last = target.rpartition(".")
            if last == "__init__" and owner in self.classes:
                return owner
            return None if target in self.infos else target
        return None

    def _resolve_call(self, func: ast.expr, scope: _Scope) -> str | None:
        """Returns the name that the callee func resolves to, or None when it cannot be resolved."""
        parts = _dotted_parts(func)
        if parts is None:
            return None
        head, rest = parts[0], parts[1:]
        if head in ("self", "cls") and scope.cls and len(rest) == 1:
            return self._method(scope.cls, rest[0])
        kind = self._var_type(head, scope.scopes) if len(rest) == 1 else None
        if kind:
            return self._method(kind, rest[0]) if kind in self.classes else f"{kind}.{rest[0]}"
        # a parameter or local of unknown type hides any global of the same name, so the call
        # is unresolved
        local_keys = [key for key in scope.scopes if not key.endswith(f".{MODULE_PART}")]
        if any(head in self._assigns(key) for key in local_keys):
            return None
        base = self._lookup(head, scope)
        return self._resolve_dotted(".".join([base] + rest)) if base else None

    # second pass: calls

    def _read_calls(self, scope: _Scope, body: list[ast.stmt]) -> None:
        """Records every call written in body, leaving out the bodies of functions defined in it."""
        for stmt in body:
            self._visit(stmt, scope)

    def _visit(self, node: ast.AST, scope: _Scope) -> None:
        """Records the calls in node.

        A nested function contributes only its decorators and defaults.
        """
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defaults = node.args.defaults + [item for item in node.args.kw_defaults if item]
            for part in node.decorator_list + defaults:
                self._visit(part, scope)
            return
        if isinstance(node, ast.Call):
            self._record_call(node, scope)
        for child in ast.iter_child_nodes(node):
            self._visit(child, scope)

    def _record_call(self, call: ast.Call, scope: _Scope) -> None:
        """Adds the edge for a call, or lists it as unresolved, then notes any file it uses."""
        target = self._resolve_call(call.func, scope)
        if target:
            self.resolved[id(call)] = target
            self.map.calls[scope.func].add(target)
        else:
            self.map.unresolved.append(
                UnresolvedCall(scope.func, ast.unparse(call.func), scope.file, call.lineno)
            )
        self._note_file_use(call, scope)

    # file names

    def _assigns(self, key: str) -> dict[str, list[ast.expr | None]]:
        """Returns the values assigned to each name in a scope.

        A value that is not an expression is listed as None.
        """
        if key not in self._assign_cache:
            found: dict[str, list[ast.expr | None]] = {}
            for name in self._params[key]:
                found.setdefault(name, []).append(None)
            for node in _shallow(self._stmts[key]):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            found.setdefault(target.id, []).append(node.value)
                        else:
                            for name in _bound_names(target):
                                found.setdefault(name, []).append(None)
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    found.setdefault(node.target.id, []).append(node.value)
                elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                    found.setdefault(node.target.id, []).append(None)
            self._assign_cache[key] = found
        return self._assign_cache[key]

    def _find_names(
        self, expr: ast.expr, scope: _Scope, depth: int
    ) -> tuple[list[str], str | None]:
        """Returns the file names that an expression holds, or the reason none can be read."""
        if isinstance(expr, ast.Constant):
            segment = re.split(r"[\\/]", expr.value)[-1] if isinstance(expr.value, str) else ""
            return ([segment] if _FILE_NAME.fullmatch(segment) else []), None
        if isinstance(expr, ast.JoinedStr):
            return [], "f-string"
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
            right = self._find_names(expr.right, scope, depth)
            if right[0]:
                return right
            left = self._find_names(expr.left, scope, depth)
            return left if left[0] else (right if right[1] else left)
        if isinstance(expr, ast.Name):
            return self._name_files(expr.id, scope, depth)
        if isinstance(expr, ast.Call):
            names, reason = [], None
            for arg in expr.args:
                found, why = self._find_names(arg, scope, depth)
                names += found
                reason = reason or why
            return names, None if names else (reason or "expression")
        return [], "expression"

    def _name_files(self, name: str, scope: _Scope, depth: int) -> tuple[list[str], str | None]:
        """Returns the file names that a variable or constant holds."""
        for key in scope.scopes:
            values = self._assigns(key).get(name)
            if values is None:
                continue
            if len(values) == 1 and values[0] is not None and depth < 3:
                return self._find_names(values[0], scope, depth + 1)
            if values == [None] and name in self._params[key]:
                return [], f"parameter {name}"
            return [], "expression"
        # a constant imported from another module in src
        module, _, constant = self.imports[scope.module].get(name, "").rpartition(".")
        module_key = f"{module}.{MODULE_PART}"
        values = self._assigns(module_key).get(constant) if module in self.trees else None
        if values and len(values) == 1 and values[0] is not None and depth < 3:
            module_scope = self._scope_for(f"{module}.{MODULE_PART}")
            return self._find_names(values[0], module_scope, depth + 1)
        return [], "expression"

    def _note_file_use(self, call: ast.Call, scope: _Scope) -> None:
        """Records the file that a call reads or writes, if the call is one of the known kinds."""
        func = call.func
        if isinstance(func, ast.Attribute):
            name = func.attr
        elif isinstance(func, ast.Name):
            name = func.id
        else:
            name = ""
        receiver = func.value if isinstance(func, ast.Attribute) else None
        target = self.resolved.get(id(call))
        arguments = call.args + [item.value for item in call.keywords]
        quiet = False
        if receiver is not None and name in _READ_METHODS:
            mode, sources = "read", [receiver]
        elif receiver is not None and name in _WRITE_METHODS:
            mode, sources = "write", [receiver]
        elif name == "open":
            at = 0 if receiver is not None else 1
            if len(call.args) > at:
                given = call.args[at]
            else:
                given = next((item.value for item in call.keywords if item.arg == "mode"), None)
            written = (
                isinstance(given, ast.Constant)
                and isinstance(given.value, str)
                and any(char in given.value for char in "wax+")
            )
            mode = "write" if written else "read"
            sources = [receiver] if receiver is not None else call.args[:1]
        elif target in ("json.load", "json.loads"):
            mode, sources, quiet = "read", arguments, target == "json.loads"
        elif target == "json.dump":
            mode, sources = "write", arguments
        elif name.lstrip("_").startswith(_READ_PREFIXES):
            mode, sources = "read", arguments
        elif name.lstrip("_").startswith(_WRITE_PREFIXES):
            mode, sources = "write", arguments
        else:
            return
        names: list[str] = []
        reason = None
        for source in sources:
            found, why = self._find_names(source, scope, 0)
            names += [item for item in found if item not in names]
            reason = reason or why
        text = ast.unparse(func)
        if names:
            for file_name in names:
                self.map.file_uses.append(
                    FileUse(scope.func, file_name, mode, text, scope.file, call.lineno)
                )
        elif not quiet:
            reason = reason or "expression"
            self.map.file_uses.append(
                FileUse(scope.func, None, mode, text, scope.file, call.lineno, reason)
            )

    # entry points

    def _script_entries(self) -> set[str]:
        """Returns the functions that [project.scripts] in the root's pyproject.toml names.

        A script that names a function outside the packages is left out. A pyproject.toml that
        is missing or is not valid TOML names none.
        """
        try:
            text = (self.map.root / "pyproject.toml").read_text(encoding="utf-8")
            data = tomllib.loads(text)
        except (OSError, tomllib.TOMLDecodeError):
            return set()
        names = set()
        for target in data.get("project", {}).get("scripts", {}).values():
            module, _, attribute = target.split()[0].partition(":")
            names.add(f"{module}.{attribute}")
        return names & self.infos.keys()

    def _uncalled(self) -> set[str]:
        """Returns the functions that no other function calls.

        A call from a function to itself, or from module-level code, does not count.
        """
        called = {
            target
            for caller, targets in self.map.calls.items() if caller in self.infos
            for target in targets if target != caller
        }
        return self.infos.keys() - called

    def _find_entries(self) -> None:
        """Marks the entry points: where a path into the code starts.

        These are the main of every cli module with its command functions, the steps registered
        on a rooster AgentGraph, and the functions that [project.scripts] in pyproject.toml
        names. When it finds none, every function that no other function calls is an entry
        point, so a script's main() under `if __name__ == "__main__"` is one.
        """
        entries: set[str] = set()
        for module in self.trees:
            main = f"{module}.main"
            if module.rpartition(".")[2] != "cli" or main not in self.infos:
                continue
            entries.add(main)
            for node in _shallow(self.infos[main].node.body):
                tests_command = isinstance(node, ast.If) and any(
                    isinstance(item, ast.Attribute) and item.attr == "command"
                    for item in ast.walk(node.test)
                )
                if tests_command:
                    for inner in _shallow(node.body):
                        resolved = self.resolved.get(id(inner))
                        if isinstance(inner, ast.Call) and resolved in self.infos:
                            entries.add(self.resolved[id(inner)])
        for name, info in self.infos.items():
            outer = self._scope_for(info.parent or f"{info.module}.{MODULE_PART}").scopes
            for decorator in info.node.decorator_list:
                is_step = (
                    isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "step"
                )
                if not is_step:
                    continue
                receiver = decorator.func.value
                kind = None
                if isinstance(receiver, ast.Name):
                    kind = self._var_type(receiver.id, outer)
                if kind and kind.startswith("rooster.") and kind.endswith(".AgentGraph"):
                    entries.add(name)
        entries |= self._script_entries()
        if not entries:
            entries = self._uncalled()
        self.map.entry_points = entries
        for name in entries:
            self.map.functions[name].is_entry = True


def build_map(root: Path) -> PathwayMap:
    """Returns the pathway map of the packages under root/src.

    Args:
        root: The repository whose src folder holds the packages to map.

    Returns:
        The call graph, the unresolved calls, the entry points and the file uses that were found.
    """
    return _Builder(Path(root)).build()


def _cycle_members(successors: dict[str, list[str]]) -> set[str]:
    """Returns the nodes of a graph that lie on a cycle, a self-loop included.

    Args:
        successors: Maps each node to the nodes it leads to. Every target is also a key.

    Returns:
        The nodes that belong to a strongly connected component of more than one node, or that
        lead to themselves.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    members: set[str] = set()
    for start in successors:
        if start in index:
            continue
        index[start] = low[start] = len(index)
        stack.append(start)
        on_stack.add(start)
        work = [(start, iter(successors[start]))]
        while work:
            node, targets = work[-1]
            for target in targets:
                if target not in index:
                    index[target] = low[target] = len(index)
                    stack.append(target)
                    on_stack.add(target)
                    work.append((target, iter(successors[target])))
                    break
                if target in on_stack:
                    low[node] = min(low[node], index[target])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    component = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == node:
                            break
                    if len(component) > 1:
                        members.update(component)
    members.update(node for node, targets in successors.items() if node in targets)
    return members


def _count_upstream(successors: dict[str, list[str]], entries: list[str], name: str) -> int:
    """Returns the number of simple paths from the entries down to name.

    The count of a node that is on no cycle depends only on the node, so it is kept and reused.
    A node on a cycle is counted again for each path that reaches it, with the nodes already on
    that path left out.

    Args:
        successors: Maps each node to the nodes a path may step to next. Name leads nowhere.
        entries: The nodes a path may start from.
        name: The node every path ends at.

    Returns:
        The number of paths.
    """
    cyclic = _cycle_members(successors)
    kept: dict[str, int] = {}

    def count(node: str, on_path: frozenset[str]) -> int:
        if node == name:
            return 1
        if node in kept:
            return kept[node]
        inside = on_path | {node} if node in cyclic else on_path
        total = sum(count(target, inside) for target in successors[node] if target not in inside)
        if node not in cyclic:
            kept[node] = total
        return total

    return sum(count(entry, frozenset()) for entry in entries)


def upstream_paths(
    pathway_map: PathwayMap, name: str, limit: int = PATH_LIMIT
) -> tuple[list[list[str]], int]:
    """Returns up to limit call paths from an entry point down to name.

    A path lists the entry point first and name last, and no function appears twice in it. A path
    stops at the first entry point it reaches, so an entry point's own path is just itself.

    The paths are listed in sorted order and the listing stops at limit, while the count of the
    others comes from a count of paths per function, so a graph with very many paths stays fast.

    Args:
        pathway_map: The map to search.
        name: The function that the paths lead to, as package.module.qualname.
        limit: The most paths to return.

    Returns:
        A tuple of the paths, sorted and cut to limit, and the count of paths left out.
    """
    entry_points = pathway_map.entry_points
    if name in entry_points:
        return ([[name]] if limit >= 1 else []), (0 if limit >= 1 else 1)
    # the functions that name can be reached from, where an entry point is not climbed past
    above = {name}
    pending = [name]
    while pending:
        node = pending.pop()
        if node in entry_points and node != name:
            continue
        for caller in pathway_map.callers.get(node, ()):
            if caller not in above:
                above.add(caller)
                pending.append(caller)
    # a path steps only to name or to a function that is not an entry point, and ends at name
    successors = {
        node: [] if node == name else sorted(
            target
            for target in pathway_map.calls.get(node, ())
            if target in above and (target == name or target not in entry_points)
        )
        for node in above
    }
    entries = sorted(node for node in above if node in entry_points)

    def walk(path: list[str]):
        for target in successors[path[-1]]:
            if target == name:
                yield [*path, target]
            elif target not in path:
                yield from walk([*path, target])

    def every():
        for entry in entries:
            yield from walk([entry])

    paths = list(itertools.islice(every(), limit))
    total = _count_upstream(successors, entries, name)
    return paths, max(0, total - limit)


def downstream(pathway_map: PathwayMap, name: str) -> list[str]:
    """Returns every function that name reaches through calls, leaves included.

    Args:
        pathway_map: The map to search.
        name: The function to start from, as package.module.qualname. It is not in the result.

    Returns:
        The sorted names of the functions reached.
    """
    seen: set[str] = set()
    pending = [name]
    while pending:
        for target in pathway_map.calls.get(pending.pop(), ()):
            if target not in seen:
                seen.add(target)
                pending.append(target)
    seen.discard(name)
    return sorted(seen)


def file_links(pathway_map: PathwayMap, name: str) -> dict:
    """Returns the files that name reads and writes, with who else uses each of them.

    The 'undetermined' list holds the file calls in name whose file name could not be read
    statically.

    Args:
        pathway_map: The map to search.
        name: The function to report on, as package.module.qualname.

    Returns:
        A dict with the keys reads and writes, which list the file names that name reads and
        writes, written_by, which maps each file read to the functions that write it, read_by,
        which maps each file written to the functions that read it, and undetermined, which lists
        the file calls whose file name could not be read.
    """
    own = [use for use in pathway_map.file_uses if use.function == name]
    reads = sorted({use.file_name for use in own if use.mode == "read" and use.file_name})
    writes = sorted({use.file_name for use in own if use.mode == "write" and use.file_name})

    def users(files: list[str], mode: str) -> dict[str, list[str]]:
        return {
            file_name: sorted(
                {
                    use.function
                    for use in pathway_map.file_uses
                    if use.mode == mode and use.file_name == file_name
                }
            )
            for file_name in files
        }

    return {
        "reads": reads,
        "writes": writes,
        "written_by": users(reads, "write"),
        "read_by": users(writes, "read"),
        "undetermined": [
            {"call": use.call, "file": use.file, "line": use.line, "reason": use.reason}
            for use in own if use.file_name is None
        ],
    }


def describe(pathway_map: PathwayMap, names: list[str], new: Sequence[str] = ()) -> dict:
    """Returns the upstream paths, downstream functions, file links and unresolved calls of each
    named function.

    Args:
        pathway_map: The map to search.
        names: The functions to describe, as package.module.qualname.
        new: The names that a plan adds or moves to, which the map does not hold yet.

    Returns:
        A dict with the key functions, which maps each known name to its report, the key unknown,
        which lists the names that are not in the map and not in new, and the key new, which lists
        the names that are not in the map and are in new.
    """
    described = {}
    unknown = []
    planned = []
    for name in names:
        if name not in pathway_map.functions:
            (planned if name in new else unknown).append(name)
            continue
        paths, more = upstream_paths(pathway_map, name)
        reached = downstream(pathway_map, name)
        within = {name, *reached}
        described[name] = {
            "upstream_paths": paths,
            "more_paths": more,
            "downstream": reached,
            "file_links": file_links(pathway_map, name),
            "unresolved": [
                {"function": item.function, "call": item.call, "file": item.file, "line": item.line}
                for item in pathway_map.unresolved if item.function in within
            ],
        }
    return {"functions": described, "unknown": unknown, "new": planned}


def _module_of_path(path: str) -> str | None:
    """Returns the dotted module of a posix path relative to the root.

    The module is named the way _find_modules names it.

    A path that is not a .py file under src gives None.
    """
    parts = path.split("/")
    if len(parts) < 3 or parts[0] != "src" or not parts[-1].endswith(".py"):
        return None
    if parts[-1] == "__init__.py":
        return ".".join(parts[1:-1])
    return ".".join([*parts[1:-1], parts[-1][:-3]])


def _old_map(root: Path, path: str, module: str) -> PathwayMap | None:
    """Returns a map of the committed version of path.

    It returns None when that version cannot be read or parsed. A file in a folder that held no
    __init__.py at HEAD gives None, because _find_modules skips it. The blob is read as bytes,
    because a text pipe on Windows swallows a decode error and returns empty output.
    """
    parts = path.split("/")
    try:
        git = find_program("git")
        for depth in range(2, len(parts)):
            package = "/".join([*parts[:depth], "__init__.py"])
            probe = subprocess.run(
                [git, "cat-file", "-e", f"HEAD:{package}"], capture_output=True, cwd=root
            )
            if probe.returncode != 0:
                return None
        finished = subprocess.run([git, "show", f"HEAD:{path}"], capture_output=True, cwd=root)
    except (OSError, GitError):
        return None
    if finished.returncode != 0:
        return None
    try:
        tree = ast.parse(finished.stdout.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError, ValueError):
        return None
    builder = _Builder(root)
    builder.map.module_files[module] = root / path
    builder._index_module(module, tree)
    return builder.map


def functions_of_diff(pathway_map: PathwayMap, diff_text: str) -> list[str]:
    """Returns the sorted functions in src that enclose a changed line of a unified diff.

    An added line counts at its line in the new file. A removed line counts at its line in the
    committed file, read with git show HEAD. A deleted file counts every function it held. When the
    committed file cannot be read or parsed, a removed line falls back to the line of the new file
    that it sits before. A changed line outside any function belongs to the module's <module> node.

    Args:
        pathway_map: The map of the working tree.
        diff_text: The unified diff to read.

    Returns:
        The sorted names of the functions that enclose a changed line.
    """
    changed: set[str] = set()
    module: str | None = None
    old_module: str | None = None
    old: PathwayMap | None = None
    last = None
    for line in diff_lines(diff_text):
        if (line.old_path, line.new_path) != last:
            last = (line.old_path, line.new_path)
            # map the committed file, and count all of it when the file was deleted
            module = pathway_map.file_modules.get(line.new_path) if line.new_path else None
            old_module = _module_of_path(line.old_path) if line.old_path else None
            old = None
            if line.old_path and old_module:
                old = _old_map(pathway_map.root, line.old_path, old_module)
            if line.new_path is None and old is not None:
                changed.update(old.functions)
        if line.marker == "+" and module:
            changed.add(_enclosing(pathway_map, module, line.new_line))
        elif line.marker == "-" and old is not None and old_module:
            changed.add(_enclosing(old, old_module, line.old_line))
        elif line.marker == "-" and module:
            changed.add(_removed_in(pathway_map, module, line.new_line, line.text[1:]))
    return sorted(changed)


def _removed_in(pathway_map: PathwayMap, module: str, line: int, text: str) -> str:
    """Returns the function that a removed line was in, given the new-file line it sits before.

    An indented line that sat at the end of a function now sits before whatever follows that
    function, so when the next line starts a node or is outside every function, the line before
    it is used instead.
    """
    after = _enclosing(pathway_map, module, line)
    if not text[:1].isspace() or line < 2:
        return after
    node = pathway_map.functions.get(after)
    if node is not None and node.start != line and not after.endswith(f".{MODULE_PART}"):
        return after
    return _enclosing(pathway_map, module, line - 1)


def _enclosing(pathway_map: PathwayMap, module: str, line: int) -> str:
    """Returns the innermost function of module that spans line, or the module's <module> node."""
    best = None
    for node in pathway_map.functions.values():
        if node.module != module or node.name.endswith(f".{MODULE_PART}"):
            continue
        if node.start <= line <= node.end:
            if best is None or node.start >= best.start:
                best = node
    return best.name if best else f"{module}.{MODULE_PART}"


def main(argv: list[str] | None = None) -> int:
    """Prints the pathway of the asked functions, or of the functions that a diff changes, as JSON.

    Args:
        argv: The arguments after the program name. None reads them from the command line.

    Returns:
        0 once the JSON is printed.
    """
    parser = argparse.ArgumentParser(
        prog="python -m ratchet.pathways", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--root", type=Path, required=True, help="the repository that holds src")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--functions", help="comma separated function names, as package.module.qualname"
    )
    group.add_argument("--diff-file", type=Path, help="a unified diff to map onto functions")
    args = parser.parse_args(argv)
    pathway_map = build_map(args.root)
    if args.functions is not None:
        names = [item.strip() for item in args.functions.split(",") if item.strip()]
        report = describe(pathway_map, names)
    else:
        changed = functions_of_diff(pathway_map, args.diff_file.read_text(encoding="utf-8"))
        report = {"changed_functions": changed, **describe(pathway_map, changed)}
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
