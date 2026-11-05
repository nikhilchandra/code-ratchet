"""Checks that upstream_paths stays fast on a wide call graph and stays exact on graphs with cycles.

A wide graph here has 4 entry points and 11 more layers of 4 functions, where every function
calls all 4 functions of the next layer. That gives 4**12 paths down to the target. Listing every
one of them would take minutes, so the timed calls run in a child process that is killed when it
runs too long.
"""

from __future__ import annotations

import itertools
import json
import random
import subprocess
import sys
from pathlib import Path

import pytest

from ratchet.pathways import PATH_LIMIT, FunctionNode, PathwayMap, upstream_paths

# The function that every path in the wide graph leads to.
TARGET = "app.t.target"

# The number of layers in the wide graph, the first of which holds the entry points.
LAYERS = 12

# The number of functions in each layer of the wide graph.
WIDTH = 4

# The number of paths from an entry point down to TARGET in the wide graph.
TOTAL = WIDTH**LAYERS

# The most seconds that one call on the wide graph may take.
BOUND = 2.0

# The most seconds that the child process may run before it is killed.
KILL_AFTER = 30

# The child process loads this file and prints what one wide-graph call returns, as JSON.
CHILD = """\
import json, runpy, sys, time
from pathlib import Path
from ratchet.pathways import describe, upstream_paths
space = runpy.run_path(sys.argv[1])
pathway_map = space["wide_map"](Path(sys.argv[3]))
started = time.perf_counter()
if sys.argv[2] == "upstream":
    paths, more = upstream_paths(pathway_map, space["TARGET"])
else:
    report = describe(pathway_map, [space["TARGET"]])["functions"][space["TARGET"]]
    paths, more = report["upstream_paths"], report["more_paths"]
print(json.dumps({"paths": paths, "more": more, "seconds": time.perf_counter() - started}))
"""


def layer_names(layer: int) -> list[str]:
    """Returns the sorted names of the functions in one layer of the wide graph."""
    return [f"app.l{layer:02d}.f{index}" for index in range(WIDTH)]


def link(pathway_map: PathwayMap, caller: str, callee: str) -> None:
    """Records that caller calls callee, in both the calls and the callers of the map."""
    pathway_map.calls.setdefault(caller, set()).add(callee)
    pathway_map.callers.setdefault(callee, set()).add(caller)


def add_function(pathway_map: PathwayMap, name: str) -> None:
    """Adds a FunctionNode for name to the map, as though it were written in app/<module>.py."""
    module = name.rpartition(".")[0]
    pathway_map.functions[name] = FunctionNode(
        name=name, module=module, file=pathway_map.root / f"{module}.py", start=1, end=2
    )


def wide_map(root: Path) -> PathwayMap:
    """Returns the wide graph, whose first layer holds the entry points and whose last layer
    calls TARGET."""
    pathway_map = PathwayMap(root=root)
    for layer in range(LAYERS):
        for name in layer_names(layer):
            add_function(pathway_map, name)
            if layer + 1 < LAYERS:
                for callee in layer_names(layer + 1):
                    link(pathway_map, name, callee)
            else:
                link(pathway_map, name, TARGET)
    add_function(pathway_map, TARGET)
    pathway_map.entry_points = set(layer_names(0))
    return pathway_map


def smallest_wide_paths() -> list[list[str]]:
    """Returns the PATH_LIMIT paths of the wide graph that come first in sorted order."""
    every = itertools.product(*(layer_names(layer) for layer in range(LAYERS)))
    return [[*path, TARGET] for path in itertools.islice(every, PATH_LIMIT)]


