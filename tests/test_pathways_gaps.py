"""Checks the pathway map on a broken file, a re-exported name and a json dump."""

from __future__ import annotations

from pathlib import Path

from ratchet.pathways import build_map, downstream, file_links


def make_tree(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / "src" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def test_a_file_that_does_not_parse_is_skipped(tmp_path):
    make_tree(tmp_path, {"app/__init__.py": "", "app/good.py": "def ok():\n    return 1\n", "app/bad.py": "def (:\n"})
    mapped = build_map(tmp_path)
    assert "app.good.ok" in mapped.functions
    assert "app.bad" not in mapped.module_files or not any(name.startswith("app.bad.") for name in mapped.functions)


def test_a_re_exported_name_resolves_through_its_package(tmp_path):
    make_tree(
        tmp_path,
        {
            "app/__init__.py": "from app.impl import work\n",
            "app/impl.py": "def work():\n    return 1\n",
            "app/use.py": "import app\n\n\ndef go():\n    return app.work()\n",
        },
    )
    mapped = build_map(tmp_path)
    assert "app.impl.work" in downstream(mapped, "app.use.go")


def test_a_json_dump_writer_is_linked_to_a_reader(tmp_path):
    make_tree(
        tmp_path,
        {
            "app/__init__.py": "",
            "app/io.py": (
                "import json\n\n\n"
                "def put(data):\n"
                "    with open('state.json', 'w') as handle:\n"
                "        json.dump(data, handle)\n\n\n"
                "def get(path):\n"
                "    return json.loads(path.read_text('state.json'))\n"
            ),
        },
    )
    mapped = build_map(tmp_path)
    assert "state.json" in file_links(mapped, "app.io.put")["writes"]
