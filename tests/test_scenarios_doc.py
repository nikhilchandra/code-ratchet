"""The module contains tests that keep docs/scenarios.md plain and short. Each numbered step is
one or two sentences with no test names, and each heading has one "Tests:" line. Every test that
the file names exists under tests/.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

# The root of the code-ratchet repository.
ROOT = Path(__file__).resolve().parent.parent

# The scenarios file these tests read.
SCENARIOS = ROOT / "docs" / "scenarios.md"

# The headings the file holds, in order.
HEADINGS = [
    "ratchet init",
    "The first audit",
    "A re-audit",
    "A problem marked done by refactor, then reopened by a re-audit",
    "A refactor run",
    "A refactor job that halts",
    "ratchet reopen",
    "ratchet problems and --history",
]

# Words of internals that the refactor steps leave out.
INTERNALS = ["neighbour", "git show", "unified_diff", ".coveragerc", "`a/`", "now names"]


def sections() -> dict[str, list[str]]:
    """Returns the lines under each "## " heading of docs/scenarios.md, keyed by the heading."""
    found: dict[str, list[str]] = {}
    lines: list[str] | None = None
    for line in SCENARIOS.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            lines = found.setdefault(line[3:].strip(), [])
        elif lines is not None:
            lines.append(line)
    return found


def steps() -> list[tuple[str, str]]:
    """Returns each numbered step of docs/scenarios.md with the heading it sits under."""
    return [
        (heading, line)
        for heading, lines in sections().items()
        for line in lines
        if re.match(r"\d+\. ", line)
    ]


def sentence_count(text: str) -> int:
    """Returns how many sentences text holds, not counting a leading step number or the dots
    inside backticks.
    """
    plain = re.sub(r"`[^`]*`", "X", re.sub(r"^\d+\. ", "", text))
    return len(re.findall(r"[.!?](?=\s|$)", plain))


def test_the_headings_stay_the_same():
    assert list(sections()) == HEADINGS


@pytest.mark.parametrize("heading", HEADINGS)
def test_each_heading_has_one_tests_line(heading):
    lines = sections()[heading]
    tests_lines = [line for line in lines if line.startswith("Tests")]
    assert len(tests_lines) == 1, tests_lines
    assert tests_lines[0].startswith("Tests: ")
    assert sum("Tests:" in line for line in lines) == 1


def test_no_step_names_a_test():
    named = [
        line
        for _, line in steps()
        if "Tests:" in line or "tests/" in line or re.search(r"\btest_\w+", line)
    ]
    assert named == []


def test_each_step_is_one_or_two_sentences():
    long_steps = [line[:60] for _, line in steps() if sentence_count(line) > 2]
    assert long_steps == []


@pytest.mark.parametrize("word", INTERNALS)
def test_the_internals_are_left_out(word):
    assert word not in SCENARIOS.read_text(encoding="utf-8")


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
