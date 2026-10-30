"""Makes the reviewer's comment fixes, after checking that they change only comments and
docstrings."""

from __future__ import annotations

import ast

from ratchet.project import Project
from ratchet.refactor.agent_answers import Review


def _code_without_comments(source: str) -> str:
    """Returns the parsed code of a Python source as text, leaving out its docstrings.

    Comments are not part of the parsed code, so two sources that differ only in comments and
    docstrings give the same text.

    Args:
        source: The Python source.

    Returns:
        The dump of the source's syntax tree, or the syntax error when it does not parse.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError as error:
        return f"syntax error: {error}"
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            node.body = node.body[1:]
    return ast.dump(tree)


def apply_comment_fixes(project: Project, review: Review) -> str:
    """Makes the reviewer's comment fixes in the working tree, and returns why they could not be
    made, or an empty string.

    Every fix is checked before any file is written, so a failure leaves the tree as it was. A fix
    must replace text that occurs exactly once in a Python file that agents may change. The file
    must then parse to the same code once its docstrings are left out, which shows that only
    comments and docstrings changed.

    Args:
        project: The project, as read from its .ratchet folder.
        review: The reviewer's answer, holding the fixes.

    Returns:
        Why the fixes were not made, or "" when all of them were.
    """
    faulted = [name for name, criterion in review.criteria if name != "comments" and not criterion.passed]
    if faulted:
        return f"the reviewer also failed {', '.join(faulted)}, which only the implementer may fix"
    if not review.comment_fixes:
        return "the reviewer wrote no comment fixes"

    # The text of each file before the fixes and after them, and whether it has Windows line
    # endings, which the fixes are matched without.
    before: dict[str, str] = {}
    after: dict[str, str] = {}
    crlf: dict[str, bool] = {}
    for fix in review.comment_fixes:
        name = fix.file.replace("\\", "/")
        path = project.root / name
        if not name.endswith(".py") or not project.may_change(name) or not path.is_file():
            return f"{name} is not a Python file that a step may change"
        if name not in before:
            text = path.read_bytes().decode("utf-8")
            crlf[name] = "\r\n" in text
            before[name] = after[name] = text.replace("\r\n", "\n")
        count = after[name].count(fix.old)
        if count != 1:
            return f"the text that a fix to {name} replaces occurs {count} times, not once"
        after[name] = after[name].replace(fix.old, fix.new)

    # a fix that changes code, or breaks the syntax, stops all of them
    for name, text in after.items():
        if _code_without_comments(text) != _code_without_comments(before[name]):
            return f"a fix to {name} changes code, not only comments and docstrings"
    for name, text in after.items():
        written = text.replace("\n", "\r\n") if crlf[name] else text
        (project.root / name).write_bytes(written.encode("utf-8"))
    return ""
