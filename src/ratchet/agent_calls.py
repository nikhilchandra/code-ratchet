"""The module contains the functions that ask agents a question and return their validated
answers, one agent at a time or several at once."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel
from rooster import Agent


class NoAnswerError(RuntimeError):
    """Signals an agent turn that did not complete, or whose reply did not fit the schema."""

    def __init__(self, message: str, log_path: Path | None = None) -> None:
        """Holds the message and the turn's log file.

        Args:
            message: What went wrong, which names the agent.
            log_path: The turn's log file, or None when the turn never started.
        """
        super().__init__(message)
        self.log_path = log_path


@dataclass(frozen=True)
class Answer:
    """The answer to one agent turn, with what the turn cost and where its log is."""

    # An instance of the agent's schema.
    data: BaseModel
    cost_usd: float
    # None when the turn left no log file.
    log_path: Path | None


def _save_prompt(agent: Agent, prompt: str, folder: Path) -> None:
    """Saves a turn's prompt in a folder, as <agent name>-prompt.md.

    Args:
        agent: The agent that is about to run.
        prompt: The turn's prompt.
        folder: The folder to save the prompt in, which is made when it is missing.
    """
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{agent.name}-prompt.md").write_text(prompt, encoding="utf-8")


def _read_result(agent: Agent, result: dict) -> Answer:
    """Turns an agent's result dict into an Answer.

    Args:
        agent: The agent that ran.
        result: The result that the agent's run() or arun() returned.

    Returns:
        The answer, with the cost and the log path from the result.

    Raises:
        NoAnswerError: The result says the turn did not complete.
    """
    log_path = result.get("log_path")
    if not result["ok"]:
        raise NoAnswerError(f"the {agent.name} gave no answer: {result['failure']}", log_path)
    return Answer(result["data"], result["cost_usd"], log_path)


def ask(agent: Agent, prompt: str, folder: Path) -> Answer:
    """Runs one agent turn and returns its answer, saving the prompt beside the attempt.

    Args:
        agent: The agent to run.
        prompt: The turn's prompt.
        folder: The folder that the prompt is saved in, as <agent name>-prompt.md.

    Returns:
        The agent's answer, as an instance of its schema, with the turn's cost and log path.

    Raises:
        NoAnswerError: The turn did not complete, or its answer did not fit the schema.
    """
    _save_prompt(agent, prompt, folder)
    return _read_result(agent, agent.run(prompt))


def ask_together(
    requests: list[tuple[Agent, str]], folder: Path
) -> list[Answer | NoAnswerError]:
    """Runs one turn of each of several agents at the same time.

    Each agent may appear once, since one agent must not run two turns at once. A turn that
    fails does not stop the others.

    Args:
        requests: Each agent with the prompt it is asked.
        folder: The folder that the prompts are saved in, as <agent name>-prompt.md.

    Returns:
        One item for each request, in the same order. An item is the agent's answer, or the
        NoAnswerError that its turn raised.

    Raises:
        Exception: Any error that a turn throws other than a failed result, such as an
            AgentError from the agent. It is raised after all the turns have finished.
    """
    for agent, prompt in requests:
        _save_prompt(agent, prompt, folder)

    async def run_all() -> list[dict | BaseException]:
        return await asyncio.gather(*(agent.arun(prompt) for agent, prompt in requests),
                                    return_exceptions=True)

    items: list[Answer | NoAnswerError] = []
    for (agent, _), result in zip(requests, asyncio.run(run_all())):
        if isinstance(result, BaseException):
            raise result
        try:
            items.append(_read_result(agent, result))
        except NoAnswerError as error:
            items.append(error)
    return items
