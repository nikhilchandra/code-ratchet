"""The module contains tests that check every test that docs/scenarios.md names exists under
tests/, so the file does not point at tests that were renamed or removed.
"""

from __future__ import annotations

import re
from pathlib import Path

# The root of the code-ratchet repository.
ROOT = Path(__file__).resolve().parent.parent

# The scenarios file these tests read.
SCENARIOS = ROOT / "docs" / "scenarios.md"


def test_every_named_test_exists():
    text = SCENARIOS.read_text(encoding="utf-8")
    missing = []
    for path, name in re.findall(r"(tests/[\w/]+\.py)::(\w+)", text):
        source = (ROOT / path).read_text(encoding="utf-8")
        if not re.search(rf"^def {name}\(", source, re.MULTILINE):
            missing.append(f"{path}::{name}")
    test_sources = "\n".join(
        path.read_text(encoding="utf-8") for path in (ROOT / "tests").glob("test_*.py")
    )
    for name in re.findall(r"`(test_\w+)`", text):
        if not re.search(rf"^def {name}\(", test_sources, re.MULTILINE):
            missing.append(name)
    assert missing == []


def test_every_named_test_file_exists():
    text = SCENARIOS.read_text(encoding="utf-8")
    missing = [path for path in re.findall(r"tests/[\w/]+\.py", text) if not (ROOT / path).is_file()]
    assert missing == []
