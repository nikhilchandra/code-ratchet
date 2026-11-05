"""Tests that the job and audit graphs keep their comments in the house style."""

import inspect

from ratchet.refactor import job_graph


def comment_above(source: str, code: str) -> list[str]:
    """Return the comment lines directly above the first line that holds code."""
    lines = source.splitlines()
    index = next(i for i, line in enumerate(lines) if code in line)
    comments = []
    for line in reversed(lines[:index]):
        if not line.strip().startswith("#"):
            break
        comments.insert(0, line.strip())
    return comments


def test_test_step_has_one_narrating_comment_above_the_coverage_check():
    source = inspect.getsource(job_graph.build_job_graph)
    assert "An empty report means nothing was measured" not in source
    comments = comment_above(source, "if coverage and run.coverage:")
    assert len(comments) == 1
    text = comments[0].removeprefix("#").strip()
    assert text[0].islower()
    assert not text.endswith(".")


def test_job_graph_docstring_says_an_empty_coverage_report_counts_no_line():
    doc = " ".join(job_graph.__doc__.split())
    assert "coverage report is empty" in doc
    assert "no line counts as untested" in doc


def test_job_graph_docstring_gives_each_sendback_its_own_sentence():
    doc = " ".join(job_graph.__doc__.split())
    assert "sends the job back to implement with a report." in doc
    assert "A revise sends it back to plan." in doc
    assert "A refused test write sends it back to write-tests." in doc
    assert "Each has a cap, and a job that reaches one is undone." in doc


def test_job_graph_docstring_wraps_at_100_chars():
    lines = inspect.getsource(job_graph).split('"""')[1].splitlines()
    assert max(len(line) for line in lines) <= 100

