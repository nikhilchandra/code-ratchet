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
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

# The most paths that upstream_paths lists for one function.
PATH_LIMIT = 50

# The part of a name that stands for the code at the top of a module.
MODULE_PART = "<module>"

# A path segment that reads as a file name, such as ledger.json or rules.md.
_FILE_NAME = re.compile(r"[\w.-]+\.\w+")

# The line that starts a hunk of a unified diff.
_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# The kinds of syntax that define a function or a class.
_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

# The methods that read or write the file that is their receiver.
_READ_METHODS = ("read_text", "read_bytes")
_WRITE_METHODS = ("write_text", "write_bytes")

# The prefixes that mark a function as reading or writing a file, once leading underscores are dropped.
_READ_PREFIXES = ("load", "read")
_WRITE_PREFIXES = ("save", "write")


@dataclass
class FunctionNode:
    """One function, method or module body in the map, with the lines it spans."""

    name: str
    module: str
    file: Path
    start: int  # the first line, which is the first decorator when there is one
    end: int
    is_entry: bool = False


@dataclass
class UnresolvedCall:
    """A call that the map could not resolve, with where it is written."""

    function: str
    call: str
    file: str  # posix path relative to the root
    line: int


@dataclass
class FileUse:
    """One place where a function reads or writes a file."""

    function: str
    file_name: str | None  # None when the name cannot be read statically
    mode: str  # 'read' or 'write'
    call: str
    file: str  # posix path relative to the root
    line: int
    reason: str | None = None  # why the name is undetermined


@dataclass
class PathwayMap:
    """The call graph of the packages under one root, and everything found while building it."""

    root: Path
    functions: dict[str, FunctionNode] = field(default_factory=dict)
    calls: dict[str, set[str]] = field(default_factory=dict)
    callers: dict[str, set[str]] = field(default_factory=dict)
    unresolved: list[UnresolvedCall] = field(default_factory=list)
    file_uses: list[FileUse] = field(default_factory=list)
    entry_points: set[str] = field(default_factory=set)
    module_files: dict[str, Path] = field(default_factory=dict)
    file_modules: dict[str, str] = field(default_factory=dict)  # posix path relative to root -> module


@dataclass
class _Info:
    """The syntax of one function, with the class and function it sits inside."""

    node: ast.FunctionDef | ast.AsyncFunctionDef
    module: str
    cls: str | None
    parent: str | None


@dataclass
class _Class:
    """One class, with its base class expressions and the names defined in its body."""

    module: str
    bases: list[ast.expr]
    methods: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _Scope:
    """Where a call is written: the function it belongs to and the scopes that name lookups search."""

    module: str
    func: str  # the name of the node that owns the call
    cls: str | None
    scopes: tuple[str, ...]  # enclosing functions from the innermost, then the module's key
    file: str


def _shallow(nodes):
    """Yields every node under nodes, including definitions but without going inside them."""
    for node in nodes:
        yield node
        if not isinstance(node, _DEFINITIONS):
            yield from _shallow(ast.iter_child_nodes(node))


