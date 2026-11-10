"""Checks the shop example: its own tests pass, ratchet init keeps its rules and settings, and
the code checker finds the problem planted against its rules. No agent is called."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from ratchet.architecture import check_architecture
from ratchet.audit.settings import load_settings
from ratchet.init_project import init_project
from ratchet.project import load_project
from ratchet.target_design import is_template, read_target_design

# the example's folder
SHOP = Path(__file__).resolve().parent.parent / "examples" / "shop"


def copy_and_commit(tmp_path: Path) -> Path:
    """Copies the example into tmp_path, makes it a git repository and commits it, as its
    README says to do."""
    root = tmp_path / "shop"
    shutil.copytree(SHOP, root)

    def git(*arguments: str) -> str:
        return subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True,
                              text=True).stdout

    git("init", "-q", "-b", "main")
    git("config", "user.email", "shop@example.com")
    git("config", "user.name", "shop")
    git("add", "-A")
    git("commit", "-q", "-m", "start")
    return root


def test_the_example_passes_its_own_tests(tmp_path: Path) -> None:
    """Proves the starting project's tests pass, which ratchet needs before it starts."""
    root = copy_and_commit(tmp_path)

    tests = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                           cwd=root, capture_output=True, text=True)

    assert tests.returncode == 0, tests.stdout


def test_init_keeps_the_examples_rules_and_leaves_them_out_of_the_project(tmp_path: Path) -> None:
    """Proves the README's steps work: the commit leaves .ratchet out, and ratchet init keeps the
    example's filled-in rules and settings."""
    root = copy_and_commit(tmp_path)
    tracked = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True,
                             text=True).stdout.splitlines()

    init_project(root, assume_yes=False, confirm=None)
    project = load_project(root, commands=["audit"])

    assert not any(path.startswith(".ratchet") for path in tracked)
    rules = (root / ".ratchet" / "rules.md").read_text(encoding="utf-8")
    assert not is_template(rules)
    assert load_settings(project).auditor.model == "sonnet"


def test_the_setup_script_makes_a_copy_ready_for_ratchet(tmp_path: Path) -> None:
    """Proves try_it.py copies the example, commits it, runs init, and refuses a folder that
    already exists."""
    target = tmp_path / "shop"
    script = [sys.executable, str(SHOP / "try_it.py"), str(target)]

    first = subprocess.run(script, capture_output=True, text=True)
    second = subprocess.run(script, capture_output=True, text=True)

    assert first.returncode == 0, first.stderr
    assert (target / ".ratchet" / ".git").is_dir()
    assert not (target / "try_it.py").exists()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=target, capture_output=True,
                            text=True).stdout
    assert status == ""
    assert second.returncode != 0 and "already exists" in second.stderr


def test_the_code_checker_finds_the_planted_print(tmp_path: Path) -> None:
    """Proves the example's rules block parses, and that the checker reports pricing's print and
    nothing else."""
    root = copy_and_commit(tmp_path)
    init_project(root, assume_yes=False, confirm=None)
    project = load_project(root, commands=["audit"])
    design = read_target_design((root / ".ratchet" / "rules.md").read_text(encoding="utf-8"))

    titles = [finding.title for finding in check_architecture(project, design)]

    assert titles == ["shop.pricing touches the outside world, which part pricing may not"]
