"""Checks the pathway map's calls, entry points, paths, file links and diff mapping, on small packages."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ratchet.pathways import (
    build_map,
    describe,
    downstream,
    file_links,
    functions_of_diff,
    main,
    upstream_paths,
)

# The keys that describe() gives for each function it knows.
FUNCTION_KEYS = {"upstream_paths", "more_paths", "downstream", "file_links", "unresolved"}

# The scratch module that the diff tests change, with its line numbers in the comments below.
MOD = '''\
"""Holds a few functions."""
import functools

LIMIT = 3


def first():
    y = 1
    return y


def second():
    return 2


@functools.cache
def third():
    return 3
'''

# A diff that changes line 4 (module level), adds line 8 (in first) and removes the line before
# line 13 (in second). It also changes a file outside src and deletes a file. {SPACE} stands for
# the single space that starts a blank context line.
DIFF = """\
diff --git a/src/app/mod.py b/src/app/mod.py
--- a/src/app/mod.py
+++ b/src/app/mod.py
@@ -2,4 +2,4 @@
 import functools
{SPACE}
-LIMIT = 2
+LIMIT = 3
{SPACE}
@@ -7,2 +7,3 @@
 def first():
+    y = 1
     return y
@@ -11,3 +12,2 @@
 def second():
-    x = 0
     return 2
diff --git a/tests/test_mod.py b/tests/test_mod.py
--- a/tests/test_mod.py
+++ b/tests/test_mod.py
@@ -1,1 +1,2 @@
 x = 1
+y = 2
diff --git a/src/app/gone.py b/src/app/gone.py
--- a/src/app/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def gone():
-    return 0
""".replace("{SPACE}", " ")

# A diff that changes only the decorator line of third, line 16.
DECORATOR_DIFF = """\
--- a/src/app/mod.py
+++ b/src/app/mod.py
@@ -16,3 +16,3 @@
-@functools.lru_cache
+@functools.cache
 def third():
     return 3
