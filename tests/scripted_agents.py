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
    """Stands in for an Agent, answering each turn from a script and keeping every prompt. Each
    answered turn costs 'cost_usd' dollars, and a failed turn costs nothing. The agent also
    keeps its 'frozen' paths as they stood at the start of each turn, in 'frozen_seen'."""

    def __init__(self, name: str, schema: type[BaseModel], turns: list[Turn], *,
                 cost_usd: float = 0.5) -> None:
        self.name = name
        self.schema = schema
        self.turns = list(turns)
        self.cost_usd = cost_usd
        self.prompts: list[str] = []
        self.session_id = None
        self.total_cost_usd = 0.0
        self.total_tokens = 0
        self.frozen: tuple[str, ...] = ()
        self.frozen_seen: list[tuple[str, ...]] = []

    def run(self, prompt: str) -> dict:
        """Returns the next scripted answer as a result, or a failed result when it is None."""
        self.prompts.append(prompt)
        self.frozen_seen.append(tuple(self.frozen))
        turn = self.turns.pop(0)
        answer = turn(prompt) if callable(turn) else turn
        if answer is None:
            return {"ok": False, "data": None, "failure": "timed out after 1 s", "cost_usd": 0.0,
                    "log_path": None}
        self.total_cost_usd += self.cost_usd
        return {"ok": True, "data": self.schema.model_validate(answer), "failure": None,
                "cost_usd": self.cost_usd, "log_path": Path(f"{self.name}.log")}

    async def arun(self, prompt: str) -> dict:
        """Returns the same result that run() does, for callers that await."""
        return self.run(prompt)


def plan(**changes: object) -> dict:
    """Returns a plan with one check, with any field changed."""
    return {"stop": False, "reason": "", "step_title": "rename the comment in add",
            "summary": "rename the comment in add",
            "problems": ["P1"], "completes": ["P1"], "functions": ["app.add"], "why_now": "", "changes": [], "added": [],
            "removed": [], "moved": [], "tests_affected": [],
            "checks": ["src/app.py defines add"], "risks": ""} | changes


def approve(*tests: dict) -> dict:
    """Returns a super-planner's approval that asks for the tests given, as wanted() makes them."""
    return {"verdict": "approve", "summary": "the plan fits", "paths": ["app.add"],
            "untested_paths": [], "tests_to_write": list(tests), "revisions": []}


def revise(*revisions: str) -> dict:
    """Returns a super-planner's answer of revise, with the revisions given."""
    return {"verdict": "revise", "summary": "revise the plan", "paths": ["app.add"],
            "untested_paths": [], "tests_to_write": [], "revisions": list(revisions)}


def wanted(file: str = "tests/test_more.py") -> dict:
    """Returns one test that the super-planner asks for, in 'file'."""
    return {"file": file, "name": "test_more", "covers": "app.add", "checks": "add(2, 2) is 4"}


def write_test(path: str, body: str, name: str = "test_more") -> Callable[[str], dict]:
    """Returns a test writer's turn that writes 'body' to the file at 'path', from the project's
    folder, with Unix line endings, and answers that it wrote the test 'name' there."""
    def turn(prompt: str) -> dict:
        file = Path.cwd() / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(body, encoding="utf-8", newline="\n")
        return {"summary": f"wrote {name}",
                "tests": [{"file": path, "name": name, "covers": "app.add"}]}
    return turn


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
    """Returns a turn that replaces 'old' with 'new' in src/app.py and answers done, keeping the
    file's Unix line endings so that an edit undone by hand matches the commit again."""
    def turn(prompt: str) -> dict:
        path = Path.cwd() / "src" / "app.py"
        text = path.read_text(encoding="utf-8").replace(old, new)
        path.write_text(text, encoding="utf-8", newline="\n")
        return done()
    return turn


GOOD = ("# add them", "# adds a to b")
BREAK = ("return a + b", "return a - b")


# The committed text of src/shop/cli.py, a package module that the pathway map holds.
SHOP_CLI = ('"""Totals a basket."""\n\n\ndef total(prices):\n    """Returns the sum."""\n'
            '    return sum(prices)\n\n\ndef main():\n    """Shows the total."""\n'
            '    print(total([1, 2]))\n')
# A function that no test calls, which a turn appends to a module.
SUB = '\n\ndef sub(a, b):\n    """Returns the difference."""\n    return a - b\n'


def commit_shop(project, repo) -> None:
    """Writes the package src/shop, with an empty __init__.py and cli.py as SHOP_CLI holds it,
    and commits it to the project's repository 'repo'."""
    package = project.root / "src" / "shop"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text(SHOP_CLI, encoding="utf-8", newline="\n")
    repo.commit("add a shop")


def append_sub(path: str) -> None:
    """Appends SUB to the file at 'path', from the project's folder, with Unix line endings."""
    file = Path.cwd() / path
    text = file.read_text(encoding="utf-8") + SUB
    file.write_text(text, encoding="utf-8", newline="\n")


def add_shop_sub(prompt: str) -> dict:
    """Appends sub to src/shop/cli.py and answers done, as an implementer's turn."""
    append_sub("src/shop/cli.py")
    return done()


def add_app_sub(prompt: str) -> dict:
    """Appends sub to src/app.py and answers done, as an implementer's turn."""
    append_sub("src/app.py")
    return done()


def write_sub_test(module: str) -> Callable[[str], dict]:
    """Returns an implementer's turn that writes tests/test_sub.py, which imports sub from the
    module named 'module' and checks that sub(3, 1) is 2, and answers done."""
    def turn(prompt: str) -> dict:
        file = Path.cwd() / "tests" / "test_sub.py"
        file.write_text(f"from {module} import sub\n\n\ndef test_sub():\n"
                        "    assert sub(3, 1) == 2\n", encoding="utf-8", newline="\n")
        return done()
    return turn
