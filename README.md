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
- **`ratchet init`** sets up a project: it writes `.ratchet/config.toml` and `.ratchet/rules.md`
  from templates and makes `.ratchet` its own repository.
- **`ratchet audit -p "..."`** writes the problem list that `refactor` works through. Agents
  audit a blind copy of the code, and the owner approves the draft before it becomes a list.
- **`ratchet problems`** reads the problem lists and the history of each problem. It costs
  nothing.
- **`ratchet reopen P7 "reason"`** marks a finished problem as open again.
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
  state/                         ratchet's own records
```

Run `ratchet init` once to create this folder. It never overwrites an existing `rules.md`.

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

## The records repository

`.ratchet/` is its own git repository, and the project's `.gitignore` holds `.ratchet/`. Inside
it, the problem lists, `config.toml`, `rules.md`, `comments.md` and every
`state/<list name>/ledger.json` are tracked. Everything else under `state/` is ignored.
ratchet commits there when it publishes a list, when `reopen` runs, when it writes the config
or `rules.md`, and when a refactor run ends.

`ratchet init` also moves an existing project over. It refuses when the working tree has
changes. When the project's git tracks `.ratchet`, it asks first, or needs `--yes` with no
terminal, and then untracks the folder, adds the ignore line and makes one commit. A second run
changes nothing. `audit` and `reopen` refuse until init has run, and `refactor` only warns.

## Writing rules.md

`rules.md` says what the code must become. The template has five sections, with the essential
questions marked, and its first line is a marker that the audit refuses to start under:

```
<!-- ratchet: template. Delete this line once the essential questions are answered. -->
```

An optional TOML block lets a script check the rules with no agent. It holds `[parts]` (each
part maps to dotted module prefixes), `[may_use]` (which must name every part),
`[may_touch_outside_world]`, `[[exceptions]]` (with `from`, `to` and `why`) and `[skip]` for
generated or vendored code. Rules that audits settle with the owner are added under
"Decided in audits".

## Running an audit

```bash
ratchet audit -p "look at the views"      # audit the code, then ask the owner
ratchet audit --fresh                     # audit as a first audit, ignoring the old list
ratchet audit --approve                   # publish the draft of the audit that waits
ratchet audit --discard                   # throw that draft away
```

The first audit writes a list. Later audits are re-audits: they check every old problem
against the code as it stands, and keep each old id on the problem that continues it.

In a terminal the owner answers the audit's questions and then reads a summary of the draft,
with `[a]pprove, [e]dit, [d]iscard, [l]ater`. With no terminal the audit stops at its draft,
`.ratchet/state/audits/<audit name>/draft.md`, and waits for `--approve` or `--discard`. Only
one audit may wait at a time, and `--approve` is refused when HEAD has moved since the audit
began.

Agents work in a temporary copy of the code with real git. The copy has no branches, tags or
remote, and the old `.ratchet` files are stripped from its history.

**The format of an entry.** A published list gives each problem a heading, then field lines,
then its text, evidence, simpler option, "Done when" and tests:

```markdown
### P7. Strip handling is written twice

Status: open
Sources: L1, A2
Grade: stated (Breaks: "a fact is written in one place")
Subject: views.strip.draw
Carried from: problems-20261102-14h05m P7
After: P3
```

`Status` is new, open, reopened or blocked. `Grade` is stated, inferred or standard. `Carried
from` appears only for an entry that continues an old one, and `After` only for one that waits
on another. The list also holds "Questions for the owner" (ids `Q1`, each with an `Answer:`
line that the next re-audit reads), "Dropped" (ids `D1`) and "Bugs noticed".

**Settings.** The `[audit]` section of `config.toml`:

```toml
[audit]
hide = []                       # path patterns left out of the audit's copy
history_with_ratchet = "strip"  # or "refuse"
max_pairs = 40                  # most pairs the pair judge is asked about
max_cost_usd = 40.0             # the most one audit may spend, at list price

[audit.auditor]                 # opus, $10, 3600 s
[audit.list_reviewer]           # opus, $6, 2400 s
[audit.rechecker]               # opus, $6, 2400 s
[audit.pair_judge]              # sonnet, $1, 600 s
[audit.checker]                 # opus, $6, 2400 s
```

Before each agent step the audit checks that what it has spent plus that step's budget stays
within `max_cost_usd`, and halts when it would not.

## Reading problems

```bash
ratchet problems                # the newest list: id, title, status and grade
ratchet problems --all          # every list, oldest first
ratchet problems P7             # the full entry from the newest list that holds P7
ratchet problems P7 --history   # one line for each event, across every list it was carried in
ratchet reopen P7 "the vote is repeated again"
```

Each problem's ledger records dated events: opened, carried, job started, committed, done,
sent back, abandoned and reopened. A history follows `Carried from` back through the older
lists, so it reads as one timeline.

## Running a refactor

On a clean working tree whose test suite passes:

```bash
ratchet refactor --max-jobs 5             # up to five jobs, each one commit or undo
ratchet refactor --focus P19              # ask the planner to take P19 first
```

The run stops when the planner finds nothing left, after `--max-jobs` jobs, or on any error.
Each job's prompts, diffs, test logs and reports are kept in the list's state folder. When the
run ends, its ledger is committed in the records repository, and ratchet prints how many commits
have landed since the list was published, suggesting `ratchet audit` after ten or more.

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

The live audit test, `tests/test_audit_live.py`, runs a real first audit on a tiny project and
is capped at $3.
