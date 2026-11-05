"""Runs one real audit through the claude CLI on a tiny project, to check the whole path works."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ratchet.audit.audit_run import APPROVE, settle_audit, start_audit
from ratchet.audit.settings import COMMAND
from ratchet.init_project import init_project
from ratchet.project import load_project

# The most the whole audit may spend, in dollars.
CAP_USD = 3.0

CONFIG = """\
test_command = ["python", "-m", "pytest", "-q"]
editable = ["src/**", "tests/**"]

[audit]
max_cost_usd = 3.0

[audit.auditor]
model = "sonnet"
budget_usd = 1.0
timeout_s = 600

[audit.list_reviewer]
model = "sonnet"
budget_usd = 0.5
timeout_s = 600

[audit.rechecker]
model = "sonnet"
budget_usd = 0.5
timeout_s = 600

[audit.pair_judge]
model = "sonnet"
budget_usd = 0.2
timeout_s = 300

[audit.checker]
model = "sonnet"
budget_usd = 0.6
timeout_s = 600
"""

RULES = """\
# Rules

Keep the code small. Each function does one job, and a fact is written in one place.
"""

SOURCE = '''\
"""Prices an order."""


def price_with_tax(amount):
    """Returns the amount plus 20 percent tax."""
    return round(amount * 1.2, 2)


def price_for_display(amount):
    """Returns the amount plus 20 percent tax."""
    return round(amount * 1.2, 2)
'''


def _git(root: Path, *arguments: str) -> None:
    """Runs one git command in the folder, failing the test when git fails."""
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True)


@pytest.mark.live
def test_a_first_audit_publishes_a_list_within_the_cap(tmp_path: Path) -> None:
    """Audits a project with one repeated fact, approves the draft, and checks the list is written."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "ratchet@example.com")
    _git(tmp_path, "config", "user.name", "ratchet")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "orders.py").write_text(SOURCE, encoding="utf-8")
    (tmp_path / "README.md").write_text("A tiny project.\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "start")
    init_project(tmp_path, assume_yes=True, confirm=None)
    (tmp_path / ".ratchet" / "config.toml").write_text(CONFIG, encoding="utf-8")
    (tmp_path / ".ratchet" / "rules.md").write_text(RULES, encoding="utf-8")
    project = load_project(tmp_path, commands=[COMMAND])

    run = start_audit(project, "look for repeated facts", interactive=False)
    assert run.outcome == "waiting"
    assert Path(run.draft_path).is_file()
    published = settle_audit(project, APPROVE)

    assert published.outcome == "published"
    assert (tmp_path / ".ratchet" / f"{published.list_name}.md").is_file()
    assert published.cost_usd <= CAP_USD
    print(f"\nlist: {published.list_name}  cost: ${published.cost_usd:.2f}")