def run_wide(tmp_path: Path, call: str) -> dict:
    """Runs one call on the wide graph in a child process and returns its paths, more count and
    seconds.

    Given a parameter 'call', the child runs upstream_paths when call is 'upstream' and describe
    when call is 'describe'. The test fails when the child runs longer than KILL_AFTER seconds.
    """
    try:
        done = subprocess.run(
            [sys.executable, "-c", CHILD, __file__, call, str(tmp_path)],
            capture_output=True,
            text=True,
            timeout=KILL_AFTER,
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{call} on the wide graph ran longer than {KILL_AFTER} seconds")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def climb_then_sort(
    pathway_map: PathwayMap, name: str, limit: int
) -> tuple[list[list[str]], int]:
    """Returns the paths down to name by listing every one of them, then sorting and cutting.

    The climb goes up through callers, never visits a function twice on one path, and stops at
    the first entry point it reaches.
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


def small_map(
    tmp_path: Path, edges: list[tuple[str, str]], entries: set[str]
) -> PathwayMap:
    """Returns a map holding the given caller and callee pairs, with entries as its entry points."""
    pathway_map = PathwayMap(root=tmp_path)
    for caller, callee in edges:
        link(pathway_map, caller, callee)
    pathway_map.entry_points = set(entries)
    return pathway_map


# The hand-made graphs with recursion, as the edges, the entry points and the name to climb from.
CYCLIC_GRAPHS = {
    "two-node cycle on the path": (
        [("e", "a"), ("a", "b"), ("b", "a"), ("b", "t"), ("f", "b")],
        {"e", "f"},
        "t",
    ),
    "self-loop": (
        [("e", "a"), ("a", "a"), ("a", "t"), ("e", "t"), ("g", "a")],
        {"e", "g"},
        "t",
    ),
    "entry point inside a cycle": (
        [("e", "a"), ("a", "e"), ("a", "t"), ("x", "e"), ("y", "x"), ("y", "a"), ("z", "y")],
        {"e", "z"},
        "t",
    ),
    "name is an entry point": (
        [("e", "t"), ("t", "a"), ("a", "t"), ("f", "a")],
        {"e", "t", "f"},
        "t",
    ),
    "name is an entry point with no callers": (
        [("t", "a")],
        {"t"},
        "t",
    ),
    "no entry point above name": (
        [("a", "b"), ("b", "a"), ("b", "t")],
        {"z"},
        "t",
    ),
    "two cycles sharing a node": (
        [
            ("e", "a"), ("a", "b"), ("b", "c"), ("c", "a"), ("c", "d"), ("d", "c"),
            ("d", "t"), ("b", "t"), ("f", "d"), ("a", "t"),
        ],
        {"e", "f"},
        "t",
    ),
}

# The number of seeded random graphs that are compared with the full climb.
RANDOM_SEEDS = 40

# The limits that each graph is compared at, from none at all to more than any graph has.
LIMITS = [0, 1, 3, 1000]


def random_graphs() -> list[tuple[list[tuple[str, str]], set[str], str]]:
    """Returns seeded random graphs of 8 functions, as the edges, the entry points and the name."""
    graphs = []
    for seed in range(RANDOM_SEEDS):
        chooser = random.Random(seed)
        nodes = [f"n{index}" for index in range(8)]
        edges = [
            (caller, callee)
            for caller in nodes
            for callee in nodes
            if chooser.random() < 0.3
        ]
        entries = set(chooser.sample(nodes, chooser.randint(1, 3)))
        graphs.append((edges, entries, chooser.choice(nodes)))
    return graphs


def test_a_wide_layered_graph_gives_fifty_sorted_paths_quickly(tmp_path: Path) -> None:
    """Proves upstream_paths on 4**12 paths returns the 50 smallest and counts the rest, in time."""
    result = run_wide(tmp_path, "upstream")

    assert result["paths"] == smallest_wide_paths()
    assert result["more"] == TOTAL - PATH_LIMIT
    assert result["seconds"] < BOUND


@pytest.mark.parametrize(
    ("edges", "entries", "name"),
    [*CYCLIC_GRAPHS.values(), *random_graphs()],
    ids=[*CYCLIC_GRAPHS, *(f"random seed {seed}" for seed in range(RANDOM_SEEDS))],
)
def test_upstream_paths_match_the_full_climb_on_graphs_with_cycles(
    tmp_path: Path, edges: list[tuple[str, str]], entries: set[str], name: str
) -> None:
    """Proves the paths and the more count equal those of a full climb, on hand-made and random
    graphs with recursion."""
    pathway_map = small_map(tmp_path, edges, entries)

    for limit in LIMITS:
        assert upstream_paths(pathway_map, name, limit) == climb_then_sort(pathway_map, name, limit)
    assert upstream_paths(pathway_map, name) == climb_then_sort(pathway_map, name, PATH_LIMIT)


def test_describe_reports_the_full_count_for_a_wide_graph(tmp_path: Path) -> None:
    """Proves describe lists 50 upstream paths and the count of the other 4**12 - 50, in time."""
    result = run_wide(tmp_path, "describe")

    assert result["paths"] == smallest_wide_paths()
    assert result["more"] == TOTAL - PATH_LIMIT
    assert result["seconds"] < BOUND
