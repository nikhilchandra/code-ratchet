"""Tests that a line removed from a committed file is mapped through that committed file, so a
deleted function or module is named rather than its neighbor."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ratchet.git import Repo
from ratchet.pathways import build_map, functions_of_diff


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


def _commit(root: Path, files: dict[str, bytes]) -> None:
    """Makes a repository in 'root' and commits 'files', a map of each posix path relative to
    'root' to its bytes, together with an empty src/app/__init__.py."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "ratchet@example.com")
    _git(root, "config", "user.name", "ratchet")
    _git(root, "config", "core.autocrlf", "false")
    for path, data in {"src/app/__init__.py": b"", **files}.items():
        _write(root, path, data)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "start")


def _write(root: Path, path: str, data: bytes) -> None:
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(data)


def _changed(root: Path) -> list[str]:
    """Returns the functions that the working tree's diff against HEAD changes."""
    return functions_of_diff(build_map(root), Repo(root).diff())


def test_deleting_a_function_in_the_middle_names_it_not_its_neighbour(tmp_path: Path) -> None:
    """Proves that deleting b() from between a() and c() names b, and the blank lines before it
    as module-level code, but not the unchanged c."""
    _commit(tmp_path, {"src/app/mod.py": b"def a():\n    return 1\n\n\ndef b():\n    return 2\n"
                                         b"\n\ndef c():\n    return 3\n"})
    _write(tmp_path, "src/app/mod.py", b"def a():\n    return 1\n\n\ndef c():\n    return 3\n")

    assert _changed(tmp_path) == ["app.mod.<module>", "app.mod.b"]


def test_deleting_the_last_function_names_it_not_the_one_before(tmp_path: Path) -> None:
    """Proves that deleting the last function of a file names it, and not the function before."""
    _commit(tmp_path, {"src/app/mod.py": b"def a():\n    return 1\n\n\ndef b():\n    return 2\n"})
    _write(tmp_path, "src/app/mod.py", b"def a():\n    return 1\n")

    assert _changed(tmp_path) == ["app.mod.<module>", "app.mod.b"]


def test_a_deleted_module_names_every_function_it_held(tmp_path: Path) -> None:
    """Proves that deleting a module names its <module> node and every function it held."""
    _commit(tmp_path, {"src/app/old.py": b"def gone():\n    return 1\n"})
    (tmp_path / "src" / "app" / "old.py").unlink()

    assert _changed(tmp_path) == ["app.old.<module>", "app.old.gone"]


def test_a_deleted_package_init_is_named_by_its_package(tmp_path: Path) -> None:
    """Proves that a deleted src/app/pkg/__init__.py is named app.pkg, the way the map names a
    package, and not app.pkg.__init__."""
    _commit(tmp_path, {"src/app/pkg/__init__.py": b"def f():\n    return 1\n"})
    (tmp_path / "src" / "app" / "pkg" / "__init__.py").unlink()

    assert _changed(tmp_path) == ["app.pkg.<module>", "app.pkg.f"]


@pytest.mark.filterwarnings("error")
def test_a_committed_file_that_is_not_utf8_falls_back_to_the_new_file(tmp_path: Path) -> None:
    """Proves that when the committed file is not UTF-8, a removed line is mapped through the new
    file instead, and that reading the committed file raises no warning."""
    _commit(tmp_path, {"src/app/mod.py": b"# caf\xe9\ndef a():\n    x = 1\n    return x\n"})
    _write(tmp_path, "src/app/mod.py", b"# cafe\ndef a():\n    x = 1\n")
    diff = ("--- a/src/app/mod.py\n+++ b/src/app/mod.py\n"
            "@@ -2,3 +2,2 @@\n def a():\n     x = 1\n-    return x\n")

    assert functions_of_diff(build_map(tmp_path), diff) == ["app.mod.a"]


def test_a_removed_line_inside_a_function_that_remains_names_that_function(tmp_path: Path) -> None:
    """Proves that removing the last line of a() names a, and not b() or the module."""
    _commit(tmp_path, {"src/app/mod.py": b"def a():\n    x = 1\n    return x\n\n\ndef b():\n"
                                         b"    return 2\n"})
    _write(tmp_path, "src/app/mod.py", b"def a():\n    x = 1\n\n\ndef b():\n    return 2\n")

    assert _changed(tmp_path) == ["app.mod.a"]


def test_a_changed_function_is_named_when_the_diff_has_no_prefix_config(tmp_path: Path) -> None:
    """Proves that an edited function is named when the repository's config sets diff.noprefix,
    which would otherwise drop the a/ and b/ prefixes from the diff's file headers."""
    _commit(tmp_path, {"src/app/mod.py": b"def a():\n    return 1\n\n\ndef b():\n    return 2\n"})
    _git(tmp_path, "config", "diff.noprefix", "true")
    _write(tmp_path, "src/app/mod.py", b"def a():\n    return 1\n\n\ndef b():\n    return 3\n")

    assert _changed(tmp_path) == ["app.mod.b"]
