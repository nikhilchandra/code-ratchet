"""The package audits a Python project's design, then fixes it one small, tested step at a time
with Claude agents.

`ratchet audit` has agents read the code and write a list of problems, which the owner approves
before it is saved. `ratchet refactor` works through that list one problem at a time. A planner
plans each step, a super-planner checks the plan against the rest of the code, a test writer writes
tests first, an implementer makes the change, and a reviewer judges it. Plain code checks the work
between agents. A step that passes is committed, and one that fails is undone. ratchet is built on
agent-rooster.
"""
