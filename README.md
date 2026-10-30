# code-ratchet

Hand a coding goal to a team of AI agents, and let them work in small steps that are each
tested, reviewed and committed. Like a ratchet, the work only moves forward: a step that
passes is committed, and a step that fails is undone.

ratchet is built on [agent-rooster](../agent-rooster), which runs the agents and the graph of
steps. ratchet adds what is specific to code: the roles and their instructions, running the
tests, the checks between agents, and git.

## Commands

- **`ratchet refactor`** improves a project's structure while keeping its behavior the same.
  A planner picks the next step from the project's problem list, an implementer makes the
  change, the test suite and code checks guard it, and a reviewer judges it. A step that
  passes is committed, and one that fails is undone.
- **`ratchet audit -p "..."`** writes the problem list that `refactor` works through.
  *Planned.*
- **`ratchet feature`** and **`ratchet debug`**. *Planned for later.*

## Setting up a project

ratchet reads everything about a project from a `.ratchet` folder at the project's root.
Agents may never change anything in it.

```
.ratchet/
  config.toml                    the test command and the files agents may change
  rules.md                       what the code must become
  comments.md                    optional: the project's rules for comments
  problems-20261026-14h32m.md    a problem list, named after the minute it was made
  state/                         ratchet's own records, which git should ignore
```

A small `config.toml`:

```toml
test_command = ["uv", "run", "pytest", "-q"]
editable = ["src/**", "tests/**"]
frozen = ["**/*.feature"]

[refactor.caps]
rejections = 3
```

**Problem lists.** ratchet works on the newest list, chosen by the date and time in its name.
A new list never replaces an old one: write a new file, and carry over any problem from the
old list that is still unfinished. Each problem needs a heading that starts with its id:

```markdown
## P19. The controller is too big

Evidence, and a simpler option.
```

**The ledger.** Each problem list has its own ledger in `.ratchet/state/<list name>/`. It
records every job and each problem's status, so a second run on the same list carries on
where the first stopped, and a new list starts fresh.

## Running a refactor

On a clean working tree whose test suite passes:

```bash
ratchet refactor --max-jobs 5             # up to five jobs, each one commit or undo
ratchet refactor --focus P19              # ask the planner to take P19 first
```

The run stops when the planner finds nothing left, after `--max-jobs` jobs, or on any error.
Each job's prompts, diffs, test logs and reports are kept in the list's state folder.

## Install

The project is named `code-ratchet`, which is its name on PyPI. Its Python package and its
command are both `ratchet`, so code imports `from ratchet ...` and you type `ratchet refactor`.

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
