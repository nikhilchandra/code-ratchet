"""Checks the questions and the approval that the owner sees in a terminal."""

from __future__ import annotations

import io
from pathlib import Path

from ratchet.audit.list_markdown import Draft, DraftProblem, DraftQuestion, render_draft
from ratchet.owner_terminal import (
    ask_questions,
    default_editor_command,
    draft_summary,
    is_interactive,
    review_draft,
)


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def make_draft() -> Draft:
    return Draft(
        problems=[
            DraftProblem(
                id="P1", title="strip written twice", status="new", sources=["L1", "C1"],
                grade="stated", breaks="no cycles", subject="views.strip",
                what_is_wrong="Repeated.", notes=["check this one"],
            ),
            DraftProblem(
                id="P2", title="hidden import", status="open", sources=["O P2"],
                grade="standard", breaks="S2", subject="cli.main", what_is_wrong="Hidden.",
            ),
        ],
        questions=[DraftQuestion(id="Q1", title="Is rounding deliberate?", text="It rounds twice.")],
        notes=["HEAD is clean"],
    )


def scripted(replies: list[str]):
    def ask(prompt: str) -> str:
        return replies.pop(0)

    return ask


def test_interactive_needs_both_streams():
    assert is_interactive(Tty(), Tty())
    assert not is_interactive(io.StringIO(), Tty())
    assert not is_interactive(Tty(), io.StringIO())


def test_questions_skip_empty_and_dont_know():
    questions = [DraftQuestion(id=f"Q{n}", title=f"title {n}", text="") for n in (1, 2, 3)]
    shown: list[str] = []
    ask = scripted(["yes it is deliberate", "", "Don't know"])
    assert ask_questions(questions, ask, shown.append) == {"Q1": "yes it is deliberate"}
    assert "Q2. title 2" in shown


def test_summary_counts_and_notes():
    text = draft_summary(make_draft())
    assert "2 problems, 1 questions, 0 dropped, 0 bugs" in text
    assert "By status: new 1, open 1" in text
    assert "By grade: standard 1, stated 1" in text
    assert "P1  new  stated  [L1, C1]  strip written twice" in text
    assert "- HEAD is clean" in text
    assert "- check this one" in text


def test_editor_command_prefers_visual(monkeypatch):
    monkeypatch.setenv("EDITOR", "nano")
    monkeypatch.setenv("VISUAL", "code -w")
    assert default_editor_command() == "code -w"
    monkeypatch.delenv("VISUAL")
    assert default_editor_command() == "nano"
    monkeypatch.delenv("EDITOR")
    assert default_editor_command() in ("notepad", "vi")


def review(tmp_path: Path, replies: list[str], head_moved=lambda: False, edit=lambda path: None):
    draft = make_draft()
    path = tmp_path / "draft.md"
    path.write_text(render_draft(draft), encoding="utf-8")
    shown: list[str] = []
    result = review_draft(draft, path, [], head_moved, scripted(replies), shown.append, edit)
    return result, shown


def test_approve_discard_and_later(tmp_path):
    assert review(tmp_path, ["a"])[0] == {"decision": "approve"}
    assert review(tmp_path, ["d"])[0] == {"decision": "discard"}
    assert review(tmp_path, ["l"])[0] is None


def test_unknown_choice_asks_again(tmp_path):
    result, shown = review(tmp_path, ["x", "d"])
    assert result == {"decision": "discard"}
    assert "Please answer a, e, d or l." in shown


def test_approve_refused_when_head_moved(tmp_path):
    result, shown = review(tmp_path, ["a", "l"], head_moved=lambda: True)
    assert result is None
    assert any("HEAD has moved" in line for line in shown)


def test_edit_is_checked_then_asks_again(tmp_path):
    def break_draft(path: Path) -> None:
        text = path.read_text(encoding="utf-8").replace("Status: new", "Status: bogus")
        path.write_text(text, encoding="utf-8")

    result, shown = review(tmp_path, ["e", "a", "d"], edit=break_draft)
    assert result == {"decision": "discard"}
    assert sum("Status 'bogus'" in line for line in shown) == 2
