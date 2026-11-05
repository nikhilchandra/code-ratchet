"""Checks the pathway map on three source forms: relative imports, calls on a call result, and open() with a
mode keyword."""

from __future__ import annotations

from pathlib import Path

from ratchet.pathways import build_map, downstream


def write_tree(root: Path, files: dict[str, str]) -> Path:
    """Writes the files under root/src, with an empty app/__init__.py, and returns root."""
    for name, text in {"app/__init__.py": "", **files}.items():
        path = root / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_a_relative_import_resolves_to_the_sibling_module(tmp_path: Path) -> None:
    """Proves a call through a from-dot import reaches the function in the sibling module."""
    root = write_tree(tmp_path, {
        "app/helper.py": "def assist():\n    return 1\n",
        "app/main.py": "from .helper import assist\n\n\ndef run():\n    return assist()\n",
    })
    pathway_map = build_map(root)
    assert "app.helper.assist" in downstream(pathway_map, "app.main.run")


def test_a_call_on_a_call_result_is_left_unresolved(tmp_path: Path) -> None:
    """Proves a call whose callee is neither a name nor an attribute does not stop the map."""
    root = write_tree(tmp_path, {
        "app/mod.py": "def make():\n    return make\n\n\ndef run():\n    return make()()\n",
    })
    pathway_map = build_map(root)
    assert "app.mod.make" in pathway_map.functions


def test_open_reads_its_mode_from_a_keyword(tmp_path: Path) -> None:
    """Proves open(path, mode='w') is a write and open(path, mode='r') is a read."""
    root = write_tree(tmp_path, {
        "app/mod.py": (
            "def save(text):\n    with open('out.txt', mode='w') as handle:\n"
            "        handle.write(text)\n\n\n"
            "def load():\n    with open('out.txt', mode='r') as handle:\n"
            "        return handle.read()\n"
        ),
    })
    modes = {item.mode for item in build_map(root).file_uses}
    assert modes == {"read", "write"}
