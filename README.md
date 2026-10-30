# ratchet

Hand a coding goal to a team of AI agents, and let them work in small steps that are each
tested, reviewed and committed. Like a ratchet, the work only moves forward: a step that
passes is committed, and a step that fails is undone.

ratchet is built on [agent-rooster](../agent-rooster), which runs the agents and the graph of
steps. ratchet adds what is specific to code: the roles and their instructions, running the
tests, the checks between agents, and git.

## Commands

- **`ratchet refactor -p "..."`** improves a project's structure while keeping its behavior
  the same. A planner picks the next step from the project's problem list, an implementer
  makes the change, the test suite and code checks guard it, and a reviewer judges it.
  *In progress.*
- **`ratchet audit -p "..."`** writes the problem list that `refactor` works through.
  *Planned.*
- **`ratchet feature`** and **`ratchet debug`**. *Planned for later.*

## Install

ratchet uses agent-rooster from the folder beside it, as an editable path. agent-rooster must
be on its `new` branch, because ratchet needs the API that exists only there.

```bash
uv sync
```

## Tests

```bash
uv run pytest -m "not live"   # free, no Claude calls
uv run pytest                 # also runs the live tests, which spend a little of your plan
```