def _dotted_parts(node: ast.expr) -> list[str] | None:
    """Returns the names of a.b.c as a list, or None when the chain does not start at a plain name."""
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
    """Returns each module under root/src as a dotted name and its file, for packages that have an __init__.py."""
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
    """Builds a PathwayMap from the parsed modules in two passes, one for definitions and one for calls."""

    def __init__(self, root: Path) -> None:
        self.map = PathwayMap(root=root)
        self.trees: dict[str, ast.Module] = {}
        self.infos: dict[str, _Info] = {}
        self.classes: dict[str, _Class] = {}
        self.nested: dict[str, dict[str, str]] = {}
        self.module_names: dict[str, dict[str, str]] = {}
        self.imports: dict[str, dict[str, str]] = {}
        self.resolved: dict[int, str] = {}  # id of a Call node -> the name it resolved to
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
                base = ".".join(package.split(".")[: len(package.split(".")) - (node.level - 1)]) if node.level else ""
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
            self.map.functions[full] = FunctionNode(full, module, self.map.module_files[module], start, node.end_lineno or node.lineno)
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
        """Returns the scope of a function's body, or of a module's top-level code when key ends in <module>."""
        if key in self.infos:
            info = self.infos[key]
            chain = [key]
            parent = info.parent
            while parent:
                chain.append(parent)
                parent = self.infos[parent].parent
            scopes = tuple(chain) + (f"{info.module}.{MODULE_PART}",)
            return _Scope(info.module, key, info.cls, scopes, self._relative(self.map.module_files[info.module]))
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
            head = self._lookup(parts[0], self._scope_for(f"{self.classes[cls].module}.{MODULE_PART}")) if parts else None
            dotted = ".".join([head] + parts[1:]) if head else None
            # a class is taken as it is, since resolving it would look up its bases again
            target = dotted if dotted in self.classes else self._resolve_dotted(dotted) if dotted else None
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
        """Returns the function, class or outside name that a full dotted name stands for, or None."""
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
        """Returns what a variable assigned once from a call holds, as a src class or an outside name."""
        for key in scopes:
            values = self._assigns(key).get(name)
            if values is None:
                continue
            if len(values) != 1 or not isinstance(values[0], ast.Call) or (key, name) in self._typing:
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
        # a parameter or local of unknown type hides any global of the same name, so the call is unresolved
        if any(head in self._assigns(key) for key in scope.scopes if not key.endswith(f".{MODULE_PART}")):
            return None
        base = self._lookup(head, scope)
        return self._resolve_dotted(".".join([base] + rest)) if base else None

    # second pass: calls

    def _read_calls(self, scope: _Scope, body: list[ast.stmt]) -> None:
        """Records every call written in body, leaving out the bodies of the functions defined in it."""
        for stmt in body:
            self._visit(stmt, scope)

    def _visit(self, node: ast.AST, scope: _Scope) -> None:
        """Records the calls in node, reading only the decorators and defaults of a nested function."""
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
        """Adds the edge for a call, or lists the call as unresolved, then notes any file it uses."""
        target = self._resolve_call(call.func, scope)
        if target:
            self.resolved[id(call)] = target
            self.map.calls[scope.func].add(target)
        else:
            self.map.unresolved.append(UnresolvedCall(scope.func, ast.unparse(call.func), scope.file, call.lineno))
        self._note_file_use(call, scope)

    # file names

    def _assigns(self, key: str) -> dict[str, list[ast.expr | None]]:
        """Returns the values assigned to each name in a scope, with None for a value that is not an expression."""
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

    def _find_names(self, expr: ast.expr, scope: _Scope, depth: int) -> tuple[list[str], str | None]:
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
        values = self._assigns(f"{module}.{MODULE_PART}").get(constant) if module in self.trees else None
        if values and len(values) == 1 and values[0] is not None and depth < 3:
            return self._find_names(values[0], self._scope_for(f"{module}.{MODULE_PART}"), depth + 1)
        return [], "expression"

    def _note_file_use(self, call: ast.Call, scope: _Scope) -> None:
        """Records the file that a call reads or writes, if the call is one of the known kinds."""
        func = call.func
        name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
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
            given = call.args[at] if len(call.args) > at else next((item.value for item in call.keywords if item.arg == "mode"), None)
            written = isinstance(given, ast.Constant) and isinstance(given.value, str) and any(char in given.value for char in "wax+")
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
                self.map.file_uses.append(FileUse(scope.func, file_name, mode, text, scope.file, call.lineno))
        elif not quiet:
            self.map.file_uses.append(FileUse(scope.func, None, mode, text, scope.file, call.lineno, reason or "expression"))

    # entry points

    def _find_entries(self) -> None:
        """Marks the command functions of every cli module and the steps registered on a rooster AgentGraph."""
        entries: set[str] = set()
        for module in self.trees:
            main = f"{module}.main"
            if module.rpartition(".")[2] != "cli" or main not in self.infos:
                continue
            entries.add(main)
            for node in _shallow(self.infos[main].node.body):
                if isinstance(node, ast.If) and any(isinstance(item, ast.Attribute) and item.attr == "command" for item in ast.walk(node.test)):
                    for inner in _shallow(node.body):
                        if isinstance(inner, ast.Call) and self.resolved.get(id(inner)) in self.infos:
                            entries.add(self.resolved[id(inner)])
        for name, info in self.infos.items():
            outer = self._scope_for(info.parent or f"{info.module}.{MODULE_PART}").scopes
            for decorator in info.node.decorator_list:
                if not (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and decorator.func.attr == "step"):
                    continue
                kind = self._var_type(decorator.func.value.id, outer) if isinstance(decorator.func.value, ast.Name) else None
                if kind and kind.startswith("rooster.") and kind.endswith(".AgentGraph"):
                    entries.add(name)
        self.map.entry_points = entries
        for name in entries:
            self.map.functions[name].is_entry = True


