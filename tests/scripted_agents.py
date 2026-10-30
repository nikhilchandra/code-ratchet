"""Stands in for the refactor agents in tests: a scripted agent, and short ways to write the
answers it gives."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

CRITERIA = ("scope", "readable", "editable", "necessary", "placed", "modular", "behavior",
            "tests", "comments")

# A turn is an answer, or a function that takes the prompt, may change files, and returns one.
Turn = dict | Callable[[str], dict]


class ScriptedAgent:
    """Stands in for an Agent, answering each turn from a script and keeping every prompt."""

    def __init__(self, name: str, schema: type[BaseModel], turns: list[Turn]) -> None:
        self.name = name
        self.schema = schema
        self.turns = list(turns)
        self.prompts: list[str] = []
        self.session_id = None
        self.total_cost_usd = 0.0
        self.total_tokens = 0

    def run(self, prompt: str) -> dict:
        """Returns the next scripted answer as a result, or a failed result when it is None."""
        self.prompts.append(prompt)
        turn = self.turns.pop(0)
        answer = turn(prompt) if callable(turn) else turn
        if answer is None:
            return {"ok": False, "data": None, "failure": "timed out after 1 s"}
        self.total_cost_usd += 0.5
        return {"ok": True, "data": self.schema.model_validate(answer), "failure": None}


def plan(**changes: object) -> dict:
    """Returns a plan with one check, with any field changed."""
    return {"stop": False, "reason": "", "step_title": "rename the comment in add",
            "summary": "rename the comment in add",
            "problems": ["P1"], "completes": ["P1"], "why_now": "", "changes": [], "added": [],
            "removed": [], "moved": [], "tests_affected": [],
            "checks": ["src/app.py defines add"], "risks": ""} | changes


def done(**changes: object) -> dict:
    """Returns an implementer's answer of done, with any field changed."""
    return {"status": "done", "summary": "", "deviations": [], "tests_changed": [],
            "blocked_reason": ""} | changes


def review(verdict: str, *, failing: tuple[str, ...] = (), check_passes: bool = True,
           fixes: list[dict] | None = None) -> dict:
    """Returns a review with a verdict, failing criteria, check 1's answer and comment fixes."""
    criteria = {name: {"pass": name not in failing, "findings": [
        {"file": "src/app.py", "line": 6, "problem": f"{name} is wrong", "fix": "do better"}
    ] if name in failing else []} for name in CRITERIA}
    return {"verdict": verdict, "summary": f"the verdict is {verdict}", "criteria": criteria,
            "plan_checks": [{"check": 1, "pass": check_passes, "evidence": "src/app.py:4"}],
            "comment_fixes": fixes or [], "notes": ["a note"]}


def edit(old: str, new: str) -> Callable[[str], dict]:
    """Returns a turn that replaces 'old' with 'new' in src/app.py and answers done."""
    def turn(prompt: str) -> dict:
        path = Path.cwd() / "src" / "app.py"
        path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
        return done()
    return turn


GOOD = ("# add them", "# adds a to b")
BREAK = ("return a + b", "return a - b")
