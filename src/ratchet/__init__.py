"""Hands a coding goal to a team of agents that work in small, tested, committed steps.

ratchet is built on agent-rooster. Its first command, `ratchet refactor`, improves a project's
structure one step at a time while keeping its behavior the same. A planner chooses each step,
an implementer makes it, the project's own tests and code checks guard it, and a reviewer judges
it. A step that passes is committed, and one that fails is undone.
"""