def build_map(root: Path) -> PathwayMap:
    """Returns the pathway map of the packages under root/src."""
    return _Builder(Path(root)).build()


def upstream_paths(pathway_map: PathwayMap, name: str, limit: int = PATH_LIMIT) -> tuple[list[list[str]], int]:
    """Returns up to limit call paths from an entry point down to name, and how many more there were.

    A path lists the entry point first and name last, and no function appears twice in it. A path
    stops at the first entry point it reaches, so an entry point's own path is just itself.
    """
    found: list[list[str]] = []

    def climb(node: str, path: list[str]) -> None:
        if node in pathway_map.entry_points:
            found.append(path[::-1])
            return
        for caller in sorted(pathway_map.callers.get(node, ())):
            if caller not in path:
                climb(caller, path + [caller])

    climb(name, [name])
    found.sort()
    return found[:limit], max(0, len(found) - limit)


def downstream(pathway_map: PathwayMap, name: str) -> list[str]:
    """Returns every function that name reaches through calls, leaves included, without name itself."""
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
    """Returns the files that name reads and writes, who writes what it reads, and who reads what it writes.

    The 'undetermined' list holds the file calls in name whose file name could not be read statically.
    """
    own = [use for use in pathway_map.file_uses if use.function == name]
    reads = sorted({use.file_name for use in own if use.mode == "read" and use.file_name})
    writes = sorted({use.file_name for use in own if use.mode == "write" and use.file_name})

    def users(files: list[str], mode: str) -> dict[str, list[str]]:
        return {
            file_name: sorted({use.function for use in pathway_map.file_uses if use.mode == mode and use.file_name == file_name})
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


def describe(pathway_map: PathwayMap, names: list[str]) -> dict:
    """Returns the upstream paths, downstream functions, file links and unresolved calls of each named function."""
    described = {}
    unknown = []
    for name in names:
        if name not in pathway_map.functions:
            unknown.append(name)
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
    return {"functions": described, "unknown": unknown}


def functions_of_diff(pathway_map: PathwayMap, diff_text: str) -> list[str]:
    """Returns the sorted functions in src that enclose a changed line of a unified diff.

    An added line counts at its line in the new file. A removed line counts at the line of the new
    file that it sits before. A changed line outside any function belongs to the module's <module> node.
    """
    changed: set[str] = set()
    module: str | None = None
    lines = diff_text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        index += 1
        if line.startswith("+++ "):
            path = line[4:].split("\t")[0].strip()
            module = pathway_map.file_modules.get(path[2:]) if path.startswith("b/") else None
            continue
        match = _HUNK.match(line)
        if not match:
            continue
        old_left = int(match.group(1) or 1)
        new_line = int(match.group(2))
        new_left = int(match.group(3) or 1)
        while (old_left > 0 or new_left > 0) and index < len(lines):
            text = lines[index]
            index += 1
            if text.startswith("\\"):
                continue
            marker = text[:1]
            if marker != "+":
                old_left -= 1
            if marker != "-":
                new_left -= 1
            if marker == "+" and module:
                changed.add(_enclosing(pathway_map, module, new_line))
            elif marker == "-" and module:
                changed.add(_removed_in(pathway_map, module, new_line, text[1:]))
            if marker != "-":
                new_line += 1
    return sorted(changed)


def _removed_in(pathway_map: PathwayMap, module: str, line: int, text: str) -> str:
    """Returns the function that a removed line was in, given the new-file line it sits before.

    An indented line that sat at the end of a function now sits before whatever follows that function,
    so when the next line starts a node or is outside every function, the line before it is used instead.
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
        if node.module == module and not node.name.endswith(f".{MODULE_PART}") and node.start <= line <= node.end:
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
    parser = argparse.ArgumentParser(prog="python -m ratchet.pathways", description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True, help="the repository that holds src")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--functions", help="comma separated function names, as package.module.qualname")
    group.add_argument("--diff-file", type=Path, help="a unified diff to map onto functions")
    args = parser.parse_args(argv)
    pathway_map = build_map(args.root)
    if args.functions is not None:
        report = describe(pathway_map, [item.strip() for item in args.functions.split(",") if item.strip()])
    else:
        changed = functions_of_diff(pathway_map, args.diff_file.read_text(encoding="utf-8"))
        report = {"changed_functions": changed, **describe(pathway_map, changed)}
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