"""


def make_tree(root: Path, files: dict[str, str]) -> Path:
    """Writes the files under root/src, with an empty app/__init__.py, and returns root."""
    for name, text in {"app/__init__.py": "", **files}.items():
        path = root / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_a_cycle_gives_finite_paths_and_downstream(tmp_path: Path) -> None:
    """Proves a calls b and b calls a gives simple upstream paths and a finite downstream."""
    root = make_tree(tmp_path, {
        "app/cli.py": "from app.loop import a\n\n\ndef main(argv=None):\n    return a()\n",
        "app/loop.py": "def a():\n    return b()\n\n\ndef b():\n    return a()\n",
    })
    pathway_map = build_map(root)

    assert upstream_paths(pathway_map, "app.loop.b") == ([["app.cli.main", "app.loop.a", "app.loop.b"]], 0)
    assert upstream_paths(pathway_map, "app.loop.a") == ([["app.cli.main", "app.loop.a"]], 0)
    assert "app.loop.b" in downstream(pathway_map, "app.loop.a")
    assert {"app.loop.a", "app.loop.b"} <= set(downstream(pathway_map, "app.cli.main"))


def test_self_cls_and_inherited_methods_resolve(tmp_path: Path) -> None:
    """Proves self.fill(), cls.make() and self.base_method() from a base class in src resolve."""
    root = make_tree(tmp_path, {
        "app/base.py": "class Base:\n    def base_method(self):\n        return 1\n",
        "app/box.py": (
            "from app.base import Base\n\n\n"
            "class Box(Base):\n"
            "    def open(self):\n"
            "        self.base_method()\n"
            "        return self.fill()\n\n"
            "    def fill(self):\n"
            "        return 2\n\n"
            "    @classmethod\n"
            "    def build(cls):\n"
            "        return cls.make()\n\n"
            "    @classmethod\n"
            "    def make(cls):\n"
            "        return 3\n"
        ),
    })
    pathway_map = build_map(root)

    assert {"app.box.Box.fill", "app.base.Base.base_method"} <= pathway_map.calls["app.box.Box.open"]
    assert "app.box.Box.make" in pathway_map.calls["app.box.Box.build"]
    assert describe(pathway_map, ["app.box.Box.open"])["functions"]["app.box.Box.open"]["unresolved"] == []


def test_imported_aliases_resolve_and_outside_calls_are_leaves(tmp_path: Path) -> None:
    """Proves aliased imports resolve into src, json.loads is a leaf and len() is builtins.len."""
    root = make_tree(tmp_path, {
        "app/util.py": "def helper():\n    return 1\n",
        "app/use.py": (
            "import json as j\n"
            "import app.util as u\n"
            "from app.util import helper as h\n\n\n"
            "def run(x):\n"
            "    h()\n"
            "    u.helper()\n"
            "    size = len(x)\n"
            "    return j.loads(x), size\n"
        ),
    })
    pathway_map = build_map(root)

    assert {"app.util.helper", "json.loads", "builtins.len"} <= pathway_map.calls["app.use.run"]
    assert describe(pathway_map, ["app.use.run"])["functions"]["app.use.run"]["unresolved"] == []


def test_an_unresolved_call_is_listed_with_file_and_line(tmp_path: Path) -> None:
    """Proves obj.thing() on a local variable is listed with its function, call, file and line."""
    root = make_tree(tmp_path, {
        "app/poke.py": (
            "def poke():\n"
            "    obj = object()\n"
            "    obj.thing()\n\n\n"
            "def outer():\n"
            "    return poke()\n"
        ),
    })
    pathway_map = build_map(root)
    found = describe(pathway_map, ["app.poke.poke", "app.poke.outer"])["functions"]

    (entry,) = found["app.poke.poke"]["unresolved"]
    assert entry["function"] == "app.poke.poke"
    assert entry["call"] in ("obj.thing", "obj.thing()")
    assert entry["file"] == "src/app/poke.py"
    assert entry["line"] == 3
    assert entry in found["app.poke.outer"]["unresolved"]


def test_a_writer_and_reader_are_linked_through_a_file_name(tmp_path: Path) -> None:
    """Proves writers and readers of notes.json link both ways, and an f-string is undetermined."""
    root = make_tree(tmp_path, {
        "app/store.py": (
            "def save_notes(folder):\n"
            "    (folder / 'notes.json').write_text('The owner saved these notes.', encoding='utf-8')\n\n\n"
            "def save_dated(folder, day):\n"
            "    (folder / f'notes-{day}.json').write_text('x', encoding='utf-8')\n"
        ),
        "app/show.py": (
            "NOTES = 'notes.json'\n\n\n"
            "def load_notes(folder):\n"
            "    path = folder / NOTES\n"
            "    return path.read_text(encoding='utf-8')\n"
        ),
        "app/peek.py": (
            "from app.show import NOTES\n\n\n"
            "def peek(folder):\n"
            "    return (folder / NOTES).read_text(encoding='utf-8')\n"
        ),
    })
    pathway_map = build_map(root)

    saved = file_links(pathway_map, "app.store.save_notes")
    assert saved["writes"] == ["notes.json"]
    assert {"app.show.load_notes", "app.peek.peek"} <= set(saved["read_by"]["notes.json"])
    loaded = file_links(pathway_map, "app.show.load_notes")
    assert loaded["reads"] == ["notes.json"]
    assert "app.store.save_notes" in loaded["written_by"]["notes.json"]
    assert "app.store.save_notes" in file_links(pathway_map, "app.peek.peek")["written_by"]["notes.json"]
    dated = file_links(pathway_map, "app.store.save_dated")
    assert dated["writes"] == []
    assert [entry["reason"] for entry in dated["undetermined"]] == ["f-string"]
    described = describe(pathway_map, ["app.store.save_notes"])["functions"]["app.store.save_notes"]
    assert described["file_links"] == saved


def test_a_diff_maps_changed_lines_to_functions(tmp_path: Path) -> None:
    """Proves added, removed and module-level lines map to their functions, and other files are left out."""
    root = make_tree(tmp_path, {"app/mod.py": MOD})
    pathway_map = build_map(root)

    assert functions_of_diff(pathway_map, DIFF) == ["app.mod.<module>", "app.mod.first", "app.mod.second"]
    assert functions_of_diff(pathway_map, DECORATOR_DIFF) == ["app.mod.third"]


def test_entry_points_are_cli_commands_and_graph_steps(tmp_path: Path) -> None:
    """Proves the entry points are cli main, its command functions and steps on a rooster AgentGraph."""
    root = make_tree(tmp_path, {
        "app/cli.py": (
            "import argparse\n\n\n"
            "def main(argv=None):\n"
            "    args = parse(argv)\n"
            "    if args.command == 'x':\n"
            "        return run_x(args)\n"
            "    return 0\n\n\n"
            "def parse(argv):\n"
            "    return argparse.ArgumentParser().parse_args(argv)\n\n\n"
            "def run_x(args):\n"
            "    return helper()\n\n\n"
            "def helper():\n"
            "    return 1\n"
        ),
        "app/flow.py": (
            "from rooster import AgentGraph\n\n\n"
            "class Board:\n"
            "    def step(self, name, next=()):\n"
            "        return lambda function: function\n\n\n"
            "def build(agents):\n"
            "    graph = AgentGraph('flow', agents, start='go')\n\n"
            "    @graph.step('go', next=(None,))\n"
            "    def go(job):\n"
            "        return None\n\n"
            "    return graph\n\n\n"
            "def build_board():\n"
            "    board = Board()\n\n"
            "    @board.step('stay', next=(None,))\n"
            "    def stay(job):\n"
            "        return None\n\n"
            "    return board\n"
        ),
    })
    pathway_map = build_map(root)

    assert pathway_map.entry_points == {"app.cli.main", "app.cli.run_x", "app.flow.build.go"}
    assert upstream_paths(pathway_map, "app.cli.helper") == ([["app.cli.run_x", "app.cli.helper"]], 0)
    assert upstream_paths(pathway_map, "app.flow.build.go") == ([["app.flow.build.go"]], 0)


def test_upstream_paths_are_capped_at_fifty(tmp_path: Path) -> None:
    """Proves 64 paths keep the first 50 in sorted order and count the other 14."""
    steps = "".join(
        f"    @graph.step('s{i}', next=(None,))\n"
        f"    def s{i}(job):\n"
        + "".join(f"        m{j}()\n" for j in range(8))
        + "\n"
        for i in range(8)
    )
    root = make_tree(tmp_path, {
        "app/mid.py": "".join(f"def m{j}():\n    return target()\n\n\n" for j in range(8))
        + "def target():\n    return 0\n",
        "app/fan.py": (
            "from rooster import AgentGraph\n\n"
            f"from app.mid import {', '.join(f'm{j}' for j in range(8))}\n\n\n"
            "def build():\n"
            "    graph = AgentGraph('fan', {}, start='s0')\n\n"
            f"{steps}"
            "    return graph\n"
        ),
    })
    pathway_map = build_map(root)
    every = sorted(
        [f"app.fan.build.s{i}", f"app.mid.m{j}", "app.mid.target"] for i in range(8) for j in range(8))

    assert upstream_paths(pathway_map, "app.mid.target") == (every[:50], 14)
    assert upstream_paths(pathway_map, "app.mid.target", limit=100) == (every, 0)
    described = describe(pathway_map, ["app.mid.target"])["functions"]["app.mid.target"]
    assert described["upstream_paths"] == every[:50]
    assert described["more_paths"] == 14


def test_the_cli_prints_json_for_functions_and_for_a_diff(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Proves --functions and --diff-file print the described functions as JSON and return 0."""
    root = make_tree(tmp_path, {
        "app/util.py": "def helper():\n    return 1\n",
        "app/mod.py": MOD,
    })

    assert main(["--root", str(root), "--functions", "app.util.helper,app.nope"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert set(printed["functions"]) == {"app.util.helper"}
    assert set(printed["functions"]["app.util.helper"]) == FUNCTION_KEYS
    assert printed["unknown"] == ["app.nope"]

    diff_file = tmp_path / "change.diff"
    diff_file.write_text(DIFF, encoding="utf-8")
    assert main(["--root", str(root), "--diff-file", str(diff_file)]) == 0
    printed = json.loads(capsys.readouterr().out)
    changed = ["app.mod.<module>", "app.mod.first", "app.mod.second"]
    assert printed["changed_functions"] == changed
    assert set(printed["functions"]) == set(changed)
    assert all(set(found) == FUNCTION_KEYS for found in printed["functions"].values())


def test_the_module_runs_as_a_script(tmp_path: Path) -> None:
    """Proves `python -m ratchet.pathways` exits 0 and prints JSON."""
    root = make_tree(tmp_path, {"app/util.py": "def helper():\n    return 1\n"})

    done = subprocess.run(
        [sys.executable, "-m", "ratchet.pathways", "--root", str(root), "--functions", "app.util.helper"],
        capture_output=True, text=True, encoding="utf-8")

    assert done.returncode == 0, done.stderr
    assert set(json.loads(done.stdout)["functions"]["app.util.helper"]) == FUNCTION_KEYS


def test_done_ids_in_ratchet_has_a_path_from_a_cli_command() -> None:
    """Proves done_ids in ratchet itself is reached from a CLI command and from the draft and publish steps."""
    pathway_map = build_map(Path(__file__).parents[1])
    name = "ratchet.audit.audit_graph.done_ids"
    described = describe(pathway_map, [name])
    paths, more = upstream_paths(pathway_map, name, limit=10_000)

    assert name in described["functions"]
    assert more == 0
    assert all(path[-1] == name for path in paths)
    from_cli = [path for path in paths if path[0] in ("ratchet.cli.run_audit", "ratchet.cli.main")]
    assert any("ratchet.audit.audit_run._approve_draft" in path for path in from_cli)
    assert ["ratchet.audit.audit_graph.build_audit_graph.draft", name] in paths
    assert ["ratchet.audit.audit_graph.build_audit_graph.publish", name] in paths
