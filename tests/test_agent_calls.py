"""Proves the shared ways to ask agents return answers, cost and log path, and report a turn
that gave no answer."""

from __future__ import annotations

from pathlib import Path

import pytest

from ratchet.agent_calls import Answer, NoAnswerError, ask, ask_together
from ratchet.refactor.agent_answers import Plan
from scripted_agents import ScriptedAgent, plan


def test_ask_returns_the_answer_cost_and_log_path(tmp_path: Path) -> None:
    """Proves ask() validates the answer, passes on the cost and log path, and saves the prompt."""
    agent = ScriptedAgent("planner", Plan, [plan()])

    answer = ask(agent, "what next?", tmp_path / "prompts")

    assert isinstance(answer, Answer)
    assert answer.data.step_title == "rename the comment in add"
    assert answer.cost_usd == 0.5
    assert answer.log_path == Path("planner.log")
    assert (tmp_path / "prompts" / "planner-prompt.md").read_text(encoding="utf-8") == "what next?"


def test_ask_raises_when_the_turn_gives_no_answer(tmp_path: Path) -> None:
    """Proves a failed turn raises NoAnswerError naming the agent and the failure."""
    agent = ScriptedAgent("planner", Plan, [None])

    with pytest.raises(NoAnswerError, match="the planner gave no answer: timed out after 1 s"):
        ask(agent, "what next?", tmp_path)

    assert (tmp_path / "planner-prompt.md").exists()


def test_ask_together_answers_in_request_order(tmp_path: Path) -> None:
    """Proves each agent gets its own prompt and its answer comes back in request order."""
    first = ScriptedAgent("first", Plan, [plan(step_title="one")])
    second = ScriptedAgent("second", Plan, [plan(step_title="two")])

    answers = ask_together([(first, "p1"), (second, "p2")], tmp_path)

    assert [item.data.step_title for item in answers] == ["one", "two"]
    assert first.prompts == ["p1"]
    assert second.prompts == ["p2"]
    assert (tmp_path / "second-prompt.md").read_text(encoding="utf-8") == "p2"


def test_ask_together_keeps_going_when_one_turn_fails(tmp_path: Path) -> None:
    """Proves a failed turn comes back as an error in its place while the others answer."""
    first = ScriptedAgent("first", Plan, [None])
    second = ScriptedAgent("second", Plan, [plan()])

    failed, answered = ask_together([(first, "p1"), (second, "p2")], tmp_path)

    assert isinstance(failed, NoAnswerError)
    assert "the first gave no answer" in str(failed)
    assert failed.log_path is None
    assert isinstance(answered, Answer)
